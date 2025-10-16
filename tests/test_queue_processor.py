import pytest
import asyncio
from datetime import datetime, timedelta, UTC
from unittest.mock import AsyncMock, patch, MagicMock
from sqlalchemy.exc import SQLAlchemyError

from queue_processor import QueueProcessor
from db import MessageQueue, NewUser, AdminSettings, ApprovedUser, BannedUser, PendingBanRequest


class TestQueueProcessor:
    @pytest.fixture
    def queue_processor(self, session_factory, mock_llm, mock_userbot, mock_telegram_client, test_config):
        """Create a QueueProcessor instance for testing"""
        return QueueProcessor(session_factory, mock_llm, mock_userbot, mock_telegram_client, test_config)


    def test_add_message_to_queue(self, queue_processor):
        """Test adding a message to the queue"""
        user_id = 12345
        chat_id = 67890
        message_id = 111
        message_text = "Test message"
        
        queue_item = queue_processor.add_message_to_queue(user_id, chat_id, message_id, message_text)
        
        assert queue_item.user_id == user_id
        assert queue_item.chat_id == chat_id
        assert queue_item.message_id == message_id
        assert queue_item.message_text == message_text
        assert queue_item.status == 'pending'
        assert queue_item.retry_count == 0

    def test_add_duplicate_message_to_queue(self, queue_processor):
        """Test adding a duplicate message to the queue"""
        user_id = 12345
        chat_id = 67890
        message_id = 111
        message_text = "Test message"
        
        # Add first message
        first_item = queue_processor.add_message_to_queue(user_id, chat_id, message_id, message_text)
        
        # Add same message again
        second_item = queue_processor.add_message_to_queue(user_id, chat_id, message_id, message_text)
        
        # Should return the existing item
        assert first_item.id == second_item.id

    def test_get_queue_status(self, queue_processor, test_session):
        """Test getting queue status"""
        # Create messages with different statuses
        messages = [
            MessageQueue(user_id=1, chat_id=1, message_id=1, message_text="Pending", status='pending'),
            MessageQueue(user_id=2, chat_id=1, message_id=2, message_text="Processing", status='processing'),
            MessageQueue(user_id=3, chat_id=1, message_id=3, message_text="Completed", status='completed'),
            MessageQueue(user_id=4, chat_id=1, message_id=4, message_text="Failed", status='failed'),
            MessageQueue(user_id=5, chat_id=1, message_id=5, message_text="Another pending", status='pending'),
        ]
        
        test_session.add_all(messages)
        test_session.commit()
        
        status = queue_processor.get_queue_status()
        
        assert status['pending'] == 2
        assert status['processing'] == 1
        assert status['completed'] == 1
        assert status['failed'] == 1
        assert status['total'] == 5

    def test_retry_failed_messages(self, queue_processor, test_session):
        """Test retrying failed messages"""
        # Create failed messages with different retry counts
        failed_msg1 = MessageQueue(
            user_id=1, chat_id=1, message_id=1, message_text="Failed 1",
            status='failed', retry_count=2, max_retries=5,
            error_message="Some error", next_retry_at=datetime.now(UTC)
        )
        failed_msg2 = MessageQueue(
            user_id=2, chat_id=1, message_id=2, message_text="Failed 2",
            status='failed', retry_count=5, max_retries=5,  # Exceeded max retries
            error_message="Some error", next_retry_at=datetime.now(UTC)
        )
        failed_msg3 = MessageQueue(
            user_id=3, chat_id=1, message_id=3, message_text="Failed 3",
            status='failed', retry_count=1, max_retries=5,
            error_message="Some error", next_retry_at=datetime.now(UTC)
        )
        
        test_session.add_all([failed_msg1, failed_msg2, failed_msg3])
        test_session.commit()
        
        # Retry failed messages
        count = queue_processor.retry_failed_messages()
        
        # Should retry 2 messages (not the one that exceeded max retries)
        assert count == 2
        
        # Check that messages were reset properly
        test_session.refresh(failed_msg1)
        test_session.refresh(failed_msg2)
        test_session.refresh(failed_msg3)
        
        assert failed_msg1.status == 'pending'
        assert failed_msg1.next_retry_at is None
        assert failed_msg1.error_message is None
        
        assert failed_msg2.status == 'failed'  # Should remain failed (exceeded max retries)
        
        assert failed_msg3.status == 'pending'

    def test_clear_completed_messages(self, queue_processor, test_session):
        """Test clearing completed messages"""
        now = datetime.now(UTC)
        old_time = now - timedelta(hours=25)  # Older than 24 hours
        recent_time = now - timedelta(hours=12)  # Less than 24 hours
        
        # Create completed messages with different processed times
        old_completed = MessageQueue(
            user_id=1, chat_id=1, message_id=1, message_text="Old completed",
            status='completed', processed_at=old_time
        )
        recent_completed = MessageQueue(
            user_id=2, chat_id=1, message_id=2, message_text="Recent completed",
            status='completed', processed_at=recent_time
        )
        pending_msg = MessageQueue(
            user_id=3, chat_id=1, message_id=3, message_text="Pending",
            status='pending'
        )
        
        test_session.add_all([old_completed, recent_completed, pending_msg])
        test_session.commit()
        
        # Clear completed messages older than 24 hours
        count = queue_processor.clear_completed_messages(older_than_hours=24)
        
        # Should clear only the old completed message
        assert count == 1
        
        # Verify the correct message was deleted
        remaining = test_session.query(MessageQueue).all()
        assert len(remaining) == 2
        
        remaining_ids = [msg.user_id for msg in remaining]
        assert 1 not in remaining_ids  # Old completed message deleted
        assert 2 in remaining_ids      # Recent completed message remains
        assert 3 in remaining_ids      # Pending message remains

    @pytest.mark.asyncio
    async def test_process_message_user_no_longer_monitored(self, queue_processor, test_session, sample_message_queue):
        """Test processing message when user is no longer being monitored"""
        # Don't create a NewUser entry, so user is not monitored

        await queue_processor._process_message(sample_message_queue, test_session)
        
        # Should mark as completed since user is not monitored
        test_session.refresh(sample_message_queue)
        assert sample_message_queue.status == 'completed'
        assert sample_message_queue.processed_at is not None

    @pytest.mark.asyncio
    async def test_process_message_user_pre_approved(self, queue_processor, test_session, sample_message_queue, sample_new_user):
        """Test processing message when user is pre-approved"""
        # Create approved user
        approved_user = ApprovedUser(
            user_id=sample_message_queue.user_id,
            chat_id=sample_message_queue.chat_id
        )
        test_session.add(approved_user)
        test_session.commit()
        
        await queue_processor._process_message(sample_message_queue, test_session)
        
        # Should mark as completed since user is pre-approved
        test_session.refresh(sample_message_queue)
        assert sample_message_queue.status == 'completed'
        assert sample_message_queue.processed_at is not None

    @pytest.mark.asyncio
    async def test_process_message_not_spam_auto_approve(self, queue_processor, test_session, sample_message_queue, sample_new_user, mock_llm):
        """Test processing message that is not spam - should auto-approve user"""
        # Configure LLM to return not spam
        mock_llm.is_spam.return_value = False
        
        await queue_processor._process_message(sample_message_queue, test_session)
        
        # Should mark as completed and auto-approve user
        test_session.refresh(sample_message_queue)
        assert sample_message_queue.status == 'completed'
        assert sample_message_queue.spam_result is False
        
        # User should be removed from monitoring and added to approved
        new_user = test_session.query(NewUser).filter_by(
            user_id=sample_message_queue.user_id,
            chat_id=sample_message_queue.chat_id
        ).first()
        assert new_user is None
        
        approved_user = test_session.query(ApprovedUser).filter_by(
            user_id=sample_message_queue.user_id,
            chat_id=sample_message_queue.chat_id
        ).first()
        assert approved_user is not None

    @pytest.mark.asyncio
    async def test_process_message_spam_with_admin_approval(self, queue_processor, test_session, sample_message_queue, sample_new_user, mock_llm, mock_telegram_client):
        """Test processing spam message with admin approval required"""
        # Configure LLM to return spam
        mock_llm.is_spam.return_value = True
        
        # Set admin settings to require approval
        admin_settings = test_session.query(AdminSettings).first()
        admin_settings.require_approval = True
        test_session.commit()
        
        # Mock telegram client responses
        mock_user = MagicMock()
        mock_user.username = "testuser"
        mock_user.first_name = "Test User"
        mock_telegram_client.get_entity.return_value = mock_user
        
        mock_sent_message = MagicMock()
        mock_sent_message.id = 12345
        mock_telegram_client.send_message.return_value = mock_sent_message
        
        await queue_processor._process_message(sample_message_queue, test_session)
        
        # Should mark as completed and create pending ban request
        test_session.refresh(sample_message_queue)
        assert sample_message_queue.status == 'completed'
        assert sample_message_queue.spam_result is True
        
        # Should create pending ban request
        pending_request = test_session.query(PendingBanRequest).filter_by(
            sender_id=sample_message_queue.user_id
        ).first()
        assert pending_request is not None
        assert pending_request.admin_message_id == 12345

    @pytest.mark.asyncio
    async def test_process_message_spam_automatic_ban(self, queue_processor, test_session, sample_message_queue, sample_new_user, mock_llm, mock_userbot, mock_telegram_client):
        """Test processing spam message with automatic ban"""
        # Configure LLM to return spam
        mock_llm.is_spam.return_value = True
        
        # Set admin settings to not require approval
        admin_settings = test_session.query(AdminSettings).first()
        admin_settings.require_approval = False
        test_session.commit()
        
        # Mock user entity
        mock_user = MagicMock()
        mock_user.username = "spammer"
        mock_user.first_name = "Spam User"
        mock_telegram_client.get_entity.return_value = mock_user
        
        await queue_processor._process_message(sample_message_queue, test_session)
        
        # Should mark as completed and create banned user
        test_session.refresh(sample_message_queue)
        assert sample_message_queue.status == 'completed'
        assert sample_message_queue.spam_result is True
        
        # Should create banned user record
        banned_user = test_session.query(BannedUser).filter_by(
            user_id=sample_message_queue.user_id
        ).first()
        assert banned_user is not None
        assert banned_user.message_text == sample_message_queue.message_text
        
        # Should remove user from monitoring
        new_user = test_session.query(NewUser).filter_by(
            user_id=sample_message_queue.user_id,
            chat_id=sample_message_queue.chat_id
        ).first()
        assert new_user is None
        
        # Should call userbot to send ban command
        mock_userbot.send_ban_command.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_message_llm_error_retry_logic(self, queue_processor, test_session, sample_message_queue, sample_new_user, mock_llm):
        """Test error handling and retry logic when LLM fails"""
        # Configure LLM to raise an exception
        mock_llm.is_spam.side_effect = Exception("API overloaded")
        
        await queue_processor._process_message(sample_message_queue, test_session)
        
        # Should mark as failed with error message
        test_session.refresh(sample_message_queue)
        assert sample_message_queue.status == 'failed'
        assert sample_message_queue.error_message == "API overloaded"
        assert sample_message_queue.retry_count == 1
        assert sample_message_queue.next_retry_at is not None
        
        # Next retry time should be in the future (exponential backoff)
        # Convert to timezone-aware for comparison since SQLite stores naive datetime
        assert sample_message_queue.next_retry_at.replace(tzinfo=UTC) > datetime.now(UTC)

    @pytest.mark.asyncio
    async def test_exponential_backoff_calculation(self, queue_processor, test_session, sample_message_queue, sample_new_user, mock_llm):
        """Test exponential backoff calculation for retries"""
        mock_llm.is_spam.side_effect = Exception("API error")
        
        # Test different retry counts
        expected_delays = [30, 60, 120, 240, 300]  # Exponential backoff with max 300s
        
        for expected_delay in expected_delays:
            sample_message_queue.status = 'pending'
            sample_message_queue.error_message = None
            sample_message_queue.next_retry_at = None
            test_session.commit()
            
            before_time = datetime.now(UTC)
            await queue_processor._process_message(sample_message_queue, test_session)
            after_time = datetime.now(UTC)
            
            test_session.refresh(sample_message_queue)
            
            # Calculate actual delay - convert naive datetime to timezone-aware for comparison
            actual_delay = (sample_message_queue.next_retry_at.replace(tzinfo=UTC) - before_time).total_seconds()
            
            # Allow some tolerance for execution time
            assert abs(actual_delay - expected_delay) < 5, f"Expected ~{expected_delay}s, got {actual_delay}s"

    @pytest.mark.asyncio
    async def test_start_stop_processor(self, queue_processor):
        """Test starting and stopping the queue processor"""
        assert queue_processor.running is False
        
        # Test starting
        task = asyncio.create_task(queue_processor.start())
        
        # Give it a moment to start
        await asyncio.sleep(0.1)
        
        assert queue_processor.running is True
        
        # Test stopping
        queue_processor.stop()
        assert queue_processor.running is False
        
        # Cancel the task to avoid warnings
        task.cancel()
        
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass  # Expected when cancelling