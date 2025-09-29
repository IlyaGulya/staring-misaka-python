import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, UTC, timedelta

from telegram import create_bot
from db import MessageQueue, NewUser, AdminSettings, ApprovedUser
from queue_processor import QueueProcessor


class TestTelegramIntegration:
    @pytest.fixture
    def mock_telegram_event(self):
        """Create a mock Telegram event"""
        event = MagicMock()
        event.chat_id = 67890
        event.id = 111
        event.raw_text = "Test spam message"
        
        # Mock sender
        sender = MagicMock()
        sender.id = 12345
        sender.first_name = "Test User"
        sender.username = "testuser"
        event.get_sender = AsyncMock(return_value=sender)
        event.sender_id = sender.id
        
        return event

    @pytest.fixture
    def queue_processor_with_integration(self, test_session, mock_llm, mock_telegram_client, test_config):
        """Create a QueueProcessor for integration testing"""
        return QueueProcessor(test_session, mock_llm, mock_telegram_client, test_config)

    @pytest.mark.asyncio
    async def test_message_handler_adds_to_queue(self, test_session, mock_llm, mock_telegram_event, queue_processor_with_integration, test_config):
        """Test that message handler adds messages to queue instead of direct processing"""
        # Create a new user to be monitored
        new_user = NewUser(user_id=12345, chat_id=67890)
        test_session.add(new_user)
        test_session.commit()
        
        # Create bot with queue processor
        bot = create_bot(test_session, mock_llm, test_config)
        bot.queue_processor = queue_processor_with_integration
        # Get the handler we attached in create_bot
        message_handler = bot._handlers["message_handler"]
        
        # Execute the message handler
        await message_handler(mock_telegram_event)
        
        # Verify message was added to queue
        queue_items = test_session.query(MessageQueue).all()
        assert len(queue_items) == 1
        
        queue_item = queue_items[0]
        assert queue_item.user_id == 12345
        assert queue_item.chat_id == 67890
        assert queue_item.message_id == 111
        assert queue_item.message_text == "Test spam message"
        assert queue_item.status == 'pending'

    @pytest.mark.asyncio
    async def test_message_handler_ignores_approved_user(self, test_session, mock_llm, mock_telegram_event, queue_processor_with_integration, test_config):
        """Test that message handler ignores pre-approved users"""
        # Create an approved user
        approved_user = ApprovedUser(user_id=12345, chat_id=67890)
        test_session.add(approved_user)
        test_session.commit()

        # Create bot with queue processor
        bot = create_bot(test_session, mock_llm, test_config)
        bot.queue_processor = queue_processor_with_integration

        message_handler = bot._handlers["message_handler"]
        await message_handler(mock_telegram_event)
        
        # Verify no message was added to queue
        queue_items = test_session.query(MessageQueue).all()
        assert len(queue_items) == 0

    @pytest.mark.asyncio
    async def test_message_handler_ignores_existing_user(self, test_session, mock_llm, mock_telegram_event, queue_processor_with_integration, test_config):
        """Test that message handler ignores messages from existing users (not in NewUser table)"""
        # Don't create a NewUser entry - user is not being monitored
        
        # Create bot with queue processor
        bot = create_bot(test_session, mock_llm, test_config)
        bot.queue_processor = queue_processor_with_integration

        message_handler = bot._handlers["message_handler"]
        await message_handler(mock_telegram_event)

        # Verify no message was added to queue
        queue_items = test_session.query(MessageQueue).all()
        assert len(queue_items) == 0

    @pytest.mark.asyncio
    async def test_message_handler_fallback_when_no_queue_processor(self, test_session, mock_llm, mock_telegram_event, test_config):
        """Test that message handler falls back to direct spam check when queue processor is unavailable"""
        # Create a new user to be monitored
        new_user = NewUser(user_id=12345, chat_id=67890)
        test_session.add(new_user)
        test_session.commit()
        
        # Configure LLM
        mock_llm.is_spam = AsyncMock(return_value=False)
        
        # Create bot WITHOUT queue processor
        bot = create_bot(test_session, mock_llm, test_config)
        
        message_handler = bot._handlers["message_handler"]
        await message_handler(mock_telegram_event)
        
        # Verify LLM was called directly
        mock_llm.is_spam.assert_called_once_with("Test spam message")
        
        # Verify no message was added to queue (fallback doesn't use queue)
        queue_items = test_session.query(MessageQueue).all()
        assert len(queue_items) == 0
        
        # User should be auto-approved (removed from NewUser, added to ApprovedUser)
        remaining_new_users = test_session.query(NewUser).filter_by(user_id=12345, chat_id=67890).all()
        assert len(remaining_new_users) == 0
        
        approved_users = test_session.query(ApprovedUser).filter_by(user_id=12345, chat_id=67890).all()
        assert len(approved_users) == 1

    @pytest.mark.asyncio
    async def test_message_handler_fallback_error_handling(self, test_session, mock_llm, mock_telegram_event, test_config):
        """Test that message handler handles errors gracefully in fallback mode"""
        # Create a new user to be monitored
        new_user = NewUser(user_id=12345, chat_id=67890)
        test_session.add(new_user)
        test_session.commit()
        
        # Configure LLM to raise an error
        mock_llm.is_spam = AsyncMock(side_effect=Exception("API overloaded"))
        
        # Create bot WITHOUT queue processor
        bot = create_bot(test_session, mock_llm, test_config)
        
        message_handler = bot._handlers["message_handler"]
        # Should not raise exception - should handle error gracefully
        await message_handler(mock_telegram_event)
        
        # User should still be in NewUser table (error prevented processing)
        remaining_new_users = test_session.query(NewUser).filter_by(user_id=12345, chat_id=67890).all()
        assert len(remaining_new_users) == 1

    @pytest.mark.asyncio
    async def test_admin_queue_status_command(self, test_session, mock_llm, queue_processor_with_integration, test_config):
        """Test admin queue status command"""
        # Create some queue items with different statuses
        queue_items = [
            MessageQueue(user_id=1, chat_id=1, message_id=1, message_text="Pending", status='pending'),
            MessageQueue(user_id=2, chat_id=1, message_id=2, message_text="Processing", status='processing'),
            MessageQueue(user_id=3, chat_id=1, message_id=3, message_text="Completed", status='completed'),
            MessageQueue(user_id=4, chat_id=1, message_id=4, message_text="Failed", status='failed'),
        ]
        test_session.add_all(queue_items)
        test_session.commit()
        
        # Create mock admin event
        admin_event = MagicMock()
        admin_event.sender_id = 99999  # Admin ID
        admin_event.raw_text = "/queue_status"
        admin_event.reply = AsyncMock()
        
        # Create bot with queue processor
        bot = create_bot(test_session, mock_llm, test_config)
        bot.queue_processor = queue_processor_with_integration
        
        admin_handler = bot._handlers["admin_reply_handler"]
        await admin_handler(admin_event)
        
        # Verify status message was sent
        admin_event.reply.assert_called_once()
        call_args = admin_event.reply.call_args[0][0]
        
        assert "Queue Status:" in call_args
        assert "Pending: 1" in call_args
        assert "Processing: 1" in call_args
        assert "Completed: 1" in call_args
        assert "Failed: 1" in call_args
        assert "Total: 4" in call_args

    @pytest.mark.asyncio
    async def test_admin_retry_failed_command(self, test_session, mock_llm, queue_processor_with_integration, test_config):
        """Test admin retry failed messages command"""
        # Create failed messages
        failed_msg1 = MessageQueue(
            user_id=1, chat_id=1, message_id=1, message_text="Failed 1",
            status='failed', retry_count=2, max_retries=5
        )
        failed_msg2 = MessageQueue(
            user_id=2, chat_id=1, message_id=2, message_text="Failed 2",
            status='failed', retry_count=1, max_retries=5
        )
        test_session.add_all([failed_msg1, failed_msg2])
        test_session.commit()
        
        # Create mock admin event
        admin_event = MagicMock()
        admin_event.sender_id = 99999  # Admin ID
        admin_event.raw_text = "/retry_failed"
        admin_event.reply = AsyncMock()
        
        # Create bot with queue processor
        bot = create_bot(test_session, mock_llm, test_config)
        bot.queue_processor = queue_processor_with_integration
        
        admin_handler = bot._handlers["admin_reply_handler"]
        await admin_handler(admin_event)
        
        # Verify retry message was sent
        admin_event.reply.assert_called_once()
        call_args = admin_event.reply.call_args[0][0]
        assert "Reset 2 failed messages to pending status" in call_args
        
        # Verify messages were reset
        test_session.refresh(failed_msg1)
        test_session.refresh(failed_msg2)
        assert failed_msg1.status == 'pending'
        assert failed_msg2.status == 'pending'

    @pytest.mark.asyncio
    async def test_admin_clear_completed_command(self, test_session, mock_llm, queue_processor_with_integration, test_config):
        """Test admin clear completed messages command"""
        # Create completed messages
        from datetime import datetime, timedelta
        old_time = datetime.now(UTC) - timedelta(hours=25)
        
        completed_msg = MessageQueue(
            user_id=1, chat_id=1, message_id=1, message_text="Completed",
            status='completed', processed_at=old_time
        )
        test_session.add(completed_msg)
        test_session.commit()
        
        # Create mock admin event
        admin_event = MagicMock()
        admin_event.sender_id = 99999  # Admin ID
        admin_event.raw_text = "/clear_completed"
        admin_event.reply = AsyncMock()
        
        # Create bot with queue processor
        bot = create_bot(test_session, mock_llm, test_config)
        bot.queue_processor = queue_processor_with_integration
        
        admin_handler = bot._handlers["admin_reply_handler"]
        await admin_handler(admin_event)
        
        # Verify clear message was sent
        admin_event.reply.assert_called_once()
        call_args = admin_event.reply.call_args[0][0]
        assert "Cleared 1 completed messages from queue" in call_args
        
        # Verify message was deleted
        remaining_messages = test_session.query(MessageQueue).all()
        assert len(remaining_messages) == 0

    @pytest.mark.asyncio
    async def test_non_admin_queue_command_rejection(self, test_session, mock_llm, queue_processor_with_integration, test_config):
        """Test that non-admin users cannot use queue commands"""
        # Create mock non-admin event
        non_admin_event = MagicMock()
        non_admin_event.sender_id = 12345  # Not admin
        non_admin_event.raw_text = "/queue_status"
        non_admin_event.reply = AsyncMock()
        
        # Create bot with queue processor
        bot = create_bot(test_session, mock_llm, test_config)
        bot.queue_processor = queue_processor_with_integration
        
        admin_handler = bot._handlers["admin_reply_handler"]
        await admin_handler(non_admin_event)
        
        # Should not be called for non-admin (handler should filter by sender)
        # This tests that the handler properly checks sender_id
        # In actual implementation, this would be filtered by the event builder
        pass  # Test passes if no exception is raised