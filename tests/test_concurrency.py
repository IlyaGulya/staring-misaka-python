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
    async def test_slow_llm_does_not_block_database_for_other_operations(self, shared_db_config):
        """Test that a slow LLM call does NOT hold a database session open,
        allowing other database operations to proceed concurrently.

        This is the core scenario that caused the original 'database is locked' bug:
        _process_message held a session open while awaiting llm.is_spam() (1-3 sec),
        which blocked all other database access (busy_timeout exceeded → bot crash).
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
            message_text="Slow LLM test message",
            status='pending'
        )
        session.add(msg)
        new_user = NewUser(user_id=9000, chat_id=shared_db_config.tracking_chat_ids[0])
        session.add(new_user)
        session.commit()
        msg_id = msg.id

        # LLM will take 0.5s to respond — simulating real-world latency
        llm_started = asyncio.Event()
        llm_can_finish = asyncio.Event()

        mock_llm = AsyncMock()

        async def slow_llm(text):
            llm_started.set()
            await llm_can_finish.wait()  # Block until test lets it finish
            return False

        mock_llm.is_spam.side_effect = slow_llm
        mock_userbot = AsyncMock()
        mock_telegram_client = AsyncMock()

        processor = QueueProcessor(
            SessionFactory, mock_llm, mock_userbot, mock_telegram_client,
            shared_db_config, processing_delay=0.005
        )

        # Start the processor — it will pick up the message and call slow LLM
        processor_task = asyncio.create_task(processor.start())

        # Wait for the LLM call to start (session should be released by now)
        await asyncio.wait_for(llm_started.wait(), timeout=2.0)

        # NOW try to do a database write from a separate session.
        # If the old code held the session open across the LLM await,
        # this would block/timeout with "database is locked".
        db_write_succeeded = False

        async def concurrent_db_write():
            nonlocal db_write_succeeded
            write_session = SessionFactory()
            try:
                another_user = NewUser(user_id=9999, chat_id=shared_db_config.tracking_chat_ids[0])
                write_session.add(another_user)
                write_session.commit()
                db_write_succeeded = True
            finally:
                write_session.close()

        # This should complete quickly (< 1 sec) if sessions are not held across awaits
        await asyncio.wait_for(concurrent_db_write(), timeout=1.0)

        assert db_write_succeeded, "Database write was blocked while LLM was processing — session leak!"

        # Let the LLM finish
        llm_can_finish.set()

        # Wait for processor to finish processing
        await wait_for_condition(
            lambda: SessionFactory().query(MessageQueue).filter_by(id=msg_id, status='completed').first() is not None,
            timeout=2.0,
            poll_interval=0.01,
            description="message to be completed"
        )

        # Stop processor
        processor.stop()
        await asyncio.gather(processor_task, return_exceptions=True)

        session.close()

    @pytest.mark.asyncio
    async def test_slow_llm_with_concurrent_message_insertion(self, shared_db_config):
        """Test that add_message_to_queue works while LLM is processing.

        This verifies the fix for the production scenario where:
        1. Processor is awaiting llm.is_spam() for message A
        2. A new message B arrives and message_handler calls add_message_to_queue()
        3. add_message_to_queue must NOT be blocked by the LLM session
        """
        from db import make_session_factory, initialize_database

        SessionFactory = make_session_factory(shared_db_config)
        initialize_database(SessionFactory, shared_db_config)

        session = SessionFactory()

        # Add initial message and user
        msg = MessageQueue(
            user_id=8000,
            chat_id=shared_db_config.tracking_chat_ids[0],
            message_id=8001,
            message_text="First message",
            status='pending'
        )
        session.add(msg)
        new_user = NewUser(user_id=8000, chat_id=shared_db_config.tracking_chat_ids[0])
        session.add(new_user)
        session.commit()

        # Slow LLM with barrier
        llm_started = asyncio.Event()
        llm_can_finish = asyncio.Event()

        mock_llm = AsyncMock()

        async def slow_llm(text):
            llm_started.set()
            await llm_can_finish.wait()
            return False

        mock_llm.is_spam.side_effect = slow_llm
        mock_userbot = AsyncMock()
        mock_telegram_client = AsyncMock()

        processor = QueueProcessor(
            SessionFactory, mock_llm, mock_userbot, mock_telegram_client,
            shared_db_config, processing_delay=0.005
        )

        # Start processor
        processor_task = asyncio.create_task(processor.start())

        # Wait for LLM to be called (= session should be released)
        await asyncio.wait_for(llm_started.wait(), timeout=2.0)

        # Insert a new message while LLM is "thinking"
        # This is exactly what message_handler does when a new message arrives
        result = await asyncio.wait_for(
            processor.add_message_to_queue(
                user_id=8000,
                chat_id=shared_db_config.tracking_chat_ids[0],
                message_id=8002,
                message_text="Second message while LLM is busy"
            ),
            timeout=1.0
        )

        assert result is not None, "add_message_to_queue was blocked by LLM processing!"
        assert result.message_text == "Second message while LLM is busy"

        # Let LLM finish
        llm_can_finish.set()

        # Stop processor
        await asyncio.sleep(0.1)
        processor.stop()
        await asyncio.gather(processor_task, return_exceptions=True)

        session.close()
