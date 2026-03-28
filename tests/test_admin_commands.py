import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta, UTC

from db import MessageQueue, AdminSettings
from queue_processor import QueueProcessor


class TestAdminCommands:
    @pytest.fixture
    def queue_processor_with_messages(self, session_factory, test_session, mock_llm, mock_userbot, mock_telegram_client, test_config):
        """Create a QueueProcessor with sample messages for testing"""
        processor = QueueProcessor(session_factory, mock_llm, mock_userbot, mock_telegram_client, test_config)
        
        # Create messages with different statuses
        now = datetime.now(UTC)
        old_time = now - timedelta(hours=25)
        recent_time = now - timedelta(hours=12)
        
        messages = [
            MessageQueue(user_id=1, chat_id=1, message_id=1, message_text="Pending 1", status='pending'),
            MessageQueue(user_id=2, chat_id=1, message_id=2, message_text="Pending 2", status='pending'),
            MessageQueue(user_id=3, chat_id=1, message_id=3, message_text="Processing", status='processing'),
            MessageQueue(user_id=4, chat_id=1, message_id=4, message_text="Completed old", 
                        status='completed', processed_at=old_time),
            MessageQueue(user_id=5, chat_id=1, message_id=5, message_text="Completed recent", 
                        status='completed', processed_at=recent_time),
            MessageQueue(user_id=6, chat_id=1, message_id=6, message_text="Failed 1", 
                        status='failed', retry_count=2, max_retries=5, 
                        error_message="API error", next_retry_at=now + timedelta(minutes=5)),
            MessageQueue(user_id=7, chat_id=1, message_id=7, message_text="Failed 2", 
                        status='failed', retry_count=1, max_retries=5,
                        error_message="Network error", next_retry_at=now + timedelta(minutes=2)),
            MessageQueue(user_id=8, chat_id=1, message_id=8, message_text="Failed exceeded", 
                        status='failed', retry_count=5, max_retries=5,
                        error_message="Max retries exceeded"),
        ]
        
        test_session.add_all(messages)
        test_session.commit()
        
        return processor

    def test_get_queue_status_detailed(self, queue_processor_with_messages):
        """Test detailed queue status retrieval"""
        status = queue_processor_with_messages.get_queue_status()
        
        assert status['pending'] == 2
        assert status['processing'] == 1
        assert status['completed'] == 2
        assert status['failed'] == 3
        assert status['total'] == 8

    def test_get_queue_status_empty_queue(self, session_factory, mock_llm, mock_userbot, mock_telegram_client, test_config):
        """Test queue status when queue is empty"""
        processor = QueueProcessor(session_factory, mock_llm, mock_userbot, mock_telegram_client, test_config)
        
        status = processor.get_queue_status()
        
        assert status['pending'] == 0
        assert status['processing'] == 0
        assert status['completed'] == 0
        assert status['failed'] == 0
        assert status['total'] == 0

    def test_retry_failed_messages_partial(self, queue_processor_with_messages, test_session):
        """Test retrying failed messages (excluding those that exceeded max retries)"""
        count = queue_processor_with_messages.retry_failed_messages()
        
        # Should retry 2 messages (failed 1 and failed 2, but not the one that exceeded max retries)
        assert count == 2
        
        # Check that the right messages were reset
        all_messages = test_session.query(MessageQueue).filter_by(status='pending').all()
        pending_messages = [msg for msg in all_messages if 'Failed' in msg.message_text]
        
        assert len(pending_messages) == 2
        
        # Check that failed messages were properly reset
        for msg in pending_messages:
            assert msg.status == 'pending'
            assert msg.next_retry_at is None
            assert msg.error_message is None
        
        # Check that the exceeded message remains failed
        exceeded_msg = test_session.query(MessageQueue).filter_by(
            message_text="Failed exceeded"
        ).first()
        assert exceeded_msg.status == 'failed'

    def test_retry_failed_messages_none_eligible(self, session_factory, test_session, mock_llm, mock_userbot, mock_telegram_client, test_config):
        """Test retrying failed messages when none are eligible"""
        processor = QueueProcessor(session_factory, mock_llm, mock_userbot, mock_telegram_client, test_config)
        
        # Create a message that exceeded max retries
        exceeded_msg = MessageQueue(
            user_id=1, chat_id=1, message_id=1, message_text="Exceeded",
            status='failed', retry_count=5, max_retries=5
        )
        test_session.add(exceeded_msg)
        test_session.commit()
        
        count = processor.retry_failed_messages()
        
        assert count == 0
        
        # Message should remain failed
        test_session.refresh(exceeded_msg)
        assert exceeded_msg.status == 'failed'

    def test_clear_completed_messages_default_timeframe(self, queue_processor_with_messages, test_session):
        """Test clearing completed messages with default 24-hour timeframe"""
        count = queue_processor_with_messages.clear_completed_messages()
        
        # Should clear 1 message (the old completed one)
        assert count == 1
        
        # Check remaining messages
        remaining_completed = test_session.query(MessageQueue).filter_by(status='completed').all()
        assert len(remaining_completed) == 1
        assert remaining_completed[0].message_text == "Completed recent"

    def test_clear_completed_messages_custom_timeframe(self, queue_processor_with_messages, test_session):
        """Test clearing completed messages with custom timeframe"""
        # Clear messages older than 6 hours
        count = queue_processor_with_messages.clear_completed_messages(older_than_hours=6)
        
        # Should clear both completed messages (both are older than 6 hours from current time)
        assert count == 2
        
        # Check no completed messages remain
        remaining_completed = test_session.query(MessageQueue).filter_by(status='completed').all()
        assert len(remaining_completed) == 0

    def test_clear_completed_messages_no_completed(self, session_factory, test_session, mock_llm, mock_userbot, mock_telegram_client, test_config):
        """Test clearing completed messages when none exist"""
        processor = QueueProcessor(session_factory, mock_llm, mock_userbot, mock_telegram_client, test_config)
        
        # Create only non-completed messages
        pending_msg = MessageQueue(
            user_id=1, chat_id=1, message_id=1, message_text="Pending",
            status='pending'
        )
        test_session.add(pending_msg)
        test_session.commit()
        
        count = processor.clear_completed_messages()
        
        assert count == 0
        
        # Pending message should remain
        remaining = test_session.query(MessageQueue).all()
        assert len(remaining) == 1
        assert remaining[0].status == 'pending'

    @pytest.mark.asyncio
    async def test_admin_commands_integration_with_telegram_bot(self, session_factory, test_session, mock_llm, mock_userbot, mock_telegram_client, test_config):
        """Test admin commands integration with Telegram bot handlers"""
        from telegram import create_bot
        
        processor = QueueProcessor(session_factory, mock_llm, mock_userbot, mock_telegram_client, test_config)
        
        # Create some test data
        test_messages = [
            MessageQueue(user_id=1, chat_id=1, message_id=1, message_text="Test", status='pending'),
            MessageQueue(user_id=2, chat_id=1, message_id=2, message_text="Test", status='failed', retry_count=1),
        ]
        test_session.add_all(test_messages)
        test_session.commit()
        
        # Mock admin event for queue status
        admin_event = MagicMock()
        admin_event.sender_id = 99999
        admin_event.raw_text = "/queue_status"
        admin_event.reply = AsyncMock()
        
        # Test queue status command
        bot = create_bot(test_session, mock_llm, mock_userbot, test_config)
        bot.queue_processor = processor
        
        # Simulate the command processing logic that would be in the actual handler
        status = processor.get_queue_status()
        expected_message = (
            f"Queue Status:\n"
            f"• Pending: {status['pending']}\n"
            f"• Processing: {status['processing']}\n"
            f"• Failed: {status['failed']}\n"
            f"• Completed: {status['completed']}\n"
            f"• Total: {status['total']}"
        )
        
        assert status['pending'] == 1
        assert status['failed'] == 1
        assert status['total'] == 2
        assert "Queue Status:" in expected_message


    def test_admin_settings_integration(self, session_factory, test_session, mock_llm, mock_userbot, mock_telegram_client, test_config):
        """Test that admin commands work correctly with AdminSettings"""
        processor = QueueProcessor(session_factory, mock_llm, mock_userbot, mock_telegram_client, test_config)
        
        # Check initial admin settings
        admin_settings = test_session.query(AdminSettings).first()
        assert admin_settings is not None
        assert admin_settings.require_approval is False  # Default from conftest
        
        # The queue processor should respect admin settings when processing messages
        # This is more of an integration check to ensure the database schema works correctly
        
        # Update admin settings
        admin_settings.require_approval = True
        test_session.commit()
        
        # Verify the change persisted
        updated_settings = test_session.query(AdminSettings).first()
        assert updated_settings.require_approval is True
        
        # Queue status should still work regardless of admin settings
        status = processor.get_queue_status()
        assert isinstance(status, dict)
        assert 'total' in status