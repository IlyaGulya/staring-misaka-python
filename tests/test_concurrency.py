import pytest
import asyncio
import tempfile
import os
from datetime import datetime, UTC
from unittest.mock import AsyncMock

from queue_processor import QueueProcessor
from db import MessageQueue, NewUser, create_session, AdminSettings
from config import Config


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
        session = create_session(shared_db_config)
        
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
        # Create two separate sessions for two processors
        session1 = create_session(shared_db_config)
        session2 = create_session(shared_db_config)
        
        # Mock LLM and other dependencies
        mock_llm1 = AsyncMock()
        mock_llm1.is_spam.return_value = False
        mock_llm2 = AsyncMock()
        mock_llm2.is_spam.return_value = False

        mock_telegram_client1 = AsyncMock()
        mock_telegram_client2 = AsyncMock()

        # Create two processors
        processor1 = QueueProcessor(session1, mock_llm1, mock_telegram_client1, shared_db_config, processing_delay=0.02)
        processor2 = QueueProcessor(session2, mock_llm2, mock_telegram_client2, shared_db_config, processing_delay=0.02)
        
        processed_messages_1 = []
        processed_messages_2 = []
        
        # Track which messages each processor handles
        original_process_1 = processor1._process_message
        original_process_2 = processor2._process_message
        
        async def track_process_1(message, session=None):
            processed_messages_1.append(message.id)
            return await original_process_1(message, session)
        
        async def track_process_2(message, session=None):
            processed_messages_2.append(message.id)
            return await original_process_2(message, session)
        
        processor1._process_message = track_process_1
        processor2._process_message = track_process_2
        
        # Start both processors concurrently for a short time
        task1 = asyncio.create_task(processor1.start())
        task2 = asyncio.create_task(processor2.start())
        
        # Let them run for a longer time to allow multiple processing cycles
        await asyncio.sleep(3.0)
        
        # Stop both processors
        processor1.stop()
        processor2.stop()
        
        # Wait for tasks to complete
        await asyncio.gather(task1, task2, return_exceptions=True)
        
        # Verify no message was processed by both processors
        overlap = set(processed_messages_1) & set(processed_messages_2)
        assert len(overlap) == 0, f"Messages processed by both processors: {overlap}"
        
        # Verify at least some messages were processed
        total_processed = len(processed_messages_1) + len(processed_messages_2)
        assert total_processed > 0, "No messages were processed"
        
        session1.close()
        session2.close()
    
    @pytest.mark.asyncio
    async def test_concurrent_message_insertion_and_processing(self, shared_db_config):
        """Test concurrent message insertion while processing"""
        session = create_session(shared_db_config)
        
        # Mock dependencies
        mock_llm = AsyncMock()
        mock_llm.is_spam.return_value = False
        mock_telegram_client = AsyncMock()

        processor = QueueProcessor(session, mock_llm, mock_telegram_client, shared_db_config, processing_delay=0.01)
        
        # Add initial NewUser for message processing
        new_user = NewUser(user_id=5000, chat_id=shared_db_config.tracking_chat_ids[0])
        session.add(new_user)
        session.commit()
        
        processed_count = 0
        
        # Track processed messages
        original_process = processor._process_message
        
        async def track_process(message, session=None):
            nonlocal processed_count
            processed_count += 1
            return await original_process(message, session)
        
        processor._process_message = track_process
        
        # Start processor
        processor_task = asyncio.create_task(processor.start())
        
        # Concurrently add messages while processor is running
        async def add_messages():
            for i in range(10):
                processor.add_message_to_queue(
                    user_id=5000,
                    chat_id=shared_db_config.tracking_chat_ids[0],
                    message_id=3000 + i,
                    message_text=f"Concurrent message {i}"
                )
                await asyncio.sleep(0.01)  # Small delay between insertions
        
        add_task = asyncio.create_task(add_messages())
        
        # Let both run for a longer time to allow multiple processing cycles
        await asyncio.sleep(3.0)
        
        # Stop processor
        processor.stop()
        await add_task  # Ensure all messages are added
        
        # Wait for processor to finish
        await asyncio.gather(processor_task, return_exceptions=True)
        
        # Verify messages were processed without conflicts
        assert processed_count > 0, "No messages were processed"
        
        # Verify final queue state is consistent
        remaining_pending = session.query(MessageQueue).filter_by(status='pending').count()
        completed = session.query(MessageQueue).filter_by(status='completed').count()
        
        assert remaining_pending + completed == 10, "Message count inconsistency"
        
        session.close()
    
    @pytest.mark.asyncio
    async def test_concurrent_retry_operations(self, shared_db_config):
        """Test concurrent retry operations don't cause conflicts"""
        session1 = create_session(shared_db_config)
        session2 = create_session(shared_db_config)
        
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
        mock_telegram_client1 = AsyncMock()
        mock_telegram_client2 = AsyncMock()

        processor1 = QueueProcessor(session1, mock_llm1, mock_telegram_client1, shared_db_config, processing_delay=0.01)
        processor2 = QueueProcessor(session2, mock_llm2, mock_telegram_client2, shared_db_config, processing_delay=0.01)
        
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
        session3 = create_session(shared_db_config)
        pending_count = session3.query(MessageQueue).filter_by(status='pending').count()
        failed_count = session3.query(MessageQueue).filter_by(status='failed').count()
        
        # All messages should be reset to pending
        assert pending_count == 5
        assert failed_count == 0
        
        session1.close()
        session2.close()
        session3.close()
    
    @pytest.mark.asyncio
    async def test_processor_graceful_shutdown_with_active_processing(self, shared_db_config):
        """Test processor graceful shutdown while actively processing messages"""
        session = create_session(shared_db_config)
        
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
        mock_llm = AsyncMock()
        async def slow_spam_check(message):
            await asyncio.sleep(0.2)  # Simulate slow processing
            return False

        mock_llm.is_spam.side_effect = slow_spam_check
        mock_telegram_client = AsyncMock()

        processor = QueueProcessor(session, mock_llm, mock_telegram_client, shared_db_config, processing_delay=0.01)
        
        # Start processor
        task = asyncio.create_task(processor.start())
        
        # Let it start processing
        await asyncio.sleep(0.1)
        
        # Stop processor while it's processing
        processor.stop()
        
        # Should shutdown gracefully
        await asyncio.gather(task, return_exceptions=True)
        
        # Verify processor stopped
        assert processor.running is False
        
        session.close()