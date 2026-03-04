import pytest
import asyncio
import tempfile
import os
from datetime import datetime, UTC
from unittest.mock import AsyncMock

from queue_processor import QueueProcessor
from db import MessageQueue, NewUser, create_session, AdminSettings
from config import Config
from tests.conftest import wait_for_condition


class TestQueueProcessorConcurrency:
    """Test QueueProcessor concurrency with SQLite row locks"""

    @pytest.fixture
    def shared_db_config(self):
        """Create a config with shared temporary database"""
        with tempfile.NamedTemporaryFile(delete=False, suffix='.db') as tmp_file:
            db_path = tmp_file.name

        config = Config.for_testing(db_path=db_path)
        yield config

        # Cleanup
        if os.path.exists(db_path):
            os.unlink(db_path)

    @pytest.fixture
    def setup_shared_data(self, shared_db_config):
        """Setup shared data in the database"""
        from db import make_session_factory, initialize_database, Base

        # Create session factory and initialize database
        session_factory = make_session_factory(shared_db_config)
        initialize_database(session_factory, shared_db_config)

        # Create a session to add test data
        session = session_factory()

        # Add test messages to queue
        messages = []
        for i in range(5):
            msg = MessageQueue(
                user_id=1000 + i,
                chat_id=shared_db_config.tracking_chat_ids[0],
                message_id=2000 + i,
                message_text=f"Test message {i}",
                status='pending'
            )
            messages.append(msg)
            session.add(msg)

        # Add corresponding NewUser entries
        for i in range(5):
            new_user = NewUser(
                user_id=1000 + i,
                chat_id=shared_db_config.tracking_chat_ids[0]
            )
            session.add(new_user)

        session.commit()
        session.close()
        return messages

    @pytest.mark.asyncio
    async def test_concurrent_processors_process_different_messages(self, shared_db_config, setup_shared_data):
        """Test that concurrent processors process different messages (row locking)"""
        # Create sessionmaker
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy import create_engine
        engine = create_engine(f'sqlite:///{shared_db_config.db_path}', echo=False)
        SessionFactory = sessionmaker(bind=engine, expire_on_commit=False)

        # Mock LLM and other dependencies
        mock_llm1 = AsyncMock()
        mock_llm1.is_spam.return_value = False
        mock_llm2 = AsyncMock()
        mock_llm2.is_spam.return_value = False

        mock_userbot1 = AsyncMock()
        mock_userbot2 = AsyncMock()
        mock_telegram_client1 = AsyncMock()
        mock_telegram_client2 = AsyncMock()

        # Create two processors with faster processing delay
        processor1 = QueueProcessor(SessionFactory, mock_llm1, mock_userbot1, mock_telegram_client1, shared_db_config, processing_delay=0.01)
        processor2 = QueueProcessor(SessionFactory, mock_llm2, mock_userbot2, mock_telegram_client2, shared_db_config, processing_delay=0.01)

        processed_messages_1 = []
        processed_messages_2 = []

        # Track which messages each processor handles
        original_process_1 = processor1._process_message
        original_process_2 = processor2._process_message

        async def track_process_1(item_id, user_id, chat_id, message_id, message_text, retry_count):
            processed_messages_1.append(item_id)
            return await original_process_1(item_id, user_id, chat_id, message_id, message_text, retry_count)

        async def track_process_2(item_id, user_id, chat_id, message_id, message_text, retry_count):
            processed_messages_2.append(item_id)
            return await original_process_2(item_id, user_id, chat_id, message_id, message_text, retry_count)

        processor1._process_message = track_process_1
        processor2._process_message = track_process_2

        # Start both processors concurrently
        task1 = asyncio.create_task(processor1.start())
        task2 = asyncio.create_task(processor2.start())

        # Wait for messages to be processed (expect all 5 messages from setup)
        await wait_for_condition(
            lambda: len(processed_messages_1) + len(processed_messages_2) >= 5,
            timeout=2.0,
            poll_interval=0.01,
            description="all 5 messages to be processed"
        )

        # Stop both processors
        processor1.stop()
        processor2.stop()

        # Wait for tasks to complete
        await asyncio.gather(task1, task2, return_exceptions=True)

        # Verify no message was processed by both processors
        overlap = set(processed_messages_1) & set(processed_messages_2)
        assert len(overlap) == 0, f"Messages processed by both processors: {overlap}"

        # Verify all messages were processed
        total_processed = len(processed_messages_1) + len(processed_messages_2)
        assert total_processed >= 5, f"Expected at least 5 messages processed, got {total_processed}"

    @pytest.mark.asyncio
    async def test_concurrent_message_insertion_and_processing(self, shared_db_config):
        """Test concurrent message insertion while processing"""
        # Create sessionmaker with initialized tables
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy import create_engine
        from db import Base, initialize_database, make_session_factory

        # Use make_session_factory to ensure proper setup
        SessionFactory = make_session_factory(shared_db_config)
        initialize_database(SessionFactory, shared_db_config)

        session = SessionFactory()

        # Mock dependencies
        mock_llm = AsyncMock()
        mock_llm.is_spam.return_value = False
        mock_userbot = AsyncMock()
        mock_telegram_client = AsyncMock()

        processor = QueueProcessor(SessionFactory, mock_llm, mock_userbot, mock_telegram_client, shared_db_config, processing_delay=0.005)

        # Add initial NewUser for message processing
        new_user = NewUser(user_id=5000, chat_id=shared_db_config.tracking_chat_ids[0])
        session.add(new_user)
        session.commit()

        messages_added = 0
        processed_count = 0

        # Track processed messages
        original_process = processor._process_message

        async def track_process(item_id, user_id, chat_id, message_id, message_text, retry_count):
            nonlocal processed_count
            processed_count += 1
            return await original_process(item_id, user_id, chat_id, message_id, message_text, retry_count)

        processor._process_message = track_process

        # Start processor
        processor_task = asyncio.create_task(processor.start())

        # Concurrently add messages while processor is running
        async def add_messages():
            nonlocal messages_added
            for i in range(10):
                await processor.add_message_to_queue(
                    user_id=5000,
                    chat_id=shared_db_config.tracking_chat_ids[0],
                    message_id=3000 + i,
                    message_text=f"Concurrent message {i}"
                )
                messages_added += 1
                await asyncio.sleep(0.005)  # Small delay between insertions

        add_task = asyncio.create_task(add_messages())

        # Wait for all messages to be added
        await wait_for_condition(
            lambda: messages_added >= 10,
            timeout=2.0,
            poll_interval=0.01,
            description="all messages to be added to queue"
        )

        # Wait for all messages to be processed
        await wait_for_condition(
            lambda: processed_count >= 10,
            timeout=2.0,
            poll_interval=0.01,
            description="all messages to be processed"
        )

        # Stop processor
        processor.stop()
        await add_task  # Ensure add task is complete

        # Wait for processor to finish
        await asyncio.gather(processor_task, return_exceptions=True)

        # Verify messages were processed without conflicts
        assert processed_count == 10, f"Expected 10 messages processed, got {processed_count}"

        # Verify final queue state is consistent
        remaining_pending = session.query(MessageQueue).filter_by(status='pending').count()
        completed = session.query(MessageQueue).filter_by(status='completed').count()

        assert remaining_pending + completed == 10, "Message count inconsistency"

        session.close()

    @pytest.mark.asyncio
    async def test_concurrent_retry_operations(self, shared_db_config):
        """Test concurrent retry operations don't cause conflicts"""
        # Create sessionmaker with initialized tables
        from db import make_session_factory, initialize_database

        SessionFactory = make_session_factory(shared_db_config)
        initialize_database(SessionFactory, shared_db_config)

        session1 = SessionFactory()

        # Add failed messages
        failed_messages = []
        for i in range(5):
            msg = MessageQueue(
                user_id=4000 + i,
                chat_id=shared_db_config.tracking_chat_ids[0],
                message_id=5000 + i,
                message_text=f"Failed message {i}",
                status='failed',
                retry_count=2,
                error_message="Test error"
            )
            failed_messages.append(msg)
            session1.add(msg)

        session1.commit()

        # Mock dependencies for processors
        mock_llm1 = AsyncMock()
        mock_llm2 = AsyncMock()
        mock_userbot1 = AsyncMock()
        mock_userbot2 = AsyncMock()
        mock_telegram_client1 = AsyncMock()
        mock_telegram_client2 = AsyncMock()

        processor1 = QueueProcessor(SessionFactory, mock_llm1, mock_userbot1, mock_telegram_client1, shared_db_config, processing_delay=0.01)
        processor2 = QueueProcessor(SessionFactory, mock_llm2, mock_userbot2, mock_telegram_client2, shared_db_config, processing_delay=0.01)

        # Perform concurrent retry operations
        async def concurrent_retries():
            results = await asyncio.gather(
                asyncio.create_task(asyncio.to_thread(processor1.retry_failed_messages)),
                asyncio.create_task(asyncio.to_thread(processor2.retry_failed_messages)),
                return_exceptions=True
            )
            return results

        results = await concurrent_retries()

        # Both operations should succeed or at least not raise exceptions
        for result in results:
            if isinstance(result, Exception):
                pytest.fail(f"Retry operation failed: {result}")

        # Verify final state is consistent
        session3 = SessionFactory()
        pending_count = session3.query(MessageQueue).filter_by(status='pending').count()
        failed_count = session3.query(MessageQueue).filter_by(status='failed').count()

        # All messages should be reset to pending
        assert pending_count == 5
        assert failed_count == 0

        session1.close()
        session3.close()

    @pytest.mark.asyncio
    async def test_processor_graceful_shutdown_with_active_processing(self, shared_db_config):
        """Test processor graceful shutdown while actively processing messages"""
        # Create sessionmaker with initialized tables
        from db import make_session_factory, initialize_database

        SessionFactory = make_session_factory(shared_db_config)
        initialize_database(SessionFactory, shared_db_config)

        session = SessionFactory()

        # Add messages and new users
        for i in range(3):
            msg = MessageQueue(
                user_id=6000 + i,
                chat_id=shared_db_config.tracking_chat_ids[0],
                message_id=7000 + i,
                message_text=f"Shutdown test message {i}",
                status='pending'
            )
            session.add(msg)

            new_user = NewUser(
                user_id=6000 + i,
                chat_id=shared_db_config.tracking_chat_ids[0]
            )
            session.add(new_user)

        session.commit()

        # Mock LLM with slow processing to simulate active work
        processing_started = False
        mock_llm = AsyncMock()

        async def slow_spam_check(message):
            nonlocal processing_started
            processing_started = True
            await asyncio.sleep(0.2)  # Simulate slow processing
            return False

        mock_llm.is_spam.side_effect = slow_spam_check
        mock_userbot = AsyncMock()
        mock_telegram_client = AsyncMock()

        processor = QueueProcessor(SessionFactory, mock_llm, mock_userbot, mock_telegram_client, shared_db_config, processing_delay=0.005)

        # Start processor
        task = asyncio.create_task(processor.start())

        # Wait for processing to actually start
        await wait_for_condition(
            lambda: processing_started,
            timeout=1.0,
            poll_interval=0.01,
            description="processing to start"
        )

        # Stop processor while it's processing
        processor.stop()

        # Should shutdown gracefully
        await asyncio.gather(task, return_exceptions=True)

        # Verify processor stopped
        assert processor.running is False

        session.close()

    @pytest.mark.asyncio
    async def test_no_open_sessions_during_llm_call(self, shared_db_config):
        """Test that NO database sessions are open while LLM is_spam() is running.

        This is the core invariant that prevents the 'database is locked' bug:
        the old code held a session open across `await llm.is_spam()` (1-3 sec),
        which meant SQLite's synchronous busy_timeout would block the entire
        asyncio event loop when another handler tried to write.

        We instrument the session factory to track open sessions and verify
        that zero sessions are open at the moment is_spam() is called.
        """
        from db import make_session_factory, initialize_database

        SessionFactory = make_session_factory(shared_db_config)
        initialize_database(SessionFactory, shared_db_config)

        session = SessionFactory()

        # Add a message and user for processing
        msg = MessageQueue(
            user_id=9000,
            chat_id=shared_db_config.tracking_chat_ids[0],
            message_id=9001,
            message_text="Session leak test message",
            status='pending'
        )
        session.add(msg)
        new_user = NewUser(user_id=9000, chat_id=shared_db_config.tracking_chat_ids[0])
        session.add(new_user)
        session.commit()
        session.close()

        # Track open sessions via instrumented factory
        open_sessions = set()
        sessions_open_during_llm = []

        original_factory = SessionFactory

        class TrackedSession:
            """Wrapper that tracks session open/close lifecycle."""
            def __init__(self, real_session, session_id):
                self._real = real_session
                self._id = session_id
                open_sessions.add(session_id)

            def __getattr__(self, name):
                return getattr(self._real, name)

            def __enter__(self):
                self._real.__enter__()
                return self

            def __exit__(self, *args):
                open_sessions.discard(self._id)
                return self._real.__exit__(*args)

            def close(self):
                open_sessions.discard(self._id)
                self._real.close()

        session_counter = [0]

        def tracking_factory():
            session_counter[0] += 1
            sid = session_counter[0]
            return TrackedSession(original_factory(), sid)

        # LLM records how many sessions are open when it's called
        llm_called = asyncio.Event()
        llm_can_finish = asyncio.Event()

        mock_llm = AsyncMock()

        async def instrumented_llm(text):
            # Record snapshot of open sessions at the moment LLM is called
            sessions_open_during_llm.append(len(open_sessions))
            llm_called.set()
            await llm_can_finish.wait()
            return False

        mock_llm.is_spam.side_effect = instrumented_llm
        mock_userbot = AsyncMock()
        mock_telegram_client = AsyncMock()

        processor = QueueProcessor(
            tracking_factory, mock_llm, mock_userbot, mock_telegram_client,
            shared_db_config, processing_delay=0.005
        )

        # Start processor
        processor_task = asyncio.create_task(processor.start())

        # Wait for LLM to be called
        await asyncio.wait_for(llm_called.wait(), timeout=2.0)

        # THE KEY ASSERTION: no sessions should be open when LLM is called
        assert len(sessions_open_during_llm) > 0, "LLM was never called"
        assert sessions_open_during_llm[0] == 0, (
            f"Found {sessions_open_during_llm[0]} open session(s) during LLM call! "
            f"Sessions must be closed before awaiting LLM to prevent database locking."
        )

        # Let LLM finish and clean up
        llm_can_finish.set()
        await asyncio.sleep(0.1)
        processor.stop()
        await asyncio.gather(processor_task, return_exceptions=True)
