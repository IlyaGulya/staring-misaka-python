import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

from telethon import events
from telethon.tl.types import UpdateChannelParticipant

from telegram import create_bot
from db import GroupSettings, NewUser, MessageQueue
from queue_processor import QueueProcessor


class TestGroupSettings:
    """Test group settings functionality (enable/disable bot per group)"""

    @pytest.fixture
    def event_env(self, test_session, mock_llm, mock_userbot, test_config, mock_queue_processor):
        """Create a bot with a patched Telethon client and return (bot, client)."""
        with patch('telegram.TelegramClient') as mock_client_cls:
            mock_client = MagicMock()
            # Provide a minimal .on decorator that just returns the function.
            def on_decorator(*args, **kwargs):
                def _wrap(fn):
                    return fn
                return _wrap
            mock_client.on = on_decorator
            mock_client_cls.return_value = mock_client

            bot = create_bot(test_session, mock_llm, mock_userbot, test_config)
            bot.queue_processor = mock_queue_processor
            return bot, mock_client

    @pytest.mark.asyncio
    async def test_toggle_bot_command_enables_and_disables(self, test_session, test_config, event_env):
        """Test that /toggle_bot command enables and disables the bot"""
        bot, _ = event_env
        toggle_bot_handler = bot._handlers["toggle_bot_command_handler"]

        # Create mock event for /toggle_bot command from admin
        mock_event = MagicMock()
        mock_event.chat_id = test_config.tracking_chat_ids[0]
        mock_event.sender_id = test_config.admin_id
        mock_event.raw_text = "/toggle_bot"
        mock_event.reply = AsyncMock()

        # Initially, bot should be enabled (default)
        # First toggle should disable it
        await toggle_bot_handler(mock_event)

        # Check that reply was called with "disabled"
        mock_event.reply.assert_called_once()
        assert "disabled" in mock_event.reply.call_args[0][0]

        # Verify in database that bot is disabled
        group_settings = test_session.query(GroupSettings).filter_by(
            chat_id=test_config.tracking_chat_ids[0]
        ).first()
        assert group_settings is not None
        assert group_settings.enabled is False

        # Reset mock
        mock_event.reply.reset_mock()

        # Second toggle should enable it again
        await toggle_bot_handler(mock_event)

        # Check that reply was called with "enabled"
        mock_event.reply.assert_called_once()
        assert "enabled" in mock_event.reply.call_args[0][0]

        # Verify in database that bot is enabled
        test_session.refresh(group_settings)
        assert group_settings.enabled is True

    @pytest.mark.asyncio
    async def test_toggle_bot_command_rejects_non_admin(self, test_session, test_config, event_env):
        """Test that /toggle_bot command rejects non-admin users"""
        bot, _ = event_env
        toggle_bot_handler = bot._handlers["toggle_bot_command_handler"]

        # Create mock event from non-admin user
        mock_event = MagicMock()
        mock_event.chat_id = test_config.tracking_chat_ids[0]
        mock_event.sender_id = 88888  # Not the admin (admin_id is 99999 in test config)
        mock_event.raw_text = "/toggle_bot"
        mock_event.reply = AsyncMock()

        await toggle_bot_handler(mock_event)

        # Check that reply was called with rejection message
        mock_event.reply.assert_called_once()
        assert "baka" in mock_event.reply.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_chat_action_handler_respects_bot_disabled(self, test_session, test_config, event_env):
        """Test that chat_action_handler ignores events when bot is disabled"""
        bot, _ = event_env
        chat_action_handler = bot._handlers["chat_action_handler"]

        # Disable bot for this chat
        group_settings = GroupSettings(chat_id=test_config.tracking_chat_ids[0], enabled=False)
        test_session.add(group_settings)
        test_session.commit()

        # Create mock event for user join
        mock_event = MagicMock()
        mock_event.chat_id = test_config.tracking_chat_ids[0]
        mock_event.user_joined = True
        mock_event.user_added = False
        mock_event.original_update = MagicMock(spec=UpdateChannelParticipant)
        mock_event.user = MagicMock()
        mock_event.user.id = 12345

        # Call the handler
        await chat_action_handler(mock_event)

        # Verify that user was NOT added to NewUser table
        new_user = test_session.query(NewUser).filter_by(
            user_id=12345, chat_id=test_config.tracking_chat_ids[0]
        ).first()
        assert new_user is None

    @pytest.mark.asyncio
    async def test_chat_action_handler_processes_when_bot_enabled(self, test_session, test_config, event_env):
        """Test that chat_action_handler processes events when bot is enabled"""
        bot, _ = event_env
        chat_action_handler = bot._handlers["chat_action_handler"]

        # Enable bot for this chat (default)
        group_settings = GroupSettings(chat_id=test_config.tracking_chat_ids[0], enabled=True)
        test_session.add(group_settings)
        test_session.commit()

        # Create mock event for user join
        mock_event = MagicMock()
        mock_event.chat_id = test_config.tracking_chat_ids[0]
        mock_event.user_joined = True
        mock_event.user_added = False
        mock_event.original_update = MagicMock(spec=UpdateChannelParticipant)
        mock_event.user = MagicMock()
        mock_event.user.id = 12345

        # Call the handler
        await chat_action_handler(mock_event)

        # Verify that user WAS added to NewUser table
        new_user = test_session.query(NewUser).filter_by(
            user_id=12345, chat_id=test_config.tracking_chat_ids[0]
        ).first()
        assert new_user is not None

    @pytest.mark.asyncio
    async def test_message_handler_respects_bot_disabled(self, test_session, test_config, event_env):
        """Test that message_handler ignores messages when bot is disabled"""
        bot, _ = event_env
        message_handler = bot._handlers["message_handler"]

        # Disable bot for this chat
        group_settings = GroupSettings(chat_id=test_config.tracking_chat_ids[0], enabled=False)
        test_session.add(group_settings)
        test_session.commit()

        # Add user to NewUser table
        new_user = NewUser(user_id=12345, chat_id=test_config.tracking_chat_ids[0])
        test_session.add(new_user)
        test_session.commit()

        # Create mock event for message from new user
        mock_event = MagicMock()
        mock_event.chat_id = test_config.tracking_chat_ids[0]
        mock_event.id = 111
        mock_event.raw_text = "Test message"
        mock_sender = MagicMock()
        mock_sender.id = 12345
        mock_event.get_sender = AsyncMock(return_value=mock_sender)

        # Call the handler
        await message_handler(mock_event)

        # Verify that queue processor was NOT called
        bot.queue_processor.add_message_to_queue.assert_not_called()

    @pytest.mark.asyncio
    async def test_queue_processor_skips_messages_when_bot_disabled(
        self, test_session, mock_llm, mock_userbot, test_config
    ):
        """Test that queue processor skips messages when bot is disabled"""
        # Create mock telegram client
        mock_client = AsyncMock()
        mock_client.send_message = AsyncMock()

        # Create queue processor
        queue_processor = QueueProcessor(
            test_session, mock_llm, mock_userbot, mock_client, test_config
        )

        # Disable bot for this chat
        group_settings = GroupSettings(chat_id=test_config.tracking_chat_ids[0], enabled=False)
        test_session.add(group_settings)

        # Add user to NewUser table
        new_user = NewUser(user_id=12345, chat_id=test_config.tracking_chat_ids[0])
        test_session.add(new_user)

        # Add message to queue
        queue_item = MessageQueue(
            user_id=12345,
            chat_id=test_config.tracking_chat_ids[0],
            message_id=111,
            message_text="Test message",
            status='pending'
        )
        test_session.add(queue_item)
        test_session.commit()

        # Process the message
        await queue_processor._process_message(queue_item, test_session)

        # Verify that message was marked as completed without spam check
        test_session.refresh(queue_item)
        assert queue_item.status == 'completed'
        assert queue_item.error_message == "Bot disabled for this chat"

        # Verify that LLM was NOT called
        mock_llm.is_spam.assert_not_called()

    @pytest.mark.asyncio
    async def test_default_enabled_state_for_new_groups(self, test_session, test_config, event_env):
        """Test that new groups default to enabled state"""
        bot, _ = event_env

        # No GroupSettings entry exists for the chat
        group_settings = test_session.query(GroupSettings).filter_by(
            chat_id=test_config.tracking_chat_ids[0]
        ).first()
        assert group_settings is None

        # Check helper function returns True (enabled by default)
        chat_action_handler = bot._handlers["chat_action_handler"]

        # Create mock event for user join
        mock_event = MagicMock()
        mock_event.chat_id = test_config.tracking_chat_ids[0]
        mock_event.user_joined = True
        mock_event.user_added = False
        mock_event.original_update = MagicMock(spec=UpdateChannelParticipant)
        mock_event.user = MagicMock()
        mock_event.user.id = 12345

        # Call the handler - should process since default is enabled
        await chat_action_handler(mock_event)

        # Verify that user WAS added to NewUser table
        new_user = test_session.query(NewUser).filter_by(
            user_id=12345, chat_id=test_config.tracking_chat_ids[0]
        ).first()
        assert new_user is not None
