import asyncio  # For main_event_loop
import datetime  # For setup_queue_test
import logging  # Add logging
from collections.abc import AsyncGenerator
from decimal import Decimal  # For setup_queue_test
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telethon import TelegramClient
from telethon.tl.types import Channel
from telethon.tl.types import User as TelegramUser

import staring_misaka.db_utils as app_db_utils
import staring_misaka.web_ui as web_ui_module  # Import the module itself
from staring_misaka.action_service import ActionService
from staring_misaka.command_handlers import CommandHandlers
from staring_misaka.config import QueueSettings, Settings

# Import necessary items for db_engine fixture
from staring_misaka.db_models import (  # FIX: Add LLMModel and Prompt
    BannedUser,  # Added for assert_user_banned helper
    Base,
    GlobalBotSettings,
    LLMModel,
    ModelPricing,  # Added for setup_queue_test
    MonitoredGroup,
    NewUser,
    Prompt,
)
from staring_misaka.db_utils import init_db as actual_init_db
from staring_misaka.db_utils import initialize_default_data as actual_initialize_default_data
from staring_misaka.dto import LLMSpamAnalysisResult  # For mock_llm_service_spam/non_spam
from staring_misaka.event_handlers import EventHandlers
from staring_misaka.llm_service import LLMService

# Use a separate in-memory SQLite for testing
TEST_DB_URL = "sqlite+aiosqlite:///file:memdb_test?mode=memory&uri=true"

# Test constants
TEST_SUPER_ADMIN_ID = 1
TEST_GROUP_ADMIN_ID = 2
TEST_REGULAR_USER_ID = 3
TEST_NEW_USER_ID = 4
TEST_BOT_ID = 123456789
TEST_CHAT_ID = -1001234567890
TEST_CHAT_ID_2 = -1009876543210
EXAMPLE_MESSAGE_ID = 1000 # Generic message ID base if needed

# Message text constants
SPAM_MESSAGE_TEXT = "Check out my amazing site! www.spam.com"
NON_SPAM_MESSAGE_TEXT = "Hello everyone, interesting topic!"


# Configure logging for tests
LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'

# 1. Set a default log level for the root logger.
# This affects all libraries (telethon, sqlalchemy, etc.) unless overridden.
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)

# 2. Set DEBUG level specifically for our application's modules.
logging.getLogger("staring_misaka").setLevel(logging.DEBUG)

# 3. Set DEBUG level for all test modules (under the 'tests' namespace).
# This assumes test files use `logging.getLogger(__name__)`.
logging.getLogger("tests").setLevel(logging.DEBUG)

# Logger for this conftest.py file. Uses __name__, so it becomes "tests.conftest".
# It will inherit the DEBUG level from the "tests" logger.
test_logger = logging.getLogger(__name__)

# Log the logging configuration status.
test_logger.info(
    "Test logging configured. Root logger level: INFO. "
    "'staring_misaka' modules log at: DEBUG. "
    "'tests' modules (e.g., tests.conftest, tests.integration.*) log at: DEBUG."
)


@pytest.fixture
def test_settings() -> Settings:
    """Override settings for testing."""
    test_logger.info("Creating test settings...")
    return Settings(
        api_id=12345, api_hash="test_hash", bot_token="test_token", admin_id=TEST_SUPER_ADMIN_ID,
        db_url=TEST_DB_URL,
        anthropic_api_key="test_anthropic_key",  # Provide dummy keys
        openai_api_key="test_openai_key",
        bot_session_path=":memory:",  # Use in-memory session for tests
        prometheus_port=8001,  # Different port
        log_level="DEBUG",  # This setting in Settings object is for app runtime, test logging is configured above
        queue=QueueSettings(processing_interval_seconds=0.1, batch_size=2, max_automatic_retries=1)
        # Faster queue for tests
    )


@pytest_asyncio.fixture
async def db_engine(test_settings):
    """
    Initializes db_utils for the application code, creates the test database engine,
    tables, and runs initial data seeding once per session.
    """
    test_logger.info("Setting up DB engine for session...")
    actual_init_db(test_settings)  # Initializes global engine and AsyncSessionFactory in app_db_utils
    test_logger.info(f"DB Engine created: {app_db_utils.engine}")
    async with app_db_utils.engine.begin() as conn:
        test_logger.info("Dropping all tables...")
        await conn.run_sync(Base.metadata.drop_all)  # Clean start for test session
        test_logger.info("Creating all tables...")
        await conn.run_sync(Base.metadata.create_all)
    test_logger.info("Initializing default data...")
    await actual_initialize_default_data(test_settings)
    test_logger.info("DB engine setup complete.")
    yield app_db_utils.engine  # Yield the correctly initialized engine
    test_logger.info("Disposing DB engine for session...")
    await app_db_utils.engine.dispose()  # Dispose the correct engine
    test_logger.info("DB engine disposed.")


@pytest_asyncio.fixture
async def db_session(db_engine, request) -> AsyncGenerator[AsyncSession, None]:
    """Provides a transactional session per test function that is always rolled back."""
    test_name = request.node.name
    test_logger.debug(f"[{test_name}] Setting up DB session...")
    connection = await db_engine.connect()
    test_logger.debug(f"[{test_name}] Connection established: {connection}")
    transaction = await connection.begin()
    test_logger.debug(f"[{test_name}] Top-level transaction started: {transaction}")

    TestSessionLocal = async_sessionmaker(
        bind=connection, expire_on_commit=False, class_=AsyncSession
    )

    async with TestSessionLocal() as session:
        test_logger.debug(f"[{test_name}] Test session created: {session}")
        gs = await session.get(GlobalBotSettings, 1)
        assert gs is not None, "GlobalBotSettings should be initialized by db_engine fixture"
        test_logger.debug(f"[{test_name}] Yielding session...")
        yield session
        test_logger.debug(f"[{test_name}] Test function finished. Flushing session state...")
        if session.is_active:
            try:
                await session.flush()
                test_logger.debug(f"[{test_name}] Session flushed.")
            except Exception as e:
                test_logger.warning(f"[{test_name}] Exception during session flush in teardown: {e}")

    test_logger.debug(f"[{test_name}] Rolling back transaction: {transaction}")
    if transaction.is_active:
        await transaction.rollback()
        test_logger.debug(f"[{test_name}] Transaction rolled back.")
    else:
        test_logger.warning(f"[{test_name}] Transaction was not active during rollback.")
    test_logger.debug(f"[{test_name}] Closing connection: {connection}")
    await connection.close()
    test_logger.debug(f"[{test_name}] DB session teardown complete.")


@pytest.fixture
def mock_telegram_client(test_settings) -> MagicMock:
    """Provides a mocked TelegramClient instance."""
    test_logger.debug("Creating mock Telegram client...")
    client = MagicMock(spec=TelegramClient)
    client.api_id = test_settings.api_id
    client.api_hash = test_settings.api_hash
    client.session = MagicMock()
    client.session.filename = test_settings.bot_session_path
    client.start = AsyncMock(return_value=client)
    client.run_until_disconnected = AsyncMock()
    client.disconnect = AsyncMock()
    client.is_connected = MagicMock(return_value=True)  # Assume connected during tests
    client.get_me = AsyncMock(return_value=MagicMock(id=TEST_BOT_ID, bot=True, username="TestBot"))

    client.sent_messages_log = []

    async def mock_send_message(*args, **kwargs):
        """Mock send_message that logs calls and returns a mock message with an ID."""
        test_logger.debug(f"Mock send_message called with args: {args}, kwargs: {kwargs}")
        client.sent_messages_log.append({"args": args, "kwargs": kwargs})
        # Handle reply_to being None or an int to avoid TypeError
        reply_to_val = kwargs.get("reply_to")
        current_id_base = reply_to_val if isinstance(reply_to_val, int) else 0
        # Generate a somewhat unique ID for mock messages to help differentiate them
        mock_message_id = current_id_base + 999 + len(client.sent_messages_log)
        mock_msg_obj = MagicMock(id=mock_message_id)
        test_logger.debug(f"Mock send_message returning mock message object with id: {mock_message_id}")
        return mock_msg_obj

    client.send_message = AsyncMock(side_effect=mock_send_message)

    client.kick_participant = AsyncMock()
    client.delete_messages = AsyncMock()
    client.edit_permissions = AsyncMock()

    async def mock_get_entity(entity_id):
        if entity_id == TEST_SUPER_ADMIN_ID: return MagicMock(spec=TelegramUser, id=TEST_SUPER_ADMIN_ID,
                                                              username="SuperAdmin", first_name="Super",
                                                              last_name="Admin", bot=False)
        if entity_id == TEST_GROUP_ADMIN_ID: return MagicMock(spec=TelegramUser, id=TEST_GROUP_ADMIN_ID,
                                                              username="GroupAdmin", first_name="Group",
                                                              last_name="Admin", bot=False)
        if entity_id == TEST_REGULAR_USER_ID: return MagicMock(spec=TelegramUser, id=TEST_REGULAR_USER_ID,
                                                               username="RegUser", first_name="Reg", last_name="User",
                                                               bot=False)
        if entity_id == TEST_NEW_USER_ID: return MagicMock(spec=TelegramUser, id=TEST_NEW_USER_ID, username="NewUser",
                                                           first_name="New", last_name="User", bot=False)
        if entity_id in (TEST_CHAT_ID, TEST_CHAT_ID_2): return MagicMock(spec=Channel, id=entity_id,
                                                                         title=f"Test Group {entity_id}",
                                                                         username=f"testgroup_{abs(entity_id)}")
        # Allow mocking for other user IDs dynamically if needed by tests (e.g. queue tests)
        if isinstance(entity_id, int) and entity_id > TEST_NEW_USER_ID : # For dynamically created users in tests
            return MagicMock(spec=TelegramUser, id=entity_id, username=f"DynamicUser{entity_id}",
                             first_name="Dynamic", last_name=f"User{entity_id}", bot=False)
        return None

    client.get_entity = AsyncMock(side_effect=mock_get_entity)

    async def mock_iter_participants(chat_id, *args, filter=None, **kwargs):
        if filter and filter.__name__ == 'ChannelParticipantsAdmins':
            if chat_id == TEST_CHAT_ID:
                yield MagicMock(spec=TelegramUser, id=TEST_GROUP_ADMIN_ID, is_admin=True)
            elif chat_id == TEST_CHAT_ID_2:  # Ensure this user is admin for TEST_CHAT_ID_2
                yield MagicMock(spec=TelegramUser, id=TEST_SUPER_ADMIN_ID,
                                is_admin=True)  # Super admin can also be group admin
                yield MagicMock(spec=TelegramUser, id=TEST_GROUP_ADMIN_ID, is_admin=True)
        if False: yield  # To make it an async generator

    client.iter_participants = mock_iter_participants
    client.on = MagicMock()
    client.add_event_handler = MagicMock()
    return client


@pytest.fixture
def mock_llm_service(test_settings, mock_telegram_client) -> MagicMock:
    """
    Provides a fully mocked LLMService. Useful for tests not focusing on LLMService internal logic.
    """
    test_logger.debug("Creating mock LLM service...")
    mock_service = MagicMock(spec=LLMService)
    default_result = LLMSpamAnalysisResult( # Use DTO for default result
        is_spam=False,
        reason="Looks okay.",
        input_tokens=10,
        output_tokens=2,
        model_name_used="mock-model-v1",
        status="success",
        error_message=None
    )
    mock_service.analyze_message_for_spam = AsyncMock(return_value=default_result)
    mock_service.reprocess_queued_item = AsyncMock(return_value=True)
    mock_service.process_llm_queue_batch = AsyncMock(return_value=0)
    # Add the _get_active_prompt_and_model method to the mock spec if it's called by test code
    # This helps if tests directly or indirectly (via other mocked methods) call this.
    mock_service._get_active_prompt_and_model = AsyncMock(
        return_value=(MagicMock(id=1), MagicMock(id=1, provider="Anthropic", api_identifier="test-model"))
    )
    return mock_service

@pytest.fixture
def mock_llm_service_spam(test_settings, mock_telegram_client) -> MagicMock:
    """Provides a mocked LLMService that always returns 'spam'."""
    test_logger.debug("Creating mock LLM service (always spam)...")
    mock_service = MagicMock(spec=LLMService)
    spam_result = LLMSpamAnalysisResult(
        is_spam=True, reason="Mocked: Spam detected",
        input_tokens=20, output_tokens=5, model_name_used="mock-spam-model-v1", status="success"
    )
    mock_service.analyze_message_for_spam = AsyncMock(return_value=spam_result)
    # Mock other methods if needed by tests using this fixture
    mock_service.reprocess_queued_item = AsyncMock(return_value=True)
    mock_service.process_llm_queue_batch = AsyncMock(return_value=0)
    return mock_service

@pytest.fixture
def mock_llm_service_non_spam(test_settings, mock_telegram_client) -> MagicMock:
    """Provides a mocked LLMService that always returns 'not spam'."""
    test_logger.debug("Creating mock LLM service (always not spam)...")
    mock_service = MagicMock(spec=LLMService)
    non_spam_result = LLMSpamAnalysisResult(
        is_spam=False, reason="Mocked: Looks okay",
        input_tokens=15, output_tokens=3, model_name_used="mock-nonspam-model-v1", status="success"
    )
    mock_service.analyze_message_for_spam = AsyncMock(return_value=non_spam_result)
    mock_service.reprocess_queued_item = AsyncMock(return_value=True)
    mock_service.process_llm_queue_batch = AsyncMock(return_value=0)
    return mock_service


@pytest.fixture
def real_llm_service(test_settings, mock_telegram_client) -> LLMService:
    """
    Provides a REAL LLMService instance. The underlying API calls will need mocking in tests.
    """
    test_logger.debug("Creating REAL LLM service instance...")
    # Note: The strategies might fail initialization if API keys are invalid/missing,
    # which might be desired for some tests (like test_llm_failure_queues_check_and_notifies_admin).
    # Tests expecting successful API interaction will need to mock the strategy's 'analyze' method.
    return LLMService(test_settings, mock_telegram_client)


@pytest.fixture
def action_service(test_settings, mock_telegram_client) -> ActionService:
    """Provides an ActionService instance with mocked client."""
    test_logger.debug("Creating Action service...")
    return ActionService(settings=test_settings, client=mock_telegram_client)


@pytest.fixture
def event_handlers(test_settings, mock_telegram_client, real_llm_service, action_service) -> EventHandlers:
    """
    Provides an EventHandlers instance using the REAL LLMService.
    API calls within LLMService will need mocking at the strategy level in specific tests.
    For tests needing a fully mocked LLMService for EventHandlers, construct EventHandlers locally in the test.
    """
    test_logger.debug("Creating Event Handlers with REAL LLM Service...")
    handlers = EventHandlers(
        settings=test_settings,
        client=mock_telegram_client,
        llm_service=real_llm_service,  # Use the real service
        action_service=action_service
    )
    handlers.update_monitored_chats_cache = AsyncMock()  # Mock this to control cache state in tests
    return handlers


@pytest.fixture
def command_handlers(test_settings, mock_telegram_client, action_service, event_handlers,
                     real_llm_service) -> CommandHandlers:  # Use real_llm_service here too
    """Provides a CommandHandlers instance with mocks and REAL LLM Service."""
    test_logger.debug("Creating Command Handlers with REAL LLM Service...")
    handlers = CommandHandlers(
        settings=test_settings,
        client=mock_telegram_client,
        action_service=action_service,
        event_handlers_ref=event_handlers,
        llm_service=real_llm_service  # Use the real service
    )
    return handlers


# --- Helper Fixtures ---

@pytest_asyncio.fixture
async def monitored_group(db_session, test_settings, request) -> MonitoredGroup: # Return MonitoredGroup
    """Ensures the default test group exists in the DB for a test and returns it."""
    test_name = request.node.name
    test_logger.debug(f"[{test_name}] Ensuring monitored group {TEST_CHAT_ID} exists...")
    group = await db_session.get(MonitoredGroup, TEST_CHAT_ID)
    if not group:
        test_logger.debug(f"[{test_name}] Monitored group {TEST_CHAT_ID} not found, creating...")
        group = MonitoredGroup(
            chat_id=TEST_CHAT_ID,
            added_by_user_id=TEST_SUPER_ADMIN_ID,
            require_admin_approval_for_ban=False,  # Default for some tests
            pre_ban_message_enabled=True,
            delete_recent_messages_on_ban=True,
            num_messages_to_delete_on_ban=1
        )
        db_session.add(group)
        await db_session.flush()  # FIX: Changed from commit to flush for test isolation
        test_logger.debug(f"[{test_name}] Monitored group {TEST_CHAT_ID} created and flushed.")
    else:
        test_logger.debug(f"[{test_name}] Monitored group {TEST_CHAT_ID} already exists.")
    yield group # Yield the group object
    test_logger.debug(f"[{test_name}] Teardown for monitored_group fixture.")
    # Cleanup is handled by db_session rollback

@pytest_asyncio.fixture
async def setup_monitored_group_with_config(db_session, test_settings, request,
                                            require_admin_approval: bool = True) -> MonitoredGroup:
    """Creates a monitored group with specific admin approval configuration."""
    test_name = request.node.name
    test_logger.debug(
        f"[{test_name}] Setting up monitored group {TEST_CHAT_ID} with require_admin_approval={require_admin_approval}")
    group = await db_session.get(MonitoredGroup, TEST_CHAT_ID)
    if group: # If it exists, update it
        group.require_admin_approval_for_ban = require_admin_approval
    else: # If not, create it
        group = MonitoredGroup(
            chat_id=TEST_CHAT_ID,
            added_by_user_id=TEST_SUPER_ADMIN_ID,
            require_admin_approval_for_ban=require_admin_approval,
            pre_ban_message_enabled=True, # Default sensible values
            delete_recent_messages_on_ban=True,
            num_messages_to_delete_on_ban=1
        )
        db_session.add(group)
    await db_session.flush()
    test_logger.debug(f"[{test_name}] Monitored group {TEST_CHAT_ID} configured and flushed.")
    yield group
    test_logger.debug(f"[{test_name}] Teardown for setup_monitored_group_with_config fixture.")


@pytest_asyncio.fixture
async def new_user_in_group(db_session, monitored_group, request) -> NewUser: # Return NewUser
    """Ensures the test new user exists in the NewUser table for the default group and returns it."""
    test_name = request.node.name
    user_key = {"user_id": TEST_NEW_USER_ID, "chat_id": TEST_CHAT_ID}  # Use dict for composite PK lookup
    test_logger.debug(f"[{test_name}] Ensuring new user {user_key} exists...")
    user = await db_session.get(NewUser, user_key)
    if not user:
        test_logger.debug(f"[{test_name}] New user {user_key} not found, creating...")
        user = NewUser(user_id=TEST_NEW_USER_ID, chat_id=TEST_CHAT_ID)
        db_session.add(user)
        await db_session.flush()  # FIX: Changed from commit to flush for test isolation
        test_logger.debug(f"[{test_name}] New user {user_key} created and flushed.")
    else:
        test_logger.debug(f"[{test_name}] New user {user_key} already exists.")
    yield user # Yield the user object
    test_logger.debug(f"[{test_name}] Teardown for new_user_in_group fixture.")
    # Cleanup is handled by db_session rollback


# Fixture from test_queue_flow, needed for test_spam_flow now
@pytest_asyncio.fixture
async def setup_queue_test(db_session, monitored_group, new_user_in_group):
    """Common setup for queue/spam tests: ensures group, user, prompt, model exist."""
    test_logger.debug("Setting up queue/spam test data (prompt/model)...")
    gs = await db_session.get(GlobalBotSettings, 1)
    assert gs is not None

    # --- Prompt Setup ---
    current_default_prompt_id_in_gs = gs.default_prompt_id
    # For tests, we often want a fresh, known prompt.
    test_prompt_name = f"TestFixturePrompt_{id(db_session)}"
    test_prompt_obj = await db_session.scalar(select(Prompt).where(Prompt.name == test_prompt_name))

    if not test_prompt_obj:
        test_prompt_obj = Prompt(name=test_prompt_name, text="Test Prompt Text: {message_text}",
                                 is_global_default=False)
        db_session.add(test_prompt_obj)
        await db_session.flush()  # Get ID for test_prompt_obj

    # Unset old default prompt's flag if it exists and is different
    if gs.default_prompt_id and gs.default_prompt_id != test_prompt_obj.id:
        old_default_prompt = await db_session.get(Prompt, gs.default_prompt_id)
        if old_default_prompt:
            old_default_prompt.is_global_default = False

    test_prompt_obj.is_global_default = True
    gs.default_prompt_id = test_prompt_obj.id
    prompt = test_prompt_obj
    test_logger.debug(
        f"Using/Set prompt for test: ID={prompt.id}, Name={prompt.name}, GS default_prompt_id now {gs.default_prompt_id}")

    # --- Model Setup ---
    current_default_model_id_in_gs = gs.default_model_id
    test_model_name = f"TestFixtureModel_{id(db_session)}"
    test_model_obj = await db_session.scalar(select(LLMModel).where(LLMModel.name == test_model_name))
    if not test_model_obj:
        test_model_obj = LLMModel(name=test_model_name, api_identifier="test-fixture-model",
                                  provider="Anthropic")  # Default to Anthropic
        db_session.add(test_model_obj)
        await db_session.flush()  # Get ID

    gs.default_model_id = test_model_obj.id
    model = test_model_obj
    test_logger.debug(
        f"Using/Set model for test: ID={model.id}, Name={model.name}, GS default_model_id now {gs.default_model_id}")

    # Add default pricing for the model used in the test
    today = datetime.date.today()
    existing_pricing = await db_session.scalar(
        select(ModelPricing)
        .where(ModelPricing.model_id == model.id)
        .where(ModelPricing.effective_from_date <= today)
        .where((ModelPricing.effective_to_date.is_(None)) | (ModelPricing.effective_to_date >= today))
    )
    if not existing_pricing:
        default_pricing = ModelPricing(
            model_id=model.id,
            input_price_per_million_tokens=Decimal("0.25"),
            output_price_per_million_tokens=Decimal("1.25"),
            currency="USD",
            effective_from_date=today - datetime.timedelta(days=1),  # Ensure it's active
            effective_to_date=None
        )
        db_session.add(default_pricing)
        test_logger.debug(f"Added default pricing for model ID {model.id} for test setup.")

    await db_session.flush()
    test_logger.debug(f"Setup complete. Yielding prompt (ID={prompt.id}) and model (ID={model.id})")
    yield prompt, model


@pytest_asyncio.fixture(autouse=True)  # Autouse to ensure it runs for all tests in modules using it
async def setup_web_ui_globals(
        test_settings: Settings,
        real_llm_service: LLMService,  # Or mock_llm_service if preferred for some tests
        action_service: ActionService
):
    """
    Sets up the global variables in the web_ui module that its handlers rely on.
    This runs for each test function to ensure a clean state.
    """
    web_ui_module._app_settings = test_settings
    try:
        web_ui_module._main_event_loop = asyncio.get_running_loop()
    except RuntimeError:  # If no loop is running yet (e.g. during pytest collection)
        web_ui_module._main_event_loop = asyncio.new_event_loop()  # Fallback, might need refinement if problematic
        asyncio.set_event_loop(web_ui_module._main_event_loop)

    web_ui_module._llm_service_instance = real_llm_service
    web_ui_module._action_service_instance = action_service

    yield  # Test runs here

    # Teardown (optional, but good practice to clear them)
    web_ui_module._app_settings = None
    web_ui_module._main_event_loop = None
    web_ui_module._llm_service_instance = None
    web_ui_module._action_service_instance = None

# --- Assertion Helpers ---
async def assert_user_banned_with_details(
    db_session: AsyncSession,
    client: MagicMock,
    user_id: int,
    chat_id: int,
    expected_reason_substring: str,
    expected_bot_id: int,
    expected_deleted_message_ids: list[int] | None = None,
):
    """Asserts that a user is banned, with checks for DB record, Telegram calls, and NewUser removal."""
    db_session.expire_all() # Ensure we read fresh data after handler's commit

    banned_user_record = await db_session.scalar(
        select(BannedUser).where(BannedUser.user_id == user_id, BannedUser.chat_id == chat_id)
    )
    assert banned_user_record is not None, f"BannedUser record for user {user_id} in chat {chat_id} not found"
    assert banned_user_record.banned_by_user_id == expected_bot_id, "Banned by user ID mismatch"
    assert expected_reason_substring in banned_user_record.reason, f"Expected reason substring '{expected_reason_substring}' not in '{banned_user_record.reason}'"

    client.kick_participant.assert_called_once_with(chat_id, user_id)

    if expected_deleted_message_ids:
        # Sort both lists to ensure order doesn't affect assertion
        actual_deleted_ids_args = client.delete_messages.call_args
        assert actual_deleted_ids_args is not None, "delete_messages was not called"
        actual_deleted_ids = sorted(actual_deleted_ids_args[0][1])
        expected_sorted_ids = sorted(expected_deleted_message_ids)
        client.delete_messages.assert_called_once_with(chat_id, expected_sorted_ids)
    else:
        client.delete_messages.assert_not_called()

    new_user_check = await db_session.get(NewUser, {"user_id": user_id, "chat_id": chat_id})
    assert new_user_check is None, "NewUser record was not deleted after ban"

async def assert_user_approved(
    db_session: AsyncSession,
    user_id: int,
    chat_id: int,
):
    """Asserts that a user is approved (not banned, removed from NewUser)."""
    db_session.expire_all() # Ensure we read fresh data after handler's commit

    new_user_record = await db_session.get(NewUser, {"user_id": user_id, "chat_id": chat_id})
    assert new_user_record is None, "NewUser record was not deleted after approval"

    banned_user_record = await db_session.scalar(
        select(BannedUser).where(BannedUser.user_id == user_id, BannedUser.chat_id == chat_id)
    )
    assert banned_user_record is None, "User was incorrectly banned after approval"
