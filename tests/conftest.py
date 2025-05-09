import logging  # Add logging
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telethon import TelegramClient
from telethon.tl.types import Channel
from telethon.tl.types import User as TelegramUser

import staring_misaka.db_utils as app_db_utils
from staring_misaka.action_service import ActionService
from staring_misaka.command_handlers import CommandHandlers
from staring_misaka.config import QueueSettings, Settings

# Import necessary items for db_engine fixture
from staring_misaka.db_models import (  # FIX: Add LLMModel and Prompt
    Base,
    GlobalBotSettings,
    LLMModel,
    MonitoredGroup,
    NewUser,
    Prompt,
)
from staring_misaka.db_utils import init_db as actual_init_db
from staring_misaka.db_utils import initialize_default_data as actual_initialize_default_data
from staring_misaka.event_handlers import EventHandlers
from staring_misaka.llm_service import LLMService

# Use a separate in-memory SQLite for testing
# REMOVED cache=shared to improve test isolation
TEST_DB_URL = "sqlite+aiosqlite:///file:memdb_test?mode=memory&uri=true"

# Test constants
TEST_SUPER_ADMIN_ID = 1
TEST_GROUP_ADMIN_ID = 2
TEST_REGULAR_USER_ID = 3
TEST_NEW_USER_ID = 4
TEST_BOT_ID = 123456789
TEST_CHAT_ID = -1001234567890
TEST_CHAT_ID_2 = -1009876543210

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


@pytest.fixture(scope="session")
def test_settings() -> Settings:
    """Override settings for testing."""
    test_logger.info("Creating test settings...")
    return Settings(
        API_ID=12345, API_HASH="test_hash", BOT_TOKEN="test_token", ADMIN_ID=TEST_SUPER_ADMIN_ID,
        DB_URL=TEST_DB_URL,
        ANTHROPIC_API_KEY="test_anthropic_key",  # Provide dummy keys
        OPENAI_API_KEY="test_openai_key",
        BOT_SESSION_PATH=":memory:",  # Use in-memory session for tests
        PROMETHEUS_PORT=8001,  # Different port
        LOG_LEVEL="DEBUG", # This setting in Settings object is for app runtime, test logging is configured above
        queue=QueueSettings(processing_interval_seconds=0.1, batch_size=2, max_automatic_retries=1)
        # Faster queue for tests
    )


@pytest_asyncio.fixture(scope="session")
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
    default_result = MagicMock()
    default_result.is_spam = False
    default_result.reason = "Looks okay."
    default_result.input_tokens = 10
    default_result.output_tokens = 2
    default_result.model_name_used = "mock-model-v1"
    default_result.status = "success"
    default_result.error_message = None
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
    """
    test_logger.debug("Creating Event Handlers with REAL LLM Service...")
    handlers = EventHandlers(
        settings=test_settings,
        client=mock_telegram_client,
        llm_service=real_llm_service, # Use the real service
        action_service=action_service
    )
    handlers.update_monitored_chats_cache = AsyncMock()  # Mock this to control cache state in tests
    return handlers


@pytest.fixture
def command_handlers(test_settings, mock_telegram_client, action_service, event_handlers,
                     real_llm_service) -> CommandHandlers: # Use real_llm_service here too
    """Provides a CommandHandlers instance with mocks and REAL LLM Service."""
    test_logger.debug("Creating Command Handlers with REAL LLM Service...")
    handlers = CommandHandlers(
        settings=test_settings,
        client=mock_telegram_client,
        action_service=action_service,
        event_handlers_ref=event_handlers,
        llm_service=real_llm_service # Use the real service
    )
    return handlers


# --- Helper Fixtures ---

@pytest_asyncio.fixture
async def monitored_group(db_session, test_settings, request) -> AsyncGenerator[None, None]:
    """Ensures the default test group exists in the DB for a test."""
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
    yield
    test_logger.debug(f"[{test_name}] Teardown for monitored_group fixture.")
    # Cleanup is handled by db_session rollback


@pytest_asyncio.fixture
async def new_user_in_group(db_session, monitored_group, request) -> AsyncGenerator[None, None]:
    """Ensures the test new user exists in the NewUser table for the default group."""
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
    yield
    test_logger.debug(f"[{test_name}] Teardown for new_user_in_group fixture.")
    # Cleanup is handled by db_session rollback


# Fixture from test_queue_flow, needed for test_spam_flow now
@pytest_asyncio.fixture
async def setup_queue_test(db_session, monitored_group, new_user_in_group):
    """Common setup for queue/spam tests: ensures group, user, prompt, model exist."""
    test_logger.debug("Setting up queue/spam test data (prompt/model)...")
    gs = await db_session.get(GlobalBotSettings, 1)
    assert gs is not None

    prompt = await db_session.get(Prompt, gs.default_prompt_id) if gs.default_prompt_id else None
    if not prompt:
        prompt_name = f"DefaultQueueTestPrompt_{id(db_session)}"
        prompt_id_to_use = gs.default_prompt_id if gs.default_prompt_id else 1 # Use ID 1 if not set
        existing_prompt_with_id = await db_session.get(Prompt, prompt_id_to_use)
        if existing_prompt_with_id and existing_prompt_with_id.name != prompt_name:
             # If ID 1 exists with a different name, try ID 100 (basic collision avoidance)
             prompt_id_to_use = 100
             existing_prompt_with_id = await db_session.get(Prompt, prompt_id_to_use)
             if existing_prompt_with_id: prompt_id_to_use += 1

        prompt = Prompt(id=prompt_id_to_use, name=prompt_name, text="Test: {message_text}", is_global_default=False)
        if not gs.default_prompt_id:
            prompt.is_global_default = True
        db_session.add(prompt)
        await db_session.flush()
        if not gs.default_prompt_id: gs.default_prompt_id = prompt.id
        test_logger.debug(f"Created/set prompt: ID={prompt.id}, Name={prompt.name}")

    model = await db_session.get(LLMModel, gs.default_model_id) if gs.default_model_id else None
    if not model:
        model_name = f"DefaultQueueTestModel_{id(db_session)}"
        model_id_to_use = gs.default_model_id if gs.default_model_id else 1 # Use ID 1 if not set
        existing_model_with_id = await db_session.get(LLMModel, model_id_to_use)
        if existing_model_with_id and existing_model_with_id.name != model_name:
            model_id_to_use = 100 # Basic collision avoidance
            existing_model_with_id = await db_session.get(LLMModel, model_id_to_use)
            if existing_model_with_id: model_id_to_use += 1

        # Default to Anthropic provider as the mock client in tests often targets this
        model = LLMModel(id=model_id_to_use, name=model_name, api_identifier="test-m-queue", provider="Anthropic")
        db_session.add(model)
        await db_session.flush()
        if not gs.default_model_id: gs.default_model_id = model.id
        test_logger.debug(f"Created/set model: ID={model.id}, Name={model.name}")

    await db_session.flush()
    test_logger.debug(f"Setup complete. Yielding prompt (ID={prompt.id}) and model (ID={model.id})")
    yield prompt, model
