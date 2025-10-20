import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace

from telegram import create_bot
from db import NewUser


@pytest.mark.parametrize("command,is_reply,expected_purge_count,target_user_id,should_succeed,description", [
    # Reply mode with default purge count (default is 25)
    ("/sban", True, 25, 55555, True, "reply with default purge count"),
    # User ID with default purge count (default is 25)
    ("/sban 55555", False, 25, 55555, True, "user_id with default purge count"),
    # User ID with explicit purge count
    ("/sban 55555 10", False, 10, 55555, True, "user_id with explicit purge count"),
    # Username with default purge count (default is 25)
    ("/sban @testuser", False, 25, 55555, True, "username with default purge count"),
    # Username with explicit purge count
    ("/sban @testuser 15", False, 15, 55555, True, "username with explicit purge count"),
])
@pytest.mark.asyncio
async def test_ban_command_various_invocations(test_session, mock_llm, test_config, command, is_reply, expected_purge_count, target_user_id, should_succeed, description):
    """Test /sban command with various invocation methods."""
    # Patch Telethon client used inside create_bot
    with patch('telegram.TelegramClient') as mock_client_cls:
        client = MagicMock()
        client.on = lambda *a, **k: (lambda fn: fn)
        # moderation methods - mock __call__ for TL functions
        client_call = AsyncMock()
        client.side_effect = lambda req: client_call(req)
        client.delete_messages = AsyncMock()
        client.send_message = AsyncMock()
        client.get_messages = AsyncMock(return_value=[MagicMock(id=i) for i in range(100, 103)])

        # Mock get_entity to return user entity
        mock_user_entity = SimpleNamespace(id=target_user_id)
        client.get_entity = AsyncMock(return_value=mock_user_entity)

        mock_client_cls.return_value = client

        # Prepare bot with handlers
        bot = create_bot(test_session, mock_llm, test_config)

        # Ensure handler is exposed
        ban_handler = bot._handlers.get("sban_command_handler")
        assert ban_handler is not None, "sban_command_handler must be exposed on client._handlers"

        # Setup event
        group_id = 67890
        log_channel_id = test_config.log_channel_map.get(group_id, test_config.admin_id)

        # Target is considered "new" to be monitored contextually (optional for command)
        test_session.add(NewUser(user_id=target_user_id, chat_id=group_id))
        test_session.commit()

        event = MagicMock()
        event.chat_id = group_id
        event.sender_id = test_config.admin_id
        event.raw_text = command
        event.reply = AsyncMock()

        # Setup pattern_match for command argument parsing (matches the actual handler pattern)
        import re
        pattern = r'^/(sban|ban)(?:\s+(.+))?'
        match = re.match(pattern, command)
        event.pattern_match = match

        if is_reply:
            # Setup reply message
            reply_msg = MagicMock()
            reply_msg.sender_id = target_user_id
            reply_msg.id = 321
            reply_msg.get_sender = AsyncMock(return_value=SimpleNamespace(id=target_user_id))

            event.is_reply = True
            event.reply_to_msg_id = reply_msg.id
            event.get_reply_message = AsyncMock(return_value=reply_msg)
        else:
            event.is_reply = False
            event.reply_to_msg_id = None
            event.get_reply_message = AsyncMock(return_value=None)

        # Run the handler
        await ban_handler(event)

        if should_succeed:
            # Assert ban occurred via TL function call
            client_call.assert_awaited()

            # Assert cleanup attempted for expected N messages
            client.delete_messages.assert_awaited()

            # Assert logging went to the mapped channel for this group
            client.send_message.assert_awaited()
            # Find calls where the destination is the expected log channel
            log_calls = [call for call in client.send_message.call_args_list
                         if len(call[0]) > 0 and call[0][0] == log_channel_id]
            assert len(log_calls) > 0, f"Expected at least one log message to {log_channel_id} (mapped channel for group {group_id}), got calls: {client.send_message.call_args_list}"

            # Verify the success reply message mentions the correct purge count
            success_reply_calls = [call for call in event.reply.call_args_list
                                   if "Banned" in str(call)]
            assert len(success_reply_calls) > 0, f"Expected success reply for {description}"
            assert str(expected_purge_count) in str(success_reply_calls[0]), f"Expected purge count {expected_purge_count} in reply for {description}"


@pytest.mark.parametrize("command,is_reply,expected_error_keyword,description", [
    # No reply and no arguments
    ("/sban", False, "usage", "no reply and no arguments"),
    # Invalid user ID
    ("/sban invalidid", False, "resolve", "invalid user ID"),
    # Invalid username
    ("/sban @nonexistentuser", False, "resolve", "invalid username"),
])
@pytest.mark.asyncio
async def test_ban_command_error_cases(test_session, mock_llm, test_config, command, is_reply, expected_error_keyword, description):
    """Test /sban command error handling for invalid inputs."""
    with patch('telegram.TelegramClient') as mock_client_cls:
        client = MagicMock()
        client.on = lambda *a, **k: (lambda fn: fn)
        client.side_effect = AsyncMock()

        # Mock get_entity to raise exception for invalid users
        client.get_entity = AsyncMock(side_effect=Exception("User not found"))

        mock_client_cls.return_value = client

        bot = create_bot(test_session, mock_llm, test_config)
        ban_handler = bot._handlers.get("sban_command_handler")
        assert ban_handler

        event = MagicMock()
        event.chat_id = 67890
        event.sender_id = test_config.admin_id
        event.raw_text = command
        event.reply = AsyncMock()

        # Setup pattern_match for command argument parsing (matches the actual handler pattern)
        import re
        pattern = r'^/(sban|ban)(?:\s+(.+))?'
        match = re.match(pattern, command)
        event.pattern_match = match

        if is_reply:
            event.is_reply = True
            event.reply_to_msg_id = 321
            reply_msg = MagicMock()
            event.get_reply_message = AsyncMock(return_value=reply_msg)
        else:
            event.is_reply = False
            event.reply_to_msg_id = None
            event.get_reply_message = AsyncMock(return_value=None)

        await ban_handler(event)

        # Assert that an error reply was sent
        event.reply.assert_awaited()
        msg = event.reply.call_args[0][0].lower()
        assert expected_error_keyword in msg, f"Expected '{expected_error_keyword}' in error message for {description}, got: {msg}"


@pytest.mark.asyncio
async def test_ban_command_non_admin_rejected(test_session, mock_llm, test_config):
    """Non-admin users cannot ban."""
    with patch('telegram.TelegramClient') as mock_client_cls:
        client = MagicMock()
        client.on = lambda *a, **k: (lambda fn: fn)
        mock_client_cls.return_value = client

        bot = create_bot(test_session, mock_llm, test_config)
        ban_handler = bot._handlers.get("sban_command_handler")
        assert ban_handler

        event = MagicMock()
        event.chat_id = 67890
        event.sender_id = 123  # not admin
        event.raw_text = "/sban 1"
        event.reply = AsyncMock()

        await ban_handler(event)
        event.reply.assert_awaited()
        assert "don't touch me" in event.reply.call_args[0][0].lower() or "baka" in event.reply.call_args[0][0].lower()