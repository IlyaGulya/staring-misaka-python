# tests/integration/test_spam_flow.py
import logging  # Add logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select, func  # Added func
from telethon import events  # Added events

from staring_misaka import metrics_service as metrics_module  # For mocking metrics
from staring_misaka.config import Settings  # For EventHandlers construction
from staring_misaka.db_models import (
    BannedUser,
    LLMLog,
    MonitoredGroup,
    NewUser,
    PendingAdminAction,
    QueuedLLMCheck,  # Added for one of the new tests
)
from staring_misaka.dto import LLMSpamAnalysisResult
from staring_misaka.event_handlers import EventHandlers  # For one of the new tests
from staring_misaka.telegram_utils import get_user_display_name
from tests.conftest import (
    TEST_BOT_ID,
    TEST_CHAT_ID,
    TEST_CHAT_ID_2,  # For non-monitored group test
    TEST_NEW_USER_ID,
    TEST_REGULAR_USER_ID,  # For approved user test
    TEST_SUPER_ADMIN_ID,
    SPAM_MESSAGE_TEXT, NON_SPAM_MESSAGE_TEXT,  # Import text constants
    assert_user_banned_with_details, assert_user_approved,  # Import helpers
    setup_monitored_group_with_config,  # Import specific fixture
)

pytestmark = pytest.mark.asyncio
test_logger = logging.getLogger(__name__)  # Use __name__ for logger


async def test_new_user_sends_spam_auto_ban(
        db_session, mock_telegram_client, event_handlers, action_service,  # Use event_handlers fixture
        monitored_group: MonitoredGroup, new_user_in_group, setup_queue_test, mocker, test_settings: Settings
):
    """
    GIVEN a new user in a monitored group configured for auto-ban
    WHEN the user sends a message identified as spam by the LLM
    THEN the user should be banned, the message deleted, and records updated.
    """
    test_logger.info("Starting test_new_user_sends_spam_auto_ban...")
    # Arrange
    # --- Metric Mocks ---
    mock_messages_processed_labels = mocker.patch.object(metrics_module.MESSAGES_PROCESSED, 'labels')
    mock_messages_processed_inc = MagicMock()
    mock_messages_processed_labels.return_value = MagicMock(inc=mock_messages_processed_inc)

    mock_spam_detected_labels = mocker.patch.object(metrics_module.SPAM_DETECTED, 'labels')
    mock_spam_detected_inc = MagicMock()
    mock_spam_detected_labels.return_value = MagicMock(inc=mock_spam_detected_inc)

    mock_users_banned_labels = mocker.patch.object(metrics_module.USERS_BANNED, 'labels')
    mock_users_banned_inc = MagicMock()
    mock_users_banned_labels.return_value = MagicMock(inc=mock_users_banned_inc)

    mock_llm_api_requests_labels = mocker.patch.object(metrics_module.LLM_API_REQUESTS, 'labels')
    mock_llm_api_requests_inc = MagicMock()
    mock_llm_api_requests_labels.return_value = MagicMock(inc=mock_llm_api_requests_inc)

    mocker.patch.object(metrics_module.LLM_TOKENS_USED, 'labels').return_value = MagicMock(inc=MagicMock())
    mocker.patch.object(metrics_module.LLM_ESTIMATED_COST_CENTS, 'labels').return_value = MagicMock(inc=MagicMock())
    # --- End Metric Mocks ---

    # Use event_handlers fixture which has real_llm_service
    event_handlers.monitored_chats_cache = [TEST_CHAT_ID]  # Set cache

    prompt, model = setup_queue_test
    default_provider = model.provider
    # Capture IDs before potential expiry
    expected_model_id = model.id
    expected_prompt_id = prompt.id

    # Ensure group is configured for auto-ban
    monitored_group.require_admin_approval_for_ban = False
    monitored_group.pre_ban_message_enabled = True
    monitored_group.delete_recent_messages_on_ban = True
    monitored_group.num_messages_to_delete_on_ban = 1
    await db_session.flush()

    # Mock the strategy's analyze method
    spam_reason_text = "LLM says this is spam via strategy mock"
    spam_result_dto = LLMSpamAnalysisResult(
        is_spam=True, reason=spam_reason_text,
        input_tokens=20, output_tokens=5, model_name_used=model.api_identifier, status="success"
    )
    llm_service_instance = event_handlers.llm_service
    strategy_instance = llm_service_instance.provider_strategies.get(default_provider)
    assert strategy_instance is not None, f"Strategy for provider '{default_provider}' not found."
    if strategy_instance.client is None: strategy_instance.client = MagicMock()  # Should be initialized by LLMService
    mocker.patch.object(strategy_instance, 'analyze', return_value=spam_result_dto)

    mock_sender = await mock_telegram_client.get_entity(TEST_NEW_USER_ID)
    message_id = 101
    mock_event = MagicMock(chat_id=TEST_CHAT_ID, id=message_id, text=SPAM_MESSAGE_TEXT, sender_id=TEST_NEW_USER_ID,
                           is_private=False)
    mock_event.get_sender = AsyncMock(return_value=mock_sender)

    # Act
    test_logger.info(f"Calling event_handlers.new_message_handler for msg {message_id}")
    await event_handlers.new_message_handler(mock_event)

    # Assert Metrics
    mock_messages_processed_labels.assert_called_once_with(chat_id=str(TEST_CHAT_ID))
    mock_spam_detected_labels.assert_called_once_with(
        chat_id=str(TEST_CHAT_ID), model_name=model.api_identifier, detection_type="auto"
    )
    mock_users_banned_labels.assert_called_once_with(
        chat_id=str(TEST_CHAT_ID), reason_type="auto_spam"
    )
    mock_llm_api_requests_labels.assert_called_once_with(
        model_name=model.api_identifier, chat_id_label=str(TEST_CHAT_ID)
    )
    mock_llm_api_requests_inc.assert_called_once()  # ensure .inc() was called

    # Assert ban details using helper
    expected_ban_reason_db = f"Automatic ban: {spam_reason_text}"
    await assert_user_banned_with_details(
        db_session, mock_telegram_client, TEST_NEW_USER_ID, TEST_CHAT_ID,
        expected_reason_substring=spam_reason_text,
        expected_bot_id=TEST_BOT_ID,
        expected_deleted_message_ids=[message_id]
    )

    # Assert pre-ban message sent
    pre_ban_call_found = any(
        call["args"][0] == TEST_CHAT_ID and call["args"][1] == spam_reason_text
        for call in mock_telegram_client.sent_messages_log
    )
    assert pre_ban_call_found, f"Pre-ban message '{spam_reason_text}' not sent to chat {TEST_CHAT_ID}"

    # Assert LLMLog (after db_session.expire_all() in assert_user_banned_with_details)
    # Re-fetch LLMLog to ensure it's not expired.
    db_session.expire_all()
    log_entry = await db_session.scalar(
        select(LLMLog)
        .where(LLMLog.user_id == TEST_NEW_USER_ID, LLMLog.message_id == message_id)
    )
    assert log_entry is not None
    assert log_entry.llm_is_spam is True
    assert log_entry.llm_reason == spam_reason_text
    assert log_entry.model_id == expected_model_id
    assert log_entry.prompt_id == expected_prompt_id

    admin_notification_found = any(
        call["args"][0] == TEST_SUPER_ADMIN_ID and f"User {TEST_NEW_USER_ID} has been banned" in call["args"][1]
        for call in mock_telegram_client.sent_messages_log
    )
    assert admin_notification_found, "Admin notification for ban not found"
    test_logger.info("Finished test_new_user_sends_spam_auto_ban.")


@pytest.mark.usefixtures("setup_monitored_group_with_config")  # Uses new fixture for config
async def test_new_user_sends_spam_admin_approval(
        db_session, mock_telegram_client, event_handlers, action_service, command_handlers,
        # event_handlers uses real_llm_service
        new_user_in_group, setup_queue_test, mocker, test_settings: Settings
):
    """
    GIVEN a new user in a monitored group configured for admin approval
    WHEN the user sends spam AND the admin approves the ban via reply
    THEN the user should be banned and records updated.
    """
    test_logger.info("Starting test_new_user_sends_spam_admin_approval...")
    # Arrange Metrics
    # (Metric mocks as in original test are fine)
    mocker.patch.object(metrics_module.MESSAGES_PROCESSED, 'labels').return_value = MagicMock(inc=MagicMock())
    mocker.patch.object(metrics_module.SPAM_DETECTED, 'labels').return_value = MagicMock(inc=MagicMock())
    mocker.patch.object(metrics_module.USERS_BANNED, 'labels').return_value = MagicMock(inc=MagicMock())
    mocker.patch.object(metrics_module.LLM_API_REQUESTS, 'labels').return_value = MagicMock(inc=MagicMock())
    mocker.patch.object(metrics_module.LLM_TOKENS_USED, 'labels').return_value = MagicMock(inc=MagicMock())
    mocker.patch.object(metrics_module.LLM_ESTIMATED_COST_CENTS, 'labels').return_value = MagicMock(inc=MagicMock())

    prompt, model = setup_queue_test
    default_provider = model.provider

    # setup_monitored_group_with_config fixture ensures require_admin_approval_for_ban = True by default
    group = await db_session.get(MonitoredGroup, TEST_CHAT_ID)
    assert group.require_admin_approval_for_ban is True
    group.num_messages_to_delete_on_ban = 1
    group.delete_recent_messages_on_ban = True
    await db_session.flush()

    spam_reason = "Suspicious crypto link"
    spam_result_dto = LLMSpamAnalysisResult(is_spam=True, reason=spam_reason, model_name_used=model.api_identifier,
                                            status="success")

    llm_service_instance = event_handlers.llm_service  # from fixture, uses real_llm_service
    strategy_instance = llm_service_instance.provider_strategies.get(default_provider)
    assert strategy_instance is not None
    if strategy_instance.client is None: strategy_instance.client = MagicMock()
    mocker.patch.object(strategy_instance, 'analyze', return_value=spam_result_dto)

    mock_sender = await mock_telegram_client.get_entity(TEST_NEW_USER_ID)
    message_id = 102  # Unique message ID
    mock_event_msg = MagicMock(chat_id=TEST_CHAT_ID, id=message_id, text=SPAM_MESSAGE_TEXT, sender_id=TEST_NEW_USER_ID,
                               is_private=False)
    mock_event_msg.get_sender = AsyncMock(return_value=mock_sender)
    event_handlers.monitored_chats_cache = [TEST_CHAT_ID]  # Ensure cache

    # Act 1: New message triggers LLM check and admin notification
    await event_handlers.new_message_handler(mock_event_msg)

    # Assert 1: Admin notification sent, pending action created
    user_display_name = await get_user_display_name(mock_sender)
    expected_admin_notification_text_part = (
        f"Potential Spam Alert:\nUser: {user_display_name} (ID: {TEST_NEW_USER_ID})\n"
        f"Chat ID: {TEST_CHAT_ID}\nMessage: \"{SPAM_MESSAGE_TEXT[:200]}...\"\nLLM Reason: {spam_reason}"
    )
    admin_notification_call = next(
        (call for call in mock_telegram_client.sent_messages_log
         if call["args"][0] == TEST_SUPER_ADMIN_ID and expected_admin_notification_text_part in call["args"][1]),
        None
    )
    assert admin_notification_call is not None, "Admin notification for approval not found or content mismatch."

    pending_action = await db_session.scalar(
        select(PendingAdminAction)
        .where(PendingAdminAction.user_to_act_on_id == TEST_NEW_USER_ID,
               PendingAdminAction.original_message_id == message_id)
    )
    assert pending_action is not None, "PendingAdminAction not created."
    admin_reply_to_msg_id = pending_action.admin_message_id
    pending_action_id = pending_action.id  # Store ID before potential deletion

    # Arrange 2: Admin reply
    mock_admin_reply = MagicMock(
        is_private=True, sender_id=TEST_SUPER_ADMIN_ID,
        reply_to_msg_id=admin_reply_to_msg_id, text="yes", reply=AsyncMock()
    )

    # Act 2: Admin replies 'yes'
    await command_handlers.admin_reply_handler(mock_admin_reply)

    # Assert 2: Ban processed
    expected_ban_reason_db = f"Admin approved ban. Original LLM reason: {spam_reason}"
    await assert_user_banned_with_details(
        db_session, mock_telegram_client, TEST_NEW_USER_ID, TEST_CHAT_ID,
        expected_reason_substring="Admin approved ban",
        expected_bot_id=TEST_BOT_ID,
        expected_deleted_message_ids=[message_id]
    )

    # Check PendingAdminAction was deleted (after db_session.expire_all() in assert_user_banned_with_details)
    pending_action_after = await db_session.get(PendingAdminAction,
                                                pending_action_id)  # Fetch again with the test's session
    assert pending_action_after is None, "PendingAdminAction was not deleted."

    mock_admin_reply.reply.assert_called_with(f"Ban processed for user {TEST_NEW_USER_ID}.")
    test_logger.info("Finished test_new_user_sends_spam_admin_approval.")


async def test_new_user_sends_non_spam(
        db_session, mock_telegram_client, event_handlers,  # event_handlers uses real_llm_service
        monitored_group, new_user_in_group, setup_queue_test, mocker, test_settings: Settings
):
    """
    GIVEN a new user in a monitored group
    WHEN the user sends a message identified as NOT spam by the LLM
    THEN the user should be approved (removed from NewUser table) and LLMLog created.
    """
    test_logger.info("Starting test_new_user_sends_non_spam...")
    # Arrange Metrics (similar to other tests, simplified here)
    mocker.patch.object(metrics_module.MESSAGES_PROCESSED, 'labels').return_value = MagicMock(inc=MagicMock())
    mocker.patch.object(metrics_module.SPAM_DETECTED, 'labels')  # Should not be called for non-spam
    mocker.patch.object(metrics_module.LLM_API_REQUESTS, 'labels').return_value = MagicMock(inc=MagicMock())

    prompt, model = setup_queue_test
    default_provider = model.provider
    # Capture IDs before potential expiry
    expected_model_id = model.id
    expected_prompt_id = prompt.id

    non_spam_reason = "General discussion"
    non_spam_result_dto = LLMSpamAnalysisResult(is_spam=False, reason=non_spam_reason,
                                                model_name_used=model.api_identifier, status="success")

    llm_service_instance = event_handlers.llm_service
    strategy_instance = llm_service_instance.provider_strategies.get(default_provider)
    assert strategy_instance is not None
    if strategy_instance.client is None: strategy_instance.client = MagicMock()
    mocker.patch.object(strategy_instance, 'analyze', return_value=non_spam_result_dto)

    mock_sender = await mock_telegram_client.get_entity(TEST_NEW_USER_ID)
    message_id = 103  # Unique message ID
    mock_event_msg = MagicMock(chat_id=TEST_CHAT_ID, id=message_id, text=NON_SPAM_MESSAGE_TEXT,
                               sender_id=TEST_NEW_USER_ID, is_private=False)
    mock_event_msg.get_sender = AsyncMock(return_value=mock_sender)
    event_handlers.monitored_chats_cache = [TEST_CHAT_ID]

    # Act
    await event_handlers.new_message_handler(mock_event_msg)

    # Assert
    await assert_user_approved(db_session, TEST_NEW_USER_ID, TEST_CHAT_ID)

    mock_telegram_client.kick_participant.assert_not_called()
    mock_telegram_client.delete_messages.assert_not_called()

    # Verify admin was not notified for approval
    admin_notification_call_found = any(
        call["args"][0] == TEST_SUPER_ADMIN_ID and "Potential Spam Alert" in call["args"][1]
        for call in mock_telegram_client.sent_messages_log
    )
    assert not admin_notification_call_found, "Admin approval should not have been requested for non-spam."

    # Verify LLMLog (after db_session.expire_all() in assert_user_approved)
    db_session.expire_all()
    log_entry = await db_session.scalar(
        select(LLMLog)
        .where(LLMLog.user_id == TEST_NEW_USER_ID, LLMLog.message_id == message_id)
    )
    assert log_entry is not None
    assert log_entry.llm_is_spam is False
    assert log_entry.llm_reason == non_spam_reason
    assert log_entry.model_id == expected_model_id
    assert log_entry.prompt_id == expected_prompt_id

    test_logger.info("Finished test_new_user_sends_non_spam.")


async def test_user_joins_monitored_group(db_session, event_handlers, monitored_group, mock_telegram_client):
    """
    GIVEN a monitored group
    WHEN a new user joins the group
    THEN a NewUser record should be created for them.
    """
    test_logger.info("Starting test_user_joins_monitored_group...")
    # Arrange
    user_to_join_id = TEST_NEW_USER_ID + 1  # A different user ID

    # Ensure this user isn't already in NewUser for this chat
    existing_new_user = await db_session.get(NewUser, {"user_id": user_to_join_id, "chat_id": TEST_CHAT_ID})
    assert existing_new_user is None

    mock_event_join = MagicMock(spec=events.ChatAction.Event)  # Use events.ChatAction
    mock_event_join.chat_id = TEST_CHAT_ID
    mock_event_join.user_id = user_to_join_id
    mock_event_join.user_added = True  # Or .user_joined = True
    mock_event_join.user_joined = False  # Telethon usually sets one or the other
    mock_event_join.action_message = MagicMock()  # Avoid AttributeError if accessed

    # Ensure the chat is in the cache for the handler
    event_handlers.monitored_chats_cache = [TEST_CHAT_ID]

    # Act
    test_logger.info(f"Calling event_handlers.chat_action_handler for user join event (user {user_to_join_id})")
    await event_handlers.chat_action_handler(mock_event_join)
    test_logger.info("Handler finished.")

    # Assert
    db_session.expire_all()  # Ensure fresh read from DB
    new_user_record = await db_session.get(NewUser, {"user_id": user_to_join_id, "chat_id": TEST_CHAT_ID})
    assert new_user_record is not None, f"NewUser record for user {user_to_join_id} in chat {TEST_CHAT_ID} not created."
    assert new_user_record.user_id == user_to_join_id
    assert new_user_record.chat_id == TEST_CHAT_ID
    assert new_user_record.join_time is not None

    test_logger.info("Finished test_user_joins_monitored_group.")


async def test_message_from_approved_user_or_bot_or_unmonitored_group(
        db_session, mock_telegram_client, event_handlers: EventHandlers,  # Type hint for clarity
        monitored_group, setup_queue_test, mocker
):
    """
    GIVEN a message is received
    WHEN the sender is an approved user (not in NewUser table), OR a bot, OR the chat is not monitored
    THEN LLM analysis should NOT be performed.
    """
    test_logger.info("Starting test_message_from_approved_user_or_bot_or_unmonitored_group...")
    # --- Metric Mocks ---
    mock_messages_processed_labels = mocker.patch.object(metrics_module.MESSAGES_PROCESSED, 'labels')
    mock_messages_processed_inc = MagicMock()
    mock_messages_processed_labels.return_value = MagicMock(inc=mock_messages_processed_inc)
    # --- End Metric Mocks ---

    # Mock the LLM service's analyze method to ensure it's not called
    # event_handlers uses real_llm_service. We need to mock the actual call point.
    mock_analyze_spam = mocker.patch.object(event_handlers.llm_service, 'analyze_message_for_spam',
                                            new_callable=AsyncMock)

    # Case 1: Approved User (not in NewUser table)
    test_logger.info("Testing case 1: Approved User")
    mock_sender_approved = await mock_telegram_client.get_entity(TEST_REGULAR_USER_ID)  # Regular user is not new
    message_id_case1 = 211
    mock_event_approved = MagicMock(
        chat_id=TEST_CHAT_ID, id=message_id_case1, text="Hello from approved user",
        sender_id=TEST_REGULAR_USER_ID, is_private=False
    )
    mock_event_approved.get_sender = AsyncMock(return_value=mock_sender_approved)
    event_handlers.monitored_chats_cache = [TEST_CHAT_ID]  # Ensure monitored

    await event_handlers.new_message_handler(mock_event_approved)
    mock_analyze_spam.assert_not_called()
    mock_messages_processed_labels.assert_called_once_with(chat_id=str(TEST_CHAT_ID))
    mock_messages_processed_inc.assert_called_once_with()
    mock_messages_processed_labels.reset_mock()
    mock_messages_processed_inc.reset_mock()

    # Case 2: Bot User
    test_logger.info("Testing case 2: Bot User")
    mock_sender_bot = MagicMock(id=TEST_BOT_ID + 1, bot=True, username="AnotherTestBot")
    message_id_case2 = 212
    mock_event_bot = MagicMock(
        chat_id=TEST_CHAT_ID, id=message_id_case2, text="Hello from bot user",
        sender_id=mock_sender_bot.id, is_private=False
    )
    mock_event_bot.get_sender = AsyncMock(return_value=mock_sender_bot)

    await event_handlers.new_message_handler(mock_event_bot)
    mock_analyze_spam.assert_not_called()
    mock_messages_processed_labels.assert_not_called()
    mock_messages_processed_inc.assert_not_called()
    mock_messages_processed_labels.reset_mock()
    mock_messages_processed_inc.reset_mock()

    # Case 3: Message in Unmonitored Group
    test_logger.info("Testing case 3: Unmonitored Group")
    mock_sender_new_in_unmonitored = await mock_telegram_client.get_entity(TEST_NEW_USER_ID)
    unmonitored_chat_id = TEST_CHAT_ID_2
    message_id_case3 = 213
    mock_event_unmonitored = MagicMock(
        chat_id=unmonitored_chat_id, id=message_id_case3, text="Hello from new user in unmonitored chat",
        sender_id=TEST_NEW_USER_ID, is_private=False
    )
    mock_event_unmonitored.get_sender = AsyncMock(return_value=mock_sender_new_in_unmonitored)
    event_handlers.monitored_chats_cache = [TEST_CHAT_ID]  # Ensure unmonitored_chat_id is not in cache

    await event_handlers.new_message_handler(mock_event_unmonitored)
    mock_analyze_spam.assert_not_called()
    mock_messages_processed_labels.assert_not_called()
    mock_messages_processed_inc.assert_not_called()

    # Check no new LLMLog entries were created for these messages
    log_for_event1 = await db_session.scalar(select(LLMLog).where(LLMLog.message_id == message_id_case1))
    assert log_for_event1 is None
    log_for_event2 = await db_session.scalar(select(LLMLog).where(LLMLog.message_id == message_id_case2))
    assert log_for_event2 is None
    log_for_event3 = await db_session.scalar(select(LLMLog).where(LLMLog.message_id == message_id_case3))
    assert log_for_event3 is None

    test_logger.info("Finished test_message_from_approved_user_or_bot_or_unmonitored_group.")