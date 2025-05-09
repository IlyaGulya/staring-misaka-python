# tests/integration/test_spam_flow.py
import logging # Add logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select, func # Added func
from telethon import events # Added events

from staring_misaka import metrics_service as metrics_module # For mocking metrics
from staring_misaka.db_models import (
    BannedUser,
    LLMLog,
    MonitoredGroup,
    NewUser,
    PendingAdminAction,
    QueuedLLMCheck, # Added for one of the new tests
)
from staring_misaka.dto import LLMSpamAnalysisResult
from staring_misaka.event_handlers import EventHandlers # For one of the new tests
from staring_misaka.telegram_utils import get_user_display_name
from tests.conftest import (
    TEST_BOT_ID,
    TEST_CHAT_ID,
    TEST_CHAT_ID_2, # For non-monitored group test
    TEST_NEW_USER_ID,
    TEST_REGULAR_USER_ID, # For approved user test
    TEST_SUPER_ADMIN_ID,
)

pytestmark = pytest.mark.asyncio
test_logger = logging.getLogger(__name__) # Use __name__ for logger


async def test_new_user_sends_spam_auto_ban(
        db_session, mock_telegram_client, event_handlers, action_service,
        monitored_group, new_user_in_group, setup_queue_test, mocker
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

    # Mock labels method for tokens and cost to prevent errors if they are called.
    # Specific inc assertions for these are not primary for this test but ensure they don't break it.
    mocker.patch.object(metrics_module.LLM_TOKENS_USED, 'labels').return_value = MagicMock(inc=MagicMock())
    mocker.patch.object(metrics_module.LLM_ESTIMATED_COST_CENTS, 'labels').return_value = MagicMock(inc=MagicMock())
    # --- End Metric Mocks ---


    prompt, model = setup_queue_test
    expected_model_id = model.id
    expected_prompt_id = prompt.id
    default_provider = model.provider

    group = await db_session.get(MonitoredGroup, TEST_CHAT_ID)
    assert group is not None, f"Monitored group {TEST_CHAT_ID} not found in setup."
    group.require_admin_approval_for_ban = False
    group.pre_ban_message_enabled = True
    group.delete_recent_messages_on_ban = True # Ensure this is True for the test
    group.num_messages_to_delete_on_ban = 1 # Explicitly set to 1
    test_logger.debug(f"Group {TEST_CHAT_ID} configured for auto-ban, delete_recent_messages_on_ban=True, num_messages_to_delete_on_ban=1.")
    await db_session.flush()

    spam_reason = "Obvious advertising link"
    spam_result = LLMSpamAnalysisResult(
        is_spam=True, reason=spam_reason, input_tokens=50, output_tokens=10,
        model_name_used=model.api_identifier, # Use actual model API ID
        status="success"
    )
    llm_service_instance = event_handlers.llm_service
    strategy_instance = llm_service_instance.provider_strategies.get(default_provider)
    assert strategy_instance is not None, f"Strategy for provider '{default_provider}' not found/initialized."
    if strategy_instance.client is None:
        strategy_instance.client = MagicMock()
    mocked_analyze = mocker.patch.object(strategy_instance, 'analyze', return_value=spam_result)

    mock_sender = await mock_telegram_client.get_entity(TEST_NEW_USER_ID)
    message_id = 101
    spam_text = "Check out my amazing site! www.spam.com"
    mock_event = MagicMock(chat_id=TEST_CHAT_ID, id=message_id, text=spam_text, sender_id=TEST_NEW_USER_ID,
                           is_private=False)
    mock_event.get_sender = AsyncMock(return_value=mock_sender)
    event_handlers.monitored_chats_cache = [TEST_CHAT_ID]

    # Act
    test_logger.info(f"Calling event_handlers.new_message_handler for msg {message_id}")
    await event_handlers.new_message_handler(mock_event)
    test_logger.info("Handler finished, session state should be flushed/committed by its context.")

    # Assert Metrics
    mock_messages_processed_labels.assert_called_once_with(chat_id=str(TEST_CHAT_ID))
    mock_messages_processed_inc.assert_called_once_with()
    mock_spam_detected_labels.assert_called_once_with(
        chat_id=str(TEST_CHAT_ID), model_name=model.api_identifier, detection_type="auto"
    )
    mock_spam_detected_inc.assert_called_once_with()
    mock_users_banned_labels.assert_called_once_with(
        chat_id=str(TEST_CHAT_ID), reason_type="auto_spam"
    )
    mock_users_banned_inc.assert_called_once_with()
    mock_llm_api_requests_labels.assert_called_once_with(
        model_name=model.api_identifier, chat_id_label=str(TEST_CHAT_ID)
    )
    mock_llm_api_requests_inc.assert_called_once_with()
    metrics_module.LLM_TOKENS_USED.labels.assert_any_call(model_name=model.api_identifier, token_type="input") # Check it's called
    metrics_module.LLM_TOKENS_USED.labels.return_value.inc.assert_any_call(50) # Check specific value
    metrics_module.LLM_TOKENS_USED.labels.assert_any_call(model_name=model.api_identifier, token_type="output")
    metrics_module.LLM_TOKENS_USED.labels.return_value.inc.assert_any_call(10)
    assert metrics_module.LLM_ESTIMATED_COST_CENTS.labels.called # Check it was called

    mocked_analyze.assert_called_once()

    pre_ban_call_found = any(
        call["args"][0] == TEST_CHAT_ID and call["args"][1] == spam_reason
        for call in mock_telegram_client.sent_messages_log
    )
    assert pre_ban_call_found, f"Pre-ban message '{spam_reason}' not sent to chat {TEST_CHAT_ID}"

    mock_telegram_client.kick_participant.assert_called_once_with(TEST_CHAT_ID, TEST_NEW_USER_ID)
    # Ensure delete_messages is called with the original message_id because num_messages_to_delete_on_ban = 1
    # and group.delete_recent_messages_on_ban = True
    mock_telegram_client.delete_messages.assert_called_once_with(TEST_CHAT_ID, [message_id])


    log_result = await db_session.execute(
        select(LLMLog.llm_is_spam, LLMLog.llm_reason, LLMLog.model_id, LLMLog.prompt_id)
        .where(LLMLog.user_id == TEST_NEW_USER_ID, LLMLog.message_id == message_id)
    )
    log_data = log_result.fetchone()
    assert log_data is not None, f"LLMLog record not found for user {TEST_NEW_USER_ID}, msg {message_id}"
    logged_is_spam, logged_reason, logged_model_id, logged_prompt_id = log_data

    db_session.expire_all() # Expire session state

    # --- Query for other records AFTER expire_all to ensure fresh state ---
    banned_user = await db_session.scalar(
        select(BannedUser).where(BannedUser.user_id == TEST_NEW_USER_ID, BannedUser.chat_id == TEST_CHAT_ID))
    assert banned_user is not None, "BannedUser record not found"
    test_logger.debug(f"Found BannedUser record ID: {banned_user.id}")
    assert banned_user.banned_by_user_id == TEST_BOT_ID
    expected_ban_db_reason = f"Automatic ban: {spam_reason}"
    assert expected_ban_db_reason in banned_user.reason
    assert spam_text[:200] in banned_user.original_message_text_sample

    new_user_check = await db_session.get(NewUser, {"user_id": TEST_NEW_USER_ID, "chat_id": TEST_CHAT_ID})
    assert new_user_check is None, "NewUser record was not deleted after ban"

    # --- Assert log attributes using variables fetched before expire_all ---
    test_logger.debug("Asserting LLMLog attributes fetched before expire_all")
    assert logged_is_spam is True
    assert logged_reason == spam_reason
    assert logged_model_id == expected_model_id
    assert logged_prompt_id == expected_prompt_id

    admin_notification_found = any(
        call["args"][0] == TEST_SUPER_ADMIN_ID and f"User {TEST_NEW_USER_ID} has been banned" in call["args"][1]
        for call in mock_telegram_client.sent_messages_log
    )
    assert admin_notification_found, "Admin notification for ban not found"
    test_logger.info("Finished test_new_user_sends_spam_auto_ban.")


async def test_new_user_sends_spam_admin_approval(
        db_session, mock_telegram_client, event_handlers, action_service, command_handlers,
        monitored_group, new_user_in_group, setup_queue_test, mocker
):
    """
    GIVEN a new user in a monitored group configured for admin approval
    WHEN the user sends spam AND the admin approves the ban via reply
    THEN the user should be banned and records updated.
    """
    test_logger.info("Starting test_new_user_sends_spam_admin_approval...")
    # Arrange Metrics
    mock_messages_processed_labels = mocker.patch.object(metrics_module.MESSAGES_PROCESSED, 'labels')
    mock_messages_processed_inc = MagicMock()
    mock_messages_processed_labels.return_value = MagicMock(inc=mock_messages_processed_inc)

    mock_spam_detected_labels = mocker.patch.object(metrics_module.SPAM_DETECTED, 'labels')
    mock_spam_detected_inc = MagicMock() # For initial detection
    mock_spam_detected_labels.return_value = MagicMock(inc=mock_spam_detected_inc)

    mock_users_banned_labels = mocker.patch.object(metrics_module.USERS_BANNED, 'labels')
    mock_users_banned_inc = MagicMock() # For admin approval ban
    mock_users_banned_labels.return_value = MagicMock(inc=mock_users_banned_inc)

    mock_llm_api_requests_labels = mocker.patch.object(metrics_module.LLM_API_REQUESTS, 'labels')
    mock_llm_api_requests_inc = MagicMock()
    mock_llm_api_requests_labels.return_value = MagicMock(inc=mock_llm_api_requests_inc)

    mocker.patch.object(metrics_module.LLM_TOKENS_USED, 'labels').return_value = MagicMock(inc=MagicMock())
    mocker.patch.object(metrics_module.LLM_ESTIMATED_COST_CENTS, 'labels').return_value = MagicMock(inc=MagicMock())


    prompt, model = setup_queue_test
    default_provider = model.provider

    group = await db_session.get(MonitoredGroup, TEST_CHAT_ID)
    assert group is not None, f"Monitored group {TEST_CHAT_ID} not found in setup."
    group.require_admin_approval_for_ban = True
    group.num_messages_to_delete_on_ban = 1 # ensure message gets deleted
    group.delete_recent_messages_on_ban = True # ensure message gets deleted
    test_logger.debug(f"Group {TEST_CHAT_ID} configured for admin approval.")
    await db_session.flush()

    spam_reason = "Suspicious crypto link"
    spam_result = LLMSpamAnalysisResult(is_spam=True, reason=spam_reason, model_name_used=model.api_identifier,
                                        status="success") # Use actual model_api_identifier
    llm_service_instance = event_handlers.llm_service
    strategy_instance = llm_service_instance.provider_strategies.get(default_provider)
    assert strategy_instance is not None, f"Strategy for provider '{default_provider}' not found/initialized."
    if strategy_instance.client is None:
        strategy_instance.client = MagicMock()
    mocked_analyze = mocker.patch.object(strategy_instance, 'analyze', return_value=spam_result)

    mock_sender = await mock_telegram_client.get_entity(TEST_NEW_USER_ID)
    message_id = 102
    spam_text = "Free coins at cryptospam.io!"
    mock_event_msg = MagicMock(chat_id=TEST_CHAT_ID, id=message_id, text=spam_text, sender_id=TEST_NEW_USER_ID,
                               is_private=False)
    mock_event_msg.get_sender = AsyncMock(return_value=mock_sender)
    event_handlers.monitored_chats_cache = [TEST_CHAT_ID]

    # Act 1: New message triggers LLM check and admin notification
    test_logger.info(f"Calling event_handlers.new_message_handler for msg {message_id} (needs approval)")
    await event_handlers.new_message_handler(mock_event_msg)
    test_logger.info("Handler finished, session state should be flushed/committed by its context.")


    # Assert 1: Admin notification sent, pending action created
    mock_messages_processed_labels.assert_called_once_with(chat_id=str(TEST_CHAT_ID))
    mock_messages_processed_inc.assert_called_once_with()
    mock_spam_detected_labels.assert_called_once_with(
        chat_id=str(TEST_CHAT_ID), model_name=model.api_identifier, detection_type="auto"
    )
    mock_spam_detected_inc.assert_called_once_with() # Initial detection
    mock_llm_api_requests_labels.assert_called_once_with(
        model_name=model.api_identifier, chat_id_label=str(TEST_CHAT_ID)
    )
    mock_llm_api_requests_inc.assert_called_once_with()
    mocked_analyze.assert_called_once() # Check LLM was called

    user_display_name_for_assertion = await get_user_display_name(mock_sender)
    expected_admin_notification_content_partial = (
        f"Potential Spam Alert:\n"
        f"User: {user_display_name_for_assertion} (ID: {TEST_NEW_USER_ID})\n"
        f"Chat ID: {TEST_CHAT_ID}\n"
        f"Message: \"{spam_text[:200]}...\"\n"
        f"LLM Reason: {spam_reason}"
    )

    admin_notification_call = None
    test_logger.debug(f"Sent messages log: {mock_telegram_client.sent_messages_log}")
    for call_info in mock_telegram_client.sent_messages_log:
        args, kwargs_call = call_info["args"], call_info["kwargs"] # Renamed kwargs to kwargs_call
        # Check if message sent TO admin and contains expected content
        if len(args) > 1 and args[0] == TEST_SUPER_ADMIN_ID and expected_admin_notification_content_partial in args[1]:
            admin_notification_call = call_info
            break
    assert admin_notification_call is not None, f"Admin notification for approval not found. Expected partial: {expected_admin_notification_content_partial}"

    log_result_stage1 = await db_session.execute(
        select(LLMLog.llm_is_spam, LLMLog.llm_reason)
        .where(LLMLog.user_id == TEST_NEW_USER_ID, LLMLog.message_id == message_id)
    )
    log_data_stage1 = log_result_stage1.fetchone()
    assert log_data_stage1 is not None
    logged_is_spam_s1, logged_reason_s1 = log_data_stage1

    db_session.expire_all() # Expire session state

    pending_action = await db_session.scalar(
        select(PendingAdminAction)
        .where(PendingAdminAction.user_to_act_on_id == TEST_NEW_USER_ID)
        .where(PendingAdminAction.original_message_id == message_id)
    )
    assert pending_action is not None, "PendingAdminAction not created or found after handler"
    admin_notification_msg_id_for_reply = pending_action.admin_message_id
    pending_action_id = pending_action.id # Store ID before potential deletion

    assert logged_is_spam_s1 is True
    assert logged_reason_s1 == spam_reason

    # Arrange 2: Prepare mock admin reply event
    mock_admin_reply = MagicMock(
        is_private=True, sender_id=TEST_SUPER_ADMIN_ID,
        reply_to_msg_id=admin_notification_msg_id_for_reply,
        text="yes", reply=AsyncMock()
    )

    # Act 2: Admin replies 'yes'
    test_logger.info(f"Calling command_handlers.admin_reply_handler for reply to {admin_notification_msg_id_for_reply}")
    await command_handlers.admin_reply_handler(mock_admin_reply)
    test_logger.info("Handler finished, session state should be flushed/committed by its context.")


    # Assert 2: Ban processed, pending action deleted
    # Check SPAM_DETECTED and USERS_BANNED metrics after admin approval
    mock_spam_detected_labels.assert_any_call(chat_id=str(TEST_CHAT_ID), model_name="AdminOverride", detection_type="admin_approved")
    # mock_spam_detected_inc should have been called twice (once auto, once admin_approved)
    assert mock_spam_detected_inc.call_count == 2

    mock_users_banned_labels.assert_called_once_with(chat_id=str(TEST_CHAT_ID), reason_type="admin_decision") # This is set by action_service
    mock_users_banned_inc.assert_called_once_with()

    mock_telegram_client.kick_participant.assert_called_once_with(TEST_CHAT_ID, TEST_NEW_USER_ID)
    mock_telegram_client.delete_messages.assert_called_once_with(TEST_CHAT_ID, [message_id])

    db_session.expire_all() # Expire session state again

    pending_action_after = await db_session.get(PendingAdminAction, pending_action_id)
    assert pending_action_after is None, "PendingAdminAction was not deleted after processing"

    banned_user = await db_session.scalar(
        select(BannedUser).where(BannedUser.user_id == TEST_NEW_USER_ID, BannedUser.chat_id == TEST_CHAT_ID))
    assert banned_user is not None, "BannedUser record not created after admin approval"
    assert f"Admin approved ban. Original LLM reason: {spam_reason}" in banned_user.reason

    new_user_check = await db_session.get(NewUser, {"user_id": TEST_NEW_USER_ID, "chat_id": TEST_CHAT_ID})
    assert new_user_check is None, "NewUser record was not deleted after admin-approved ban"

    mock_admin_reply.reply.assert_called_with(f"Ban processed for user {TEST_NEW_USER_ID}.")
    test_logger.info("Finished test_new_user_sends_spam_admin_approval.")


async def test_new_user_sends_non_spam(
        db_session, mock_telegram_client, event_handlers,
        monitored_group, new_user_in_group, setup_queue_test, mocker
):
    """
    GIVEN a new user in a monitored group
    WHEN the user sends a message identified as NOT spam by the LLM
    THEN the user should be approved (removed from NewUser table) and LLMLog created.
    """
    test_logger.info("Starting test_new_user_sends_non_spam...")
    # Arrange Metrics
    mock_messages_processed_labels = mocker.patch.object(metrics_module.MESSAGES_PROCESSED, 'labels')
    mock_messages_processed_inc = MagicMock()
    mock_messages_processed_labels.return_value = MagicMock(inc=mock_messages_processed_inc)

    mock_spam_detected_labels = mocker.patch.object(metrics_module.SPAM_DETECTED, 'labels') # Should not be called

    mock_llm_api_requests_labels = mocker.patch.object(metrics_module.LLM_API_REQUESTS, 'labels')
    mock_llm_api_requests_inc = MagicMock()
    mock_llm_api_requests_labels.return_value = MagicMock(inc=mock_llm_api_requests_inc)

    mocker.patch.object(metrics_module.LLM_TOKENS_USED, 'labels').return_value = MagicMock(inc=MagicMock())
    mocker.patch.object(metrics_module.LLM_ESTIMATED_COST_CENTS, 'labels').return_value = MagicMock(inc=MagicMock())

    prompt, model = setup_queue_test
    expected_model_id = model.id
    expected_prompt_id = prompt.id
    default_provider = model.provider

    non_spam_reason = "General discussion"
    non_spam_result = LLMSpamAnalysisResult(is_spam=False, reason=non_spam_reason,
                                            model_name_used=model.api_identifier, status="success") # Use actual model_api_identifier
    llm_service_instance = event_handlers.llm_service
    strategy_instance = llm_service_instance.provider_strategies.get(default_provider)
    assert strategy_instance is not None
    if strategy_instance.client is None:
        strategy_instance.client = MagicMock()
    mocked_analyze = mocker.patch.object(strategy_instance, 'analyze', return_value=non_spam_result)

    mock_sender = await mock_telegram_client.get_entity(TEST_NEW_USER_ID)
    message_id = 103
    ok_text = "Hello everyone, interesting topic!"
    mock_event_msg = MagicMock(chat_id=TEST_CHAT_ID, id=message_id, text=ok_text, sender_id=TEST_NEW_USER_ID,
                               is_private=False)
    mock_event_msg.get_sender = AsyncMock(return_value=mock_sender)
    event_handlers.monitored_chats_cache = [TEST_CHAT_ID]

    # Act
    test_logger.info(f"Calling event_handlers.new_message_handler for non-spam msg {message_id}")
    await event_handlers.new_message_handler(mock_event_msg)
    test_logger.info("Handler finished, session state should be flushed/committed by its context.")


    # Assert Metrics
    mock_messages_processed_labels.assert_called_once_with(chat_id=str(TEST_CHAT_ID))
    mock_messages_processed_inc.assert_called_once_with()
    mock_spam_detected_labels.assert_not_called() # Non-spam
    mock_llm_api_requests_labels.assert_called_once_with(
        model_name=model.api_identifier, chat_id_label=str(TEST_CHAT_ID)
    )
    mock_llm_api_requests_inc.assert_called_once_with()
    mocked_analyze.assert_called_once() # Check LLM was called
    mock_telegram_client.kick_participant.assert_not_called()
    mock_telegram_client.delete_messages.assert_not_called()

    admin_notification_call_found = any(
        call["args"][0] == TEST_SUPER_ADMIN_ID and "Potential Spam Alert" in call["args"][1]
        for call in mock_telegram_client.sent_messages_log
    )
    assert not admin_notification_call_found, "Admin approval should not have been requested for non-spam."

    log_result = await db_session.execute(
        select(LLMLog.llm_is_spam, LLMLog.llm_reason, LLMLog.model_id, LLMLog.prompt_id)
        .where(LLMLog.user_id == TEST_NEW_USER_ID, LLMLog.message_id == message_id)
    )
    log_data = log_result.fetchone()
    assert log_data is not None, f"LLMLog record not found for user {TEST_NEW_USER_ID}, msg {message_id}"
    logged_is_spam, logged_reason, logged_model_id, logged_prompt_id = log_data

    db_session.expire_all() # Expire session state

    new_user_check = await db_session.get(NewUser, {"user_id": TEST_NEW_USER_ID, "chat_id": TEST_CHAT_ID})
    assert new_user_check is None, "NewUser record was not deleted for non-spam message"

    assert logged_is_spam is False
    assert logged_reason == non_spam_reason
    assert logged_model_id == expected_model_id
    assert logged_prompt_id == expected_prompt_id

    test_logger.info("Finished test_new_user_sends_non_spam.")


async def test_user_joins_monitored_group(db_session, event_handlers, monitored_group, mock_telegram_client):
    """
    GIVEN a monitored group
    WHEN a new user joins the group
    THEN a NewUser record should be created for them.
    """
    test_logger.info("Starting test_user_joins_monitored_group...")
    # Arrange
    user_to_join_id = TEST_NEW_USER_ID + 1 # A different user ID

    # Ensure this user isn't already in NewUser for this chat
    existing_new_user = await db_session.get(NewUser, {"user_id": user_to_join_id, "chat_id": TEST_CHAT_ID})
    assert existing_new_user is None

    mock_event_join = MagicMock(spec=events.ChatAction.Event) # Use events.ChatAction
    mock_event_join.chat_id = TEST_CHAT_ID
    mock_event_join.user_id = user_to_join_id
    mock_event_join.user_added = True # Or .user_joined = True
    mock_event_join.user_joined = False # Telethon usually sets one or the other
    mock_event_join.action_message = MagicMock() # Avoid AttributeError if accessed

    # Ensure the chat is in the cache for the handler
    event_handlers.monitored_chats_cache = [TEST_CHAT_ID]

    # Act
    test_logger.info(f"Calling event_handlers.chat_action_handler for user join event (user {user_to_join_id})")
    await event_handlers.chat_action_handler(mock_event_join)
    test_logger.info("Handler finished.")

    # Assert
    db_session.expire_all() # Ensure fresh read from DB
    new_user_record = await db_session.get(NewUser, {"user_id": user_to_join_id, "chat_id": TEST_CHAT_ID})
    assert new_user_record is not None, f"NewUser record for user {user_to_join_id} in chat {TEST_CHAT_ID} not created."
    assert new_user_record.user_id == user_to_join_id
    assert new_user_record.chat_id == TEST_CHAT_ID
    assert new_user_record.join_time is not None

    test_logger.info("Finished test_user_joins_monitored_group.")


async def test_message_from_approved_user_or_bot_or_unmonitored_group(
        db_session, mock_telegram_client, event_handlers: EventHandlers, # Type hint for clarity
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
    mock_analyze_spam = mocker.patch.object(event_handlers.llm_service, 'analyze_message_for_spam', new_callable=AsyncMock)

    # Case 1: Approved User (not in NewUser table)
    test_logger.info("Testing case 1: Approved User")
    mock_sender_approved = await mock_telegram_client.get_entity(TEST_REGULAR_USER_ID) # Regular user is not new
    message_id_case1 = 211 # Changed from 201 to avoid conflict
    mock_event_approved = MagicMock(
        chat_id=TEST_CHAT_ID, id=message_id_case1, text="Hello from approved user",
        sender_id=TEST_REGULAR_USER_ID, is_private=False
    )
    mock_event_approved.get_sender = AsyncMock(return_value=mock_sender_approved)
    event_handlers.monitored_chats_cache = [TEST_CHAT_ID] # Ensure monitored

    await event_handlers.new_message_handler(mock_event_approved)
    mock_analyze_spam.assert_not_called()
    mock_messages_processed_labels.assert_called_once_with(chat_id=str(TEST_CHAT_ID)) # Called once for this message
    mock_messages_processed_inc.assert_called_once_with()
    mock_messages_processed_labels.reset_mock() # Reset for next case
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
    # User TEST_BOT_ID + 1 might be in NewUser if not cleared, but sender.bot should override
    # For robustness, ensure they are not in NewUser for this specific test case if it matters.
    # However, the `if not sender or sender.bot:` check in handler should catch it first.

    await event_handlers.new_message_handler(mock_event_bot)
    mock_analyze_spam.assert_not_called() # Still not called
    # MESSAGES_PROCESSED is NOT called if sender is a bot, due to early return in handler.
    mock_messages_processed_labels.assert_not_called() # Corrected
    mock_messages_processed_inc.assert_not_called() # Corrected
    mock_messages_processed_labels.reset_mock()
    mock_messages_processed_inc.reset_mock()


    # Case 3: Message in Unmonitored Group
    test_logger.info("Testing case 3: Unmonitored Group")
    mock_sender_new_in_unmonitored = await mock_telegram_client.get_entity(TEST_NEW_USER_ID) # A "new" user
    # Ensure user is in NewUser for this unmonitored chat to test the _is_chat_monitored branch correctly
    unmonitored_chat_id = TEST_CHAT_ID_2
    # db_session.add(NewUser(user_id=TEST_NEW_USER_ID, chat_id=unmonitored_chat_id)) # Not strictly needed, as chat monitoring is the primary check
    # await db_session.flush()
    message_id_case3 = 213
    mock_event_unmonitored = MagicMock(
        chat_id=unmonitored_chat_id, id=message_id_case3, text="Hello from new user in unmonitored chat",
        sender_id=TEST_NEW_USER_ID, is_private=False
    )
    mock_event_unmonitored.get_sender = AsyncMock(return_value=mock_sender_new_in_unmonitored)
    # event_handlers.monitored_chats_cache is [TEST_CHAT_ID], so unmonitored_chat_id is not in it.
    # Ensure the cache is explicitly set for this part of the test if it could have been modified by other cases.
    event_handlers.monitored_chats_cache = [TEST_CHAT_ID]


    await event_handlers.new_message_handler(mock_event_unmonitored)
    mock_analyze_spam.assert_not_called() # Still not called
    # MESSAGES_PROCESSED.inc() is NOT called if chat is not monitored due to early return.
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