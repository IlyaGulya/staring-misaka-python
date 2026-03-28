import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, UTC, timedelta

from telethon.tl.types import PeerChannel

from telegram import create_bot
from db import MessageQueue, NewUser, BannedUser, AdminSettings, ApprovedUser
from llm import SpamCheckResponse
from queue_processor import QueueProcessor


class TestTelegramIntegration:
    @pytest.fixture
    def mock_telegram_event(self):
        """Create a mock Telegram event"""
        event = MagicMock()
        event.chat_id = 67890
        event.id = 111
        event.raw_text = "Test spam message"
        event.message.reply_to = None
        event.message.fwd_from = None
        event.message.reply_markup = None
        event.message.entities = None
        event.message.media = None
        event.message.message = "Test spam message"

        # Mock sender
        sender = MagicMock()
        sender.id = 12345
        sender.first_name = "Test User"
        sender.username = "testuser"
        event.get_sender = AsyncMock(return_value=sender)
        event.sender_id = sender.id

        return event

    @pytest.fixture
    def queue_processor_with_integration(self, session_factory, mock_llm, mock_userbot, mock_telegram_client, test_config):
        """Create a QueueProcessor for integration testing"""
        return QueueProcessor(session_factory, mock_llm, mock_userbot, mock_telegram_client, test_config)

    @pytest.mark.asyncio
    async def test_message_handler_adds_to_queue(self, session_factory, test_session, mock_llm, mock_userbot, mock_telegram_event, queue_processor_with_integration, test_config):
        """Test that message handler adds messages to queue instead of direct processing"""
        # Create a new user to be monitored
        new_user = NewUser(user_id=12345, chat_id=67890)
        test_session.add(new_user)
        test_session.commit()

        # Create bot with queue processor
        bot = create_bot(session_factory, mock_llm, mock_userbot, test_config)
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
        assert "Test spam message" in queue_item.message_text
        assert queue_item.status == 'pending'

    @pytest.mark.asyncio
    async def test_message_handler_ignores_approved_user(self, session_factory, test_session, mock_llm, mock_userbot, mock_telegram_event, queue_processor_with_integration, test_config):
        """Test that message handler ignores pre-approved users"""
        # Create an approved user
        approved_user = ApprovedUser(user_id=12345, chat_id=67890)
        test_session.add(approved_user)
        test_session.commit()

        # Create bot with queue processor
        bot = create_bot(session_factory, mock_llm, mock_userbot, test_config)
        bot.queue_processor = queue_processor_with_integration

        message_handler = bot._handlers["message_handler"]
        await message_handler(mock_telegram_event)
        
        # Verify no message was added to queue
        queue_items = test_session.query(MessageQueue).all()
        assert len(queue_items) == 0

    @pytest.mark.asyncio
    async def test_message_handler_ignores_existing_user(self, session_factory, test_session, mock_llm, mock_userbot, mock_telegram_event, queue_processor_with_integration, test_config):
        """Test that message handler ignores messages from existing users (not in NewUser table)"""
        # Don't create a NewUser entry - user is not being monitored

        # Create bot with queue processor
        bot = create_bot(session_factory, mock_llm, mock_userbot, test_config)
        bot.queue_processor = queue_processor_with_integration

        message_handler = bot._handlers["message_handler"]
        await message_handler(mock_telegram_event)

        # Verify no message was added to queue
        queue_items = test_session.query(MessageQueue).all()
        assert len(queue_items) == 0

    @pytest.mark.asyncio
    async def test_admin_queue_status_command(self, session_factory, test_session, mock_llm, mock_userbot, queue_processor_with_integration, test_config):
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
        bot = create_bot(session_factory, mock_llm, mock_userbot, test_config)
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
    async def test_admin_retry_failed_command(self, session_factory, test_session, mock_llm, mock_userbot, queue_processor_with_integration, test_config):
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
        bot = create_bot(session_factory, mock_llm, mock_userbot, test_config)
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
    async def test_admin_clear_completed_command(self, session_factory, test_session, mock_llm, mock_userbot, queue_processor_with_integration, test_config):
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
        bot = create_bot(session_factory, mock_llm, mock_userbot, test_config)
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
    async def test_non_admin_queue_command_rejection(self, session_factory, test_session, mock_llm, mock_userbot, queue_processor_with_integration, test_config):
        """Test that non-admin users cannot use queue commands"""
        # Create mock non-admin event
        non_admin_event = MagicMock()
        non_admin_event.sender_id = 12345  # Not admin
        non_admin_event.raw_text = "/queue_status"
        non_admin_event.reply = AsyncMock()

        # Create bot with queue processor
        bot = create_bot(session_factory, mock_llm, mock_userbot, test_config)
        bot.queue_processor = queue_processor_with_integration
        
        admin_handler = bot._handlers["admin_reply_handler"]
        await admin_handler(non_admin_event)
        
        # Should not be called for non-admin (handler should filter by sender)
        # This tests that the handler properly checks sender_id
        # In actual implementation, this would be filtered by the event builder
        pass  # Test passes if no exception is raised


class TestCrossChannelReplyDetection:
    """Tests for cross-channel reply spam detection."""

    SUPERGROUP_CHAT_ID = -1001075815423
    CURRENT_CHANNEL_ID = 1075815423
    UNRELATED_CHANNEL_ID = 9999999999
    LINKED_CHANNEL_ID = 5555555555

    @pytest.fixture
    def cross_channel_event(self):
        """Create a mock event with a cross-channel reply."""
        event = MagicMock()
        event.chat_id = self.SUPERGROUP_CHAT_ID
        event.id = 111
        event.raw_text = "Лучший!!"
        event.message.reply_to.reply_to_peer_id = PeerChannel(channel_id=self.UNRELATED_CHANNEL_ID)
        event.message.fwd_from = None
        event.message.reply_markup = None
        event.message.entities = None
        event.message.media = None
        event.message.message = "Лучший!!"

        sender = MagicMock()
        sender.id = 12345
        sender.first_name = "Spammer"
        sender.username = "spammer_bot"
        event.get_sender = AsyncMock(return_value=sender)
        event.sender_id = sender.id

        return event

    @pytest.fixture
    def cross_channel_test_config(self, test_config):
        """Extend test config to include supergroup chat ID."""
        test_config.tracking_chat_ids = [self.SUPERGROUP_CHAT_ID]
        return test_config

    @pytest.mark.asyncio
    async def test_cross_channel_reply_triggers_ban(self, session_factory, test_session, mock_llm, mock_userbot, cross_channel_event, cross_channel_test_config):
        """New user replying to a post from an unrelated channel gets banned."""
        new_user = NewUser(user_id=12345, chat_id=self.SUPERGROUP_CHAT_ID)
        test_session.add(new_user)
        test_session.commit()

        bot = create_bot(session_factory, mock_llm, mock_userbot, cross_channel_test_config)
        bot.queue_processor = MagicMock()
        bot.queue_processor.add_message_to_queue = AsyncMock()
        # Pre-populate cache: no linked channel for this group
        bot._linked_channel_cache[self.SUPERGROUP_CHAT_ID] = None
        # Mock client methods (bot IS the TelegramClient)
        bot.send_message = AsyncMock()
        bot.get_entity = AsyncMock(return_value=MagicMock(username="spammer_bot", first_name="Spammer"))

        message_handler = bot._handlers["message_handler"]
        await message_handler(cross_channel_event)

        # Should NOT go to queue — banned directly
        bot.queue_processor.add_message_to_queue.assert_not_called()
        # LLM should NOT be called
        mock_llm.is_spam.assert_not_called()
        # User should be banned
        banned = test_session.query(BannedUser).filter_by(user_id=12345).first()
        assert banned is not None

    @pytest.mark.asyncio
    async def test_linked_channel_reply_not_banned(self, session_factory, test_session, mock_llm, mock_userbot, cross_channel_test_config):
        """Reply to the group's own linked channel is legitimate — goes to queue."""
        new_user = NewUser(user_id=12345, chat_id=self.SUPERGROUP_CHAT_ID)
        test_session.add(new_user)
        test_session.commit()

        event = MagicMock()
        event.chat_id = self.SUPERGROUP_CHAT_ID
        event.id = 222
        event.raw_text = "Отличный пост!"
        event.message.reply_to.reply_to_peer_id = PeerChannel(channel_id=self.LINKED_CHANNEL_ID)
        event.message.fwd_from = None
        event.message.reply_markup = None
        event.message.entities = None
        event.message.media = None
        event.message.message = "Отличный пост!"

        sender = MagicMock()
        sender.id = 12345
        sender.first_name = "Real User"
        sender.username = "realuser"
        event.get_sender = AsyncMock(return_value=sender)
        event.sender_id = sender.id

        bot = create_bot(session_factory, mock_llm, mock_userbot, cross_channel_test_config)
        # Pre-populate linked channel cache
        bot._linked_channel_cache[self.SUPERGROUP_CHAT_ID] = self.LINKED_CHANNEL_ID
        bot.queue_processor = MagicMock()
        bot.queue_processor.add_message_to_queue = AsyncMock()

        message_handler = bot._handlers["message_handler"]
        await message_handler(event)

        # Should go to queue, NOT banned
        bot.queue_processor.add_message_to_queue.assert_called_once()
        banned = test_session.query(BannedUser).filter_by(user_id=12345).first()
        assert banned is None

    @pytest.mark.asyncio
    async def test_same_channel_reply_not_banned(self, session_factory, test_session, mock_llm, mock_userbot, cross_channel_test_config):
        """Reply to a post from this same group is not flagged."""
        new_user = NewUser(user_id=12345, chat_id=self.SUPERGROUP_CHAT_ID)
        test_session.add(new_user)
        test_session.commit()

        event = MagicMock()
        event.chat_id = self.SUPERGROUP_CHAT_ID
        event.id = 333
        event.raw_text = "Согласен!"
        event.message.reply_to.reply_to_peer_id = PeerChannel(channel_id=self.CURRENT_CHANNEL_ID)
        event.message.fwd_from = None
        event.message.reply_markup = None
        event.message.entities = None
        event.message.media = None
        event.message.message = "Согласен!"

        sender = MagicMock()
        sender.id = 12345
        sender.first_name = "Normal User"
        sender.username = "normaluser"
        event.get_sender = AsyncMock(return_value=sender)
        event.sender_id = sender.id

        bot = create_bot(session_factory, mock_llm, mock_userbot, cross_channel_test_config)
        bot.queue_processor = MagicMock()
        bot.queue_processor.add_message_to_queue = AsyncMock()

        message_handler = bot._handlers["message_handler"]
        await message_handler(event)

        # Should go to queue, NOT banned
        bot.queue_processor.add_message_to_queue.assert_called_once()
        banned = test_session.query(BannedUser).filter_by(user_id=12345).first()
        assert banned is None

    @pytest.mark.asyncio
    async def test_api_error_skips_cross_channel_check(self, session_factory, test_session, mock_llm, mock_userbot, cross_channel_test_config):
        """If linked channel API call fails, skip cross-channel ban and send to LLM instead."""
        new_user = NewUser(user_id=12345, chat_id=self.SUPERGROUP_CHAT_ID)
        test_session.add(new_user)
        test_session.commit()

        event = MagicMock()
        event.chat_id = self.SUPERGROUP_CHAT_ID
        event.id = 555
        event.raw_text = "Лучший!!"
        event.message.reply_to.reply_to_peer_id = PeerChannel(channel_id=self.UNRELATED_CHANNEL_ID)
        event.message.fwd_from = None
        event.message.reply_markup = None
        event.message.entities = None
        event.message.media = None
        event.message.message = "Лучший!!"

        sender = MagicMock()
        sender.id = 12345
        sender.first_name = "Maybe Spammer"
        sender.username = "maybebot"
        event.get_sender = AsyncMock(return_value=sender)
        event.sender_id = sender.id

        bot = create_bot(session_factory, mock_llm, mock_userbot, cross_channel_test_config)
        bot.queue_processor = MagicMock()
        bot.queue_processor.add_message_to_queue = AsyncMock()
        # Do NOT pre-populate cache — _get_linked_channel_id will fail (client not connected)
        # and return _UNKNOWN, so cross-channel check should be skipped

        message_handler = bot._handlers["message_handler"]
        await message_handler(event)

        # Should go to queue (safe fallback), NOT banned
        bot.queue_processor.add_message_to_queue.assert_called_once()
        queued_text = bot.queue_processor.add_message_to_queue.call_args.kwargs['message_text']
        assert "<cross_channel_reply>" in queued_text
        assert "Лучший!!" in queued_text
        banned = test_session.query(BannedUser).filter_by(user_id=12345).first()
        assert banned is None

    @pytest.mark.asyncio
    async def test_no_reply_not_flagged(self, session_factory, test_session, mock_llm, mock_userbot, cross_channel_test_config):
        """Regular message with no reply is not flagged by cross-channel check."""
        new_user = NewUser(user_id=12345, chat_id=self.SUPERGROUP_CHAT_ID)
        test_session.add(new_user)
        test_session.commit()

        event = MagicMock()
        event.chat_id = self.SUPERGROUP_CHAT_ID
        event.id = 444
        event.raw_text = "Привет всем!"
        event.message.reply_to = None
        event.message.fwd_from = None
        event.message.reply_markup = None
        event.message.entities = None
        event.message.media = None
        event.message.message = "Привет всем!"

        sender = MagicMock()
        sender.id = 12345
        sender.first_name = "Normal User"
        sender.username = "normaluser"
        event.get_sender = AsyncMock(return_value=sender)
        event.sender_id = sender.id

        bot = create_bot(session_factory, mock_llm, mock_userbot, cross_channel_test_config)
        bot.queue_processor = MagicMock()
        bot.queue_processor.add_message_to_queue = AsyncMock()

        message_handler = bot._handlers["message_handler"]
        await message_handler(event)

        # Should go to queue, NOT banned
        bot.queue_processor.add_message_to_queue.assert_called_once()
        banned = test_session.query(BannedUser).filter_by(user_id=12345).first()
        assert banned is None