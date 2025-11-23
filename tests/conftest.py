import os
import tempfile
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest_asyncio
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config import Config
from db import Base, MessageQueue, NewUser, AdminSettings
from tests.test_server_validation import validate_test_server_ip
from telegram import create_bot
from queue_processor import QueueProcessor


# Define session-scoped event loop to ensure all session-scoped async fixtures
# (like Telegram clients) share the same loop and persist across tests.
@pytest_asyncio.fixture(scope="session")
def event_loop():
    """Provide a session-scoped event loop for all async fixtures and tests"""
    policy = asyncio.get_event_loop_policy()
    loop = policy.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def test_config():
    """Create a test configuration"""
    # Create unique session files for each test to avoid database locking
    bot_session = tempfile.NamedTemporaryFile(delete=False, suffix='.session')
    userbot_session = tempfile.NamedTemporaryFile(delete=False, suffix='.session')
    
    config = Config.for_testing(
        bot_session_path=bot_session.name,
        userbot_session_path=userbot_session.name,
        # New: a default per-group logging channel mapping used by the bot
        LOG_CHANNEL_MAP=f"{67890}:{67891},{12345}:{12346}"
    )
    
    # Close the temp files so Telethon can use them
    bot_session.close()
    userbot_session.close()
    
    yield config
    
    # Cleanup session files
    for session_file in [bot_session.name, userbot_session.name]:
        if os.path.exists(session_file):
            try:
                os.unlink(session_file)
            except:
                pass  # Ignore cleanup errors


@pytest.fixture
def temp_db():
    """Create a temporary SQLite database for testing"""
    with tempfile.NamedTemporaryFile(delete=False, suffix='.db') as tmp_file:
        db_path = tmp_file.name
    
    yield db_path
    
    # Cleanup
    if os.path.exists(db_path):
        os.unlink(db_path)


@pytest.fixture
def session_factory(temp_db, test_config):
    """Create a sessionmaker for tests using the same setup as production.

    This ensures tests use the same WAL mode, busy_timeout, and other PRAGMA
    settings as production, making concurrency tests realistic.
    """
    from db import make_session_factory

    # Update test config to use the temp database
    test_config.db_path = temp_db

    # Use the production session factory setup to inherit all PRAGMA settings
    factory = make_session_factory(test_config)

    # Create all tables
    Base.metadata.create_all(factory().bind)

    return factory


@pytest.fixture
def test_session(session_factory, test_config):
    """Create a test database session"""
    session = session_factory()

    # Add default admin settings
    admin_settings = AdminSettings(require_approval=False)
    session.add(admin_settings)
    session.commit()

    yield session

    session.close()


@pytest.fixture
def mock_llm():
    """Mock LLM with configurable spam detection"""
    mock = AsyncMock()
    mock.is_spam = AsyncMock(return_value=False)
    return mock


@pytest.fixture
def mock_telegram_client():
    """Mock Telegram client"""
    mock = AsyncMock()
    mock.send_message = AsyncMock()
    mock.get_entity = AsyncMock()
    # New methods used by bot-driven moderation:
    mock.delete_messages = AsyncMock()
    # Mock __call__ for EditBannedRequest and other TL functions
    mock.return_value = AsyncMock()
    return mock


@pytest.fixture
def message_queue_factory(test_session):
    """Factory for creating message queue entries with configurable parameters"""
    def _create_message_queue(
        user_id=12345,
        chat_id=67890,
        message_id=111,
        message_text="Test message content",
        status='pending',
        retry_count=0,
        max_retries=5,
        processed_at=None,
        error_message=None,
        spam_result=None
    ):
        queue_item = MessageQueue(
            user_id=user_id,
            chat_id=chat_id,
            message_id=message_id,
            message_text=message_text,
            status=status,
            retry_count=retry_count,
            max_retries=max_retries,
            processed_at=processed_at,
            error_message=error_message,
            spam_result=spam_result
        )
        test_session.add(queue_item)
        test_session.commit()
        return queue_item
    return _create_message_queue


@pytest.fixture
def sample_message_queue(message_queue_factory):
    """Create a sample message queue entry with default values"""
    return message_queue_factory()


@pytest.fixture
def new_user_factory(test_session):
    """Factory for creating new user entries with configurable parameters"""
    def _create_new_user(user_id=12345, chat_id=67890):
        new_user = NewUser(user_id=user_id, chat_id=chat_id)
        test_session.add(new_user)
        test_session.commit()
        return new_user
    return _create_new_user


@pytest.fixture
def sample_new_user(new_user_factory):
    """Create a sample new user with default values"""
    return new_user_factory()


@pytest.fixture
def mock_queue_processor():
    """Mock queue processor for testing"""
    mock = MagicMock()
    mock.add_message_to_queue = MagicMock()
    mock.get_queue_status = MagicMock(return_value={
        'pending': 0,
        'processing': 0,
        'completed': 0,
        'failed': 0,
        'total': 0
    })
    mock.retry_failed_messages = MagicMock(return_value=0)
    mock.clear_completed_messages = MagicMock(return_value=0)
    return mock


@pytest.fixture
def fast_asyncio_sleep():
    """Speed up asyncio.sleep for tests by 10x (e.g., 1 second becomes 0.1 seconds)

    This fixture patches asyncio.sleep to run faster in tests, significantly
    reducing test execution time for tests that need to wait for async operations.
    """
    original_sleep = asyncio.sleep
    speed_factor = 10  # Sleep will be 10x faster

    async def faster_sleep(delay, result=None):
        """Sleep for a much shorter duration in tests"""
        return await original_sleep(delay / speed_factor, result)

    with patch('asyncio.sleep', side_effect=faster_sleep):
        yield

    # Restore original sleep (cleanup happens automatically with context manager)


# Import from test_utils to avoid circular imports with e2e_world
from tests.test_utils import wait_for_condition

__all__ = ['wait_for_condition']


# =====================================================================
# E2E Integration Test Fixtures (for tests/test_e2e_telegram.py)
# =====================================================================


def _require_e2e_env(*names):
    """Skip test if required E2E environment variables are missing"""
    missing = [n for n in names if not os.getenv(n)]
    if missing:
        pytest.skip(f"Missing E2E env vars: {', '.join(missing)}")



@pytest.fixture(scope="session")
def e2e_config():
    """Configuration for E2E tests using Telegram test servers"""
    from dotenv import load_dotenv
    # Load .env.test if it exists
    load_dotenv(".env.test")

    _require_e2e_env(
        "TEST_API_ID", "TEST_API_HASH",
        "TEST_USER_SESSION", "TEST_ADMIN_ID",
        "TEST_BOT_TOKEN"
    )

    # Get datacenter configuration
    dc_ip = os.getenv("TEST_DATACENTER_IP", "149.154.167.40")
    dc_port = int(os.getenv("TEST_DATACENTER_PORT", "80"))
    dc_id = int(os.getenv("TEST_DC_ID", "2"))

    # KILL-SWITCH: Validate that configured datacenter is a test server
    validate_test_server_ip(dc_ip, "E2E config (TEST_DATACENTER_IP)")

    return {
        'api_id': int(os.getenv("TEST_API_ID")),
        'api_hash': os.getenv("TEST_API_HASH"),
        'user_session': os.getenv("TEST_USER_SESSION"),
        'admin_session': os.getenv("TEST_ADMIN_SESSION", os.getenv("TEST_USER_SESSION")),
        'admin_id': int(os.getenv("TEST_ADMIN_ID")),
        # Required: Pre-created bot token (create using scripts/setup_test_bot.py)
        'bot_token': os.getenv("TEST_BOT_TOKEN"),
        # Datacenter configuration (validated above)
        'dc_ip': dc_ip,
        'dc_port': dc_port,
        'dc_id': dc_id,
    }


@pytest.fixture(scope="session")
def e2e_bot_token(e2e_config):
    """Get bot token for E2E tests.

    Returns the pre-created bot token from TEST_BOT_TOKEN environment variable.
    The bot must be created beforehand using scripts/setup_test_bot.py.
    """
    return e2e_config['bot_token']


@pytest_asyncio.fixture(scope="session")
async def e2e_bot_client(e2e_config, e2e_bot_token, e2e_admin_client, tmp_path_factory):
    """Real bot client connected to Telegram test servers (test.telegram.org)

    This client will have the bot's event handlers registered and will process
    messages in the test group just like in production.
    """
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    # Create a temporary session directory
    session_dir = tmp_path_factory.mktemp("e2e_sessions")
    session_file = str(session_dir / "e2e_bot.session")

    # Connect to test servers
    client = TelegramClient(
        session_file,
        e2e_config['api_id'],
        e2e_config['api_hash'],
        use_ipv6=False
    )

    # CRITICAL: We must force the bot to use the configured Test Server DC.
    # By default, a new file session connects to Production.
    # We use the datacenter configuration from e2e_config (or fall back to admin session).
    client.session.set_dc(
        e2e_config['dc_id'],
        e2e_config['dc_ip'],
        e2e_config['dc_port']
    )

    await client.connect()
    await client.start(bot_token=e2e_bot_token)

    yield client

    import time
    import asyncio
    import logging
    logger = logging.getLogger(__name__)
    start_time = time.time()
    logger.info("[SESSION TEARDOWN] Disconnecting bot client...")
    try:
        await asyncio.wait_for(client.disconnect(), timeout=5.0)
        elapsed = time.time() - start_time
        logger.info(f"[SESSION TEARDOWN] ✓ Bot client disconnected in {elapsed:.2f}s")
    except asyncio.TimeoutError:
        elapsed = time.time() - start_time
        logger.warning(f"[SESSION TEARDOWN] Bot client disconnect timed out after {elapsed:.2f}s, forcing...")


@pytest_asyncio.fixture(scope="session")
async def e2e_user_client(e2e_config, tmp_path_factory):
    """Real user client connected to Telegram test servers

    This client simulates a regular user for testing bot interactions.
    """
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    # Create client with StringSession
    # The StringSession already contains DC info, so we don't need to set it manually
    client = TelegramClient(
        StringSession(e2e_config['user_session']),
        e2e_config['api_id'],
        e2e_config['api_hash'],
        use_ipv6=False
    )

    await client.connect()

    if not await client.is_user_authorized():
        pytest.skip("TEST_USER_SESSION is not authorized. Generate a valid StringSession for the test user.")

    yield client

    import time
    import asyncio
    import logging
    logger = logging.getLogger(__name__)
    start_time = time.time()
    logger.info("[SESSION TEARDOWN] Disconnecting user client...")
    try:
        await asyncio.wait_for(client.disconnect(), timeout=5.0)
        elapsed = time.time() - start_time
        logger.info(f"[SESSION TEARDOWN] ✓ User client disconnected in {elapsed:.2f}s")
    except asyncio.TimeoutError:
        elapsed = time.time() - start_time
        logger.warning(f"[SESSION TEARDOWN] User client disconnect timed out after {elapsed:.2f}s, forcing...")


@pytest_asyncio.fixture(scope="session")
async def e2e_admin_client(e2e_config, tmp_path_factory):
    """Real admin client connected to Telegram test servers

    This client simulates the admin user for testing approval workflows.
    """
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    # Create client with StringSession
    # The StringSession already contains DC info from when it was generated
    client = TelegramClient(
        StringSession(e2e_config['admin_session']),
        e2e_config['api_id'],
        e2e_config['api_hash'],
        use_ipv6=False
    )

    await client.connect()

    if not await client.is_user_authorized():
        pytest.skip("TEST_ADMIN_SESSION is not authorized. Generate a valid StringSession for the test admin.")

    # KILL-SWITCH: Ensure we are actually on a test server
    server_ip = client.session.server_address
    try:
        validate_test_server_ip(server_ip, "E2E admin client (TEST_ADMIN_SESSION)")
    except RuntimeError as e:
        await client.disconnect()
        raise e

    yield client

    import time
    import asyncio
    import logging
    logger = logging.getLogger(__name__)
    start_time = time.time()
    logger.info("[SESSION TEARDOWN] Disconnecting admin client...")
    try:
        await asyncio.wait_for(client.disconnect(), timeout=5.0)
        elapsed = time.time() - start_time
        logger.info(f"[SESSION TEARDOWN] ✓ Admin client disconnected in {elapsed:.2f}s")
    except asyncio.TimeoutError:
        elapsed = time.time() - start_time
        logger.warning(f"[SESSION TEARDOWN] Admin client disconnect timed out after {elapsed:.2f}s, forcing...")


@pytest_asyncio.fixture(scope="session")
async def e2e_bot_username(e2e_bot_client):
    """Get the bot's username for opening conversations"""
    me = await e2e_bot_client.get_me()
    if not me.username:
        pytest.skip("Bot has no public username set. Set one via @BotFather.")
    return me.username


@pytest_asyncio.fixture(scope="function")
async def e2e_bot_env(e2e_bot_client, e2e_session_factory, e2e_config, e2e_bot_token, e2e_test_group_id, mock_llm_e2e, e2e_admin_client):
    """Initialize the bot application for each E2E test with a fresh group"""
    import logging

    # Set reasonable logging levels for E2E tests
    logging.getLogger('telethon').setLevel(logging.WARNING)
    logging.getLogger('telegram').setLevel(logging.INFO)  # INFO level for telegram
    logging.getLogger('queue_processor').setLevel(logging.INFO)
    logging.getLogger('tests.e2e_world').setLevel(logging.DEBUG)  # DEBUG to see wait_for_message_from_bot logs
    logging.getLogger('tests.test_e2e_telegram').setLevel(logging.INFO)
    logging.getLogger('tests.conftest').setLevel(logging.INFO)
    logging.getLogger('e2e_world').setLevel(logging.DEBUG)  # DEBUG to see wait_for_message_from_bot logs

    # Create a minimal config object for the bot
    class E2EBotConfig:
        def __init__(self, e2e_config, group_id, token):
            self.api_id = e2e_config['api_id']
            self.api_hash = e2e_config['api_hash']
            self.bot_session_path = "e2e_bot.session"
            self.userbot_session_path = "e2e_userbot.session"
            self.bot_token = token
            self.admin_id = e2e_config['admin_id']
            self.tracking_chat_ids = [group_id]
            self.log_channel_map = {}
            self.default_purge_count = 10

    config = E2EBotConfig(e2e_config, e2e_test_group_id, e2e_bot_token)
    logging.getLogger(__name__).info(f"[E2E_BOT_ENV] Bot config created for group {e2e_test_group_id}")

    # Initialize admin peer by having admin send /start to bot
    # This allows the bot to send messages to the admin
    try:
        bot_me = await e2e_bot_client.get_me()
        logging.getLogger(__name__).info(f"[E2E_BOT_ENV] Bot identity: {bot_me.id} / @{bot_me.username}")
        await e2e_admin_client.send_message(bot_me.username or bot_me.id, "/start")
        # Give Telegram time to process and establish the peer relationship
        await asyncio.sleep(1)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Failed to initialize admin peer: {e}")

    # Create the bot with explicit client parameter (no patching needed)
    logging.getLogger(__name__).info(f"[E2E_BOT_ENV] Creating bot with create_bot()")
    bot = create_bot(e2e_session_factory, mock_llm_e2e, config, client=e2e_bot_client)
    logging.getLogger(__name__).info(f"[E2E_BOT_ENV] Bot created, client connected: {bot.is_connected()}")

    # Create and attach queue processor BEFORE catching up
    # This ensures the processor is ready when updates start flowing
    queue_processor = QueueProcessor(
        e2e_session_factory,
        mock_llm_e2e,
        e2e_bot_client,
        config,
        processing_delay=0.5,  # Fast processing for tests
        max_concurrent_jobs=3
    )
    bot.queue_processor = queue_processor

    # Start queue processor in background
    processor_task = asyncio.create_task(queue_processor.start())
    logging.getLogger(__name__).info("[E2E_BOT_ENV] Queue processor started")

    # Now catch up with updates - the processor is ready to handle them
    await bot.catch_up()
    logging.getLogger(__name__).info("[E2E_BOT_ENV] Bot client catching up with updates")

    yield {
        'bot': bot,
        'queue_processor': queue_processor,
        'config': config
    }

    # Cleanup at end of session
    import time
    start_time = time.time()
    logging.getLogger(__name__).info("[BOT_ENV TEARDOWN] Starting cleanup")

    queue_processor.stop()
    # Wait a bit for tasks to cancel
    await asyncio.sleep(0.5)
    processor_task.cancel()
    try:
        await processor_task
    except asyncio.CancelledError:
        pass

    elapsed = time.time() - start_time
    logging.getLogger(__name__).info(f"[BOT_ENV TEARDOWN] ✓ Cleanup completed in {elapsed:.2f}s")


@pytest_asyncio.fixture(scope="function")
async def e2e_test_group_id(e2e_admin_client, e2e_user_client, e2e_bot_username):
    """Create a test supergroup, add bot, promote to admin, return ID"""
    from telethon.tl.functions.channels import (
        CreateChannelRequest, EditAdminRequest, InviteToChannelRequest,
        DeleteChannelRequest
    )
    from telethon.tl.functions.messages import ExportChatInviteRequest, ImportChatInviteRequest
    from telethon.tl.types import ChatAdminRights
    from telethon import utils
    import time
    import asyncio

    import logging
    logging.basicConfig(level=logging.INFO, force=True)
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)

    print("[FIXTURE] e2e_test_group_id: Starting fixture execution")

    # 1. Admin creates PRIVATE supergroup (megagroup=True)
    # We use admin client to create so they are the owner
    # Private groups with invite links are the most reliable method on test servers
    title = f"Test Group {int(time.time())}"
    print(f"[FIXTURE] Creating PRIVATE test group: {title}")
    logger.info(f"[FIXTURE] Creating PRIVATE test group: {title}")

    result = await e2e_admin_client(CreateChannelRequest(
        title=title,
        about="E2E Test Group for Staring Misaka",
        megagroup=True
    ))
    chat = result.chats[0]
    chat_id = utils.get_peer_id(chat)
    logger.info(f"[FIXTURE] Created group with ID: {chat_id}")

    try:
        # 2. Add bot to group
        bot_entity = await e2e_admin_client.get_input_entity(e2e_bot_username)
        logger.info(f"[FIXTURE] Adding bot {e2e_bot_username} to group {chat_id}")
        invite_result = await e2e_admin_client(InviteToChannelRequest(
            channel=chat,
            users=[bot_entity]
        ))
        logger.info(f"[FIXTURE] Bot added to group, result: {invite_result}")
        
        # 3. Promote bot to admin
        rights = ChatAdminRights(
            change_info=True,
            post_messages=True,
            edit_messages=True,
            delete_messages=True,
            ban_users=True,
            invite_users=True,
            pin_messages=True,
            add_admins=False,
            anonymous=False,
            manage_call=False,
            other=True 
        )
        
        logger.info(f"[FIXTURE] Promoting bot to admin in group {chat_id}")
        await e2e_admin_client(EditAdminRequest(
            channel=chat,
            user_id=bot_entity,
            admin_rights=rights,
            rank="Bot Admin"
        ))
        logger.info(f"[FIXTURE] Bot promoted to admin")

        # 4. Add user to the group via invite link
        # This is the most reliable method on test servers
        from telethon.tl.functions.messages import ExportChatInviteRequest, ImportChatInviteRequest

        print("[FIXTURE] Adding user to group...")
        admin_me = await e2e_admin_client.get_me()
        user_me = await e2e_user_client.get_me()
        print(f"[FIXTURE] Admin: {admin_me.id}, User: {user_me.id}")
        logger.info(f"[FIXTURE] Admin: {admin_me.id}, User: {user_me.id}")

        if admin_me.id != user_me.id:
            # Retry join with fresh invite links if expired
            from telethon.errors import InviteHashExpiredError
            max_retries = 3
            retry_count = 0

            while retry_count < max_retries:
                try:
                    # Generate fresh invite link
                    logger.info(f"[FIXTURE] Generating invite link for group {chat_id} (attempt {retry_count + 1}/{max_retries})")
                    invite = await e2e_admin_client(ExportChatInviteRequest(peer=chat))
                    invite_link = invite.link
                    logger.info(f"[FIXTURE] Invite link: {invite_link}")

                    # Extract hash from link
                    hash_token = invite_link.split('/')[-1].replace('+', '')
                    logger.info(f"[FIXTURE] Hash token: {hash_token}")

                    # User joins via invite link
                    logger.info(f"[FIXTURE] User {user_me.id} joining via invite link")
                    result = await e2e_user_client(ImportChatInviteRequest(hash=hash_token))
                    logger.info(f"[FIXTURE] User {user_me.id} joined via invite link successfully")
                    logger.info(f"[FIXTURE] Join result: {result}")

                    # Verify the join worked
                    await asyncio.sleep(0.5)
                    participants = await e2e_admin_client.get_participants(chat, limit=100)
                    user_ids = [p.id for p in participants]
                    logger.info(f"[FIXTURE] Group now has {len(participants)} participants: {user_ids}")
                    if user_me.id in user_ids:
                        logger.info(f"[FIXTURE] ✓ User {user_me.id} successfully joined")
                        break  # Success!
                    else:
                        logger.error(f"[FIXTURE] ✗ User {user_me.id} NOT in group after join!")
                        retry_count += 1
                except InviteHashExpiredError as e:
                    logger.warning(f"[FIXTURE] Invite link expired (attempt {retry_count + 1}/{max_retries}): {e}")
                    retry_count += 1
                    if retry_count >= max_retries:
                        logger.error(f"[FIXTURE] Failed to join after {max_retries} attempts")
                        raise
                    await asyncio.sleep(0.5)  # Brief pause before retry
                except Exception as e:
                    logger.error(f"[FIXTURE] User join failed: {e}", exc_info=True)
                    raise

        logger.info(f"[FIXTURE] Yielding group ID {chat_id} to test")
        yield chat_id

    finally:
        # Cleanup: Delete group
        import time
        start_time = time.time()
        logger.info(f"[FIXTURE TEARDOWN] Starting group deletion for {chat_id}")
        try:
            await e2e_admin_client(DeleteChannelRequest(channel=chat))
            elapsed = time.time() - start_time
            logger.info(f"[FIXTURE TEARDOWN] ✓ Group {chat_id} deleted in {elapsed:.2f}s")
        except Exception as e:
            elapsed = time.time() - start_time
            logger.warning(f"[FIXTURE TEARDOWN] Failed to delete group {chat_id} after {elapsed:.2f}s: {e}")


@pytest.fixture(scope="session")
def mock_llm_e2e():
    """Mock LLM with controllable spam detection for E2E tests

    Usage in tests:
        mock_llm_e2e.is_spam.return_value = True  # Detect as spam
        mock_llm_e2e.is_spam.return_value = False  # Detect as non-spam
    """
    mock = AsyncMock()
    mock.is_spam = AsyncMock(return_value=False)
    return mock


@pytest_asyncio.fixture(scope="session")
async def e2e_session_factory(tmp_path_factory):
    """Create a session factory for E2E tests with isolated database"""
    from db import make_session_factory, Base, AdminSettings

    # Create a temporary database for this test
    db_dir = tmp_path_factory.mktemp("e2e_db")
    db_path = str(db_dir / "e2e_test.db")

    # Create a minimal config object with just db_path
    class E2EConfig:
        def __init__(self, db_path):
            self.db_path = db_path

    config = E2EConfig(db_path)
    factory = make_session_factory(config)

    # Create all tables
    Base.metadata.create_all(factory().bind)

    # Add default admin settings
    with factory() as session:
        admin_settings = AdminSettings(require_approval=False)
        session.add(admin_settings)
        session.commit()

    yield factory

    # Cleanup
    if os.path.exists(db_path):
        os.unlink(db_path)


@pytest_asyncio.fixture(scope="function")
async def e2e_world(
    e2e_bot_env,
    e2e_user_client,
    e2e_admin_client,
    e2e_test_group_id,
    e2e_session_factory,
):
    """Fully wired test world for a single group.

    This fixture provides a single object that bundles:
    - All Telegram clients (user, admin, bot)
    - Database session factory
    - Bot configuration
    - Queue processor
    - Helper methods for checking state and synchronizing on events

    Use this instead of accessing multiple fixtures separately.
    """
    from tests.e2e_world import E2EWorld

    bot = e2e_bot_env["bot"]
    queue_processor = e2e_bot_env["queue_processor"]
    config = e2e_bot_env["config"]

    world = E2EWorld(
        user_client=e2e_user_client,
        admin_client=e2e_admin_client,
        bot_client=bot,
        session_factory=e2e_session_factory,
        config=config,
        group_id=e2e_test_group_id,
        queue_processor=queue_processor,
    )

    # Initial consistency checks: make sure bot is admin, etc.
    await world.assert_bot_is_admin()

    return world