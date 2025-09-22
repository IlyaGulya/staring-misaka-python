import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, UTC

from telethon import events
from telethon.tl.types import UpdateChannelParticipant

from telegram import create_bot
from db import NewUser, ApprovedUser, MessageQueue
from config import Config


class TestBotEventHandlers:
    """Test bot event handlers (chat_action_handler, notspam_command_handler)"""

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

            bot = create_bot(test_session, mock_llm, mock_userbot, test_config, mock_queue_processor)
            return bot, mock_client
    
    @pytest.mark.asyncio
    async def test_chat_action_handler_user_joined(self, test_session, test_config, event_env):
        """Test chat_action_handler when user joins"""
        bot, _ = event_env
        chat_action_handler = bot._handlers["chat_action_handler"]
        
        assert chat_action_handler is not None
        
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
        
        # Verify user was added to NewUser table
        new_user = test_session.query(NewUser).filter_by(
            user_id=12345,
            chat_id=test_config.tracking_chat_ids[0]
        ).first()
        
        assert new_user is not None
        assert new_user.user_id == 12345
        assert new_user.chat_id == test_config.tracking_chat_ids[0]
    
    @pytest.mark.asyncio
    async def test_chat_action_handler_user_added(self, test_session, test_config, event_env):
        """Test chat_action_handler when user is added"""
        bot, _ = event_env
        chat_action_handler = bot._handlers["chat_action_handler"]
        
        assert chat_action_handler is not None
        
        # Create mock event for user added
        mock_event = MagicMock()
        mock_event.chat_id = test_config.tracking_chat_ids[0]
        mock_event.user_joined = False
        mock_event.user_added = True
        mock_event.original_update = MagicMock(spec=UpdateChannelParticipant)
        mock_event.user = MagicMock()
        mock_event.user.id = 67890
        
        # Call the handler
        await chat_action_handler(mock_event)
        
        # Verify user was added to NewUser table
        new_user = test_session.query(NewUser).filter_by(
            user_id=67890,
            chat_id=test_config.tracking_chat_ids[0]
        ).first()
        
        assert new_user is not None
        assert new_user.user_id == 67890
        assert new_user.chat_id == test_config.tracking_chat_ids[0]
    
    @pytest.mark.asyncio
    async def test_chat_action_handler_pre_approved_user(self, test_session, test_config, event_env):
        """Test chat_action_handler skips pre-approved users"""
        bot, _ = event_env
        chat_action_handler = bot._handlers["chat_action_handler"]
        
        assert chat_action_handler is not None
        
        # Create pre-approved user
        approved_user = ApprovedUser(
            user_id=99999,
            chat_id=test_config.tracking_chat_ids[0]
        )
        test_session.add(approved_user)
        test_session.commit()
        
        # Create mock event
        mock_event = MagicMock()
        mock_event.chat_id = test_config.tracking_chat_ids[0]
        mock_event.user_joined = True
        mock_event.user_added = False
        mock_event.original_update = MagicMock(spec=UpdateChannelParticipant)
        mock_event.user = MagicMock()
        mock_event.user.id = 99999
        
        # Call the handler
        await chat_action_handler(mock_event)
        
        # Verify user was NOT added to NewUser table
        new_user = test_session.query(NewUser).filter_by(
            user_id=99999,
            chat_id=test_config.tracking_chat_ids[0]
        ).first()
        
        assert new_user is None
    
    @pytest.mark.asyncio
    async def test_chat_action_handler_existing_user_updates_join_time(self, test_session, test_config, event_env):
        """Test chat_action_handler updates join_time for existing users"""
        bot, _ = event_env
        chat_action_handler = bot._handlers["chat_action_handler"]
        
        assert chat_action_handler is not None
        
        # Create existing user with old join time
        old_time = datetime(2023, 1, 1, tzinfo=UTC)
        existing_user = NewUser(
            user_id=11111,
            chat_id=test_config.tracking_chat_ids[0]
        )
        existing_user.join_time = old_time
        test_session.add(existing_user)
        test_session.commit()
        
        # Create mock event
        mock_event = MagicMock()
        mock_event.chat_id = test_config.tracking_chat_ids[0]
        mock_event.user_joined = True
        mock_event.user_added = False
        mock_event.original_update = MagicMock(spec=UpdateChannelParticipant)
        mock_event.user = MagicMock()
        mock_event.user.id = 11111
        
        # Call the handler
        await chat_action_handler(mock_event)
        
        # Verify join time was updated
        test_session.refresh(existing_user)
        # Convert to UTC for comparison since the handler uses UTC
        updated_time = existing_user.join_time.replace(tzinfo=UTC) if existing_user.join_time.tzinfo is None else existing_user.join_time
        assert updated_time > old_time
    
    @pytest.mark.asyncio
    async def test_chat_action_handler_non_tracked_chat(self, test_session, test_config, event_env):
        """Test chat_action_handler ignores events from non-tracked chats"""
        bot, _ = event_env
        chat_action_handler = bot._handlers["chat_action_handler"]
        
        assert chat_action_handler is not None
        
        # Create mock event from non-tracked chat
        mock_event = MagicMock()
        mock_event.chat_id = 999999  # Not in tracking_chat_ids
        mock_event.user_joined = True
        mock_event.user_added = False
        mock_event.original_update = MagicMock(spec=UpdateChannelParticipant)
        mock_event.user = MagicMock()
        mock_event.user.id = 55555
        
        # Call the handler
        await chat_action_handler(mock_event)
        
        # Verify no user was added
        new_user_count = test_session.query(NewUser).count()
        assert new_user_count == 0
    
    @pytest.mark.asyncio
    async def test_notspam_command_handler_admin_approves_user(self, test_session, test_config, event_env):
        """Test /notspam command handler when admin approves user"""
        bot, mock_client = event_env
        notspam_handler = bot._handlers["notspam_command_handler"]

        # Create a new user to approve
        new_user = NewUser(
            user_id=77777,
            chat_id=test_config.tracking_chat_ids[0]
        )
        test_session.add(new_user)
        test_session.commit()

        # Prepare event
        mock_event = MagicMock()
        mock_event.sender_id = test_config.admin_id
        mock_event.chat_id = test_config.tracking_chat_ids[0]
        mock_event.raw_text = "/notspam 77777"
        mock_event.reply = AsyncMock()

        # Client.get_entity should resolve the user for nicer messaging
        mock_client.get_entity = AsyncMock(return_value=MagicMock(id=77777, username="testuser", first_name="Test User"))

        # Run
        await notspam_handler(mock_event)

        # Assert: user removed from monitoring and added to approved
        from db import NewUser as NU, ApprovedUser as AU
        assert test_session.query(NU).filter_by(user_id=77777, chat_id=test_config.tracking_chat_ids[0]).first() is None
        assert test_session.query(AU).filter_by(user_id=77777, chat_id=test_config.tracking_chat_ids[0]).first() is not None
        mock_event.reply.assert_called()
    
    @pytest.mark.asyncio
    async def test_notspam_command_handler_non_admin_rejected(self, test_session, test_config, event_env):
        """Test /notspam command handler rejects non-admin users"""
        bot, _ = event_env
        notspam_handler = bot._handlers["notspam_command_handler"]
        # Create mock event for non-admin user
        mock_event = MagicMock()
        mock_event.sender_id = 12345  # Not the admin_id
        mock_event.chat_id = test_config.tracking_chat_ids[0]
        mock_event.raw_text = "/notspam 77777"
        mock_event.reply = AsyncMock()
        await notspam_handler(mock_event)
        mock_event.reply.assert_called()
    
    @pytest.mark.asyncio
    async def test_notspam_command_handler_invalid_usage(self, test_session, test_config, event_env):
        """Test /notspam command handler with invalid usage"""
        bot, _ = event_env
        notspam_handler = bot._handlers["notspam_command_handler"]
        # Create mock event with missing user identifier
        mock_event = MagicMock()
        mock_event.sender_id = test_config.admin_id
        mock_event.chat_id = test_config.tracking_chat_ids[0]
        mock_event.raw_text = "/notspam"  # Missing user identifier
        mock_event.reply = AsyncMock()
        await notspam_handler(mock_event)
        mock_event.reply.assert_called()