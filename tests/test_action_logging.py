import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, UTC

from queue_processor import QueueProcessor
from db import MessageQueue, NewUser, AdminSettings


@pytest.mark.asyncio
async def test_spam_autoban_logs_to_group_channel(test_session, mock_llm, mock_telegram_client, test_config):
    """QueueProcessor should log its actions to the configured log channel per group."""
    # Setup: automatic ban (no approval)
    admin_settings = test_session.query(AdminSettings).first()
    admin_settings.require_approval = False
    test_session.commit()

    # Message in queue + corresponding monitored user
    queue_item = MessageQueue(
        user_id=7777, chat_id=67890, message_id=4242,
        message_text="Spam link http://bad", status='pending'
    )
    test_session.add(queue_item)
    test_session.add(NewUser(user_id=7777, chat_id=67890))
    test_session.commit()

    # LLM says spam
    mock_llm.is_spam = AsyncMock(return_value=True)
    mock_telegram_client.delete_messages = AsyncMock()
    mock_telegram_client.send_message = AsyncMock()
    # Mock user entity with proper attributes
    mock_user = MagicMock()
    mock_user.username = "spammer"
    mock_user.first_name = "Spam User"
    mock_telegram_client.get_entity = AsyncMock(return_value=mock_user)
    mock_telegram_client.get_messages = AsyncMock(return_value=[])

    processor = QueueProcessor(test_session, mock_llm, mock_telegram_client, test_config)
    await processor._process_message(queue_item)

    # Check we logged to the mapped channel or admin DM
    log_channel = test_config.log_channel_map.get(67890, test_config.admin_id)
    mock_telegram_client.send_message.assert_awaited()
    # Find the log message call
    log_calls = [call for call in mock_telegram_client.send_message.call_args_list
                 if len(call[0]) > 0 and call[0][0] == log_channel]
    assert len(log_calls) > 0, f"Expected at least one log message to {log_channel}, got calls: {mock_telegram_client.send_message.call_args_list}"