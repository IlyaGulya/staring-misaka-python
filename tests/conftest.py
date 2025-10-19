import os
import tempfile
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config import Config
from db import Base, MessageQueue, NewUser, AdminSettings


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


async def wait_for_condition(condition_fn, timeout=5.0, poll_interval=0.01, description="condition"):
    """Wait for a condition to become true, with timeout.

    Args:
        condition_fn: Callable that returns True when condition is met (can be async or sync)
        timeout: Maximum time to wait in seconds
        poll_interval: How often to check the condition in seconds
        description: Description of what we're waiting for (for error messages)

    Raises:
        TimeoutError: If condition is not met within timeout

    Example:
        await wait_for_condition(
            lambda: len(processed_messages) > 0,
            timeout=2.0,
            description="messages to be processed"
        )
    """
    import inspect
    start_time = asyncio.get_event_loop().time()

    while True:
        # Check if condition is met
        if inspect.iscoroutinefunction(condition_fn):
            result = await condition_fn()
        else:
            result = condition_fn()

        if result:
            return

        # Check timeout
        elapsed = asyncio.get_event_loop().time() - start_time
        if elapsed >= timeout:
            raise TimeoutError(f"Timeout waiting for {description} after {timeout}s")

        # Wait before next check
        await asyncio.sleep(poll_interval)