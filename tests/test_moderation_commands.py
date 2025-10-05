import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace

from telegram import create_bot
from db import NewUser


@pytest.mark.asyncio
async def test_ban_command_bans_and_cleans_and_logs(test_session, mock_llm, test_config):
    """When /sban N is used in reply, bot bans the user, deletes N messages, and logs to the mapped log channel."""
    # Patch Telethon client used inside create_bot
    with patch('telegram.TelegramClient') as mock_client_cls:
        client = MagicMock()
        client.on = lambda *a, **k: (lambda fn: fn)
        # moderation methods - mock __call__ for TL functions
        client_call = AsyncMock()
        client.side_effect = lambda req: client_call(req)
        client.delete_messages = AsyncMock()
        client.send_message = AsyncMock()
        client.get_entity = AsyncMock()
        client.get_messages = AsyncMock(return_value=[MagicMock(id=i) for i in range(100, 103)])

        mock_client_cls.return_value = client

        # Prepare bot with handlers
        bot = create_bot(test_session, mock_llm, test_config)

        # Ensure handler is exposed
        ban_handler = bot._handlers.get("sban_command_handler")
        assert ban_handler is not None, "sban_command_handler must be exposed on client._handlers"

        # Setup event: /sban 3 as a reply to target's message
        target_user_id = 55555
        group_id = 67890
        log_channel_id = test_config.log_channel_map.get(group_id, test_config.admin_id)

        # Target is considered "new" to be monitored contextually (optional for command)
        test_session.add(NewUser(user_id=target_user_id, chat_id=group_id))
        test_session.commit()

        reply_msg = MagicMock()
        reply_msg.sender_id = target_user_id
        reply_msg.id = 321
        # sban handler awaits reply.get_sender()
        reply_msg.get_sender = AsyncMock(return_value=SimpleNamespace(id=target_user_id))

        event = MagicMock()
        event.chat_id = group_id
        event.sender_id = test_config.admin_id
        event.raw_text = "/sban 3"
        event.reply_to_msg_id = reply_msg.id
        event.get_reply_message = AsyncMock(return_value=reply_msg)
        event.reply = AsyncMock()

        # Run the handler
        await ban_handler(event)

        # Assert ban occurred via TL function call
        client_call.assert_awaited()

        # Assert cleanup attempted for N messages
        client.delete_messages.assert_awaited()

        # Assert logging went to the mapped channel for this group (67891 for group 67890)
        client.send_message.assert_awaited()
        # Find calls where the destination is the expected log channel
        log_calls = [call for call in client.send_message.call_args_list
                     if len(call[0]) > 0 and call[0][0] == log_channel_id]
        assert len(log_calls) > 0, f"Expected at least one log message to {log_channel_id} (mapped channel for group {group_id}), got calls: {client.send_message.call_args_list}"


@pytest.mark.asyncio
async def test_ban_command_requires_reply_or_user_id(test_session, mock_llm, test_config):
    """If /sban is used without a reply or a resolvable arg, the bot should hint usage."""
    with patch('telegram.TelegramClient') as mock_client_cls:
        client = MagicMock()
        client.on = lambda *a, **k: (lambda fn: fn)
        client.side_effect = AsyncMock()
        mock_client_cls.return_value = client

        bot = create_bot(test_session, mock_llm, test_config)
        ban_handler = bot._handlers.get("sban_command_handler")
        assert ban_handler

        event = MagicMock()
        event.chat_id = 67890
        event.sender_id = test_config.admin_id
        event.raw_text = "/sban"
        event.reply_to_msg_id = None
        event.get_reply_message = AsyncMock(return_value=None)
        event.reply = AsyncMock()

        await ban_handler(event)
        event.reply.assert_awaited()
        msg = event.reply.call_args[0][0].lower()
        assert "usage" in msg or "reply" in msg


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