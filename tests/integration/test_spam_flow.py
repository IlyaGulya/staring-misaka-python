from unittest.mock import AsyncMock, MagicMock
import logging  # Add logging

import pytest
from sqlalchemy import select

from staring_misaka.db_models import BannedUser, LLMLog, MonitoredGroup, NewUser, PendingAdminAction
from staring_misaka.dto import LLMSpamAnalysisResult, MessageContext
from staring_misaka.telegram_utils import get_user_display_name
from tests.conftest import TEST_BOT_ID, TEST_CHAT_ID, TEST_NEW_USER_ID, TEST_SUPER_ADMIN_ID

pytestmark = pytest.mark.asyncio
test_logger = logging.getLogger(__name__)


async def test_new_user_sends_spam_auto_ban(
        db_session, mock_telegram_client, event_handlers, action_service,
        monitored_group, new_user_in_group, setup_queue_test, mocker # Add setup_queue_test and mocker
):
    """
    GIVEN a new user in a monitored group configured for auto-ban
    WHEN the user sends a message identified as spam by the LLM
    THEN the user should be banned, the message deleted, and records updated.
    """
    test_logger.info("Starting test_new_user_sends_spam_auto_ban...")
    # Arrange
    prompt, model = setup_queue_test # Ensures default prompt/model exist
    # --- FIX: Store IDs before potential expire_all ---
    expected_model_id = model.id
    expected_prompt_id = prompt.id
    # -------------------------------------------------
    default_provider = model.provider # Get the provider from the setup model

    group = await db_session.get(MonitoredGroup, TEST_CHAT_ID)
    assert group is not None, f"Monitored group {TEST_CHAT_ID} not found in setup."
    group.require_admin_approval_for_ban = False
    group.pre_ban_message_enabled = True
    group.num_messages_to_delete_on_ban = 1
    test_logger.debug(f"Group {TEST_CHAT_ID} configured for auto-ban.")
    await db_session.flush()

    spam_reason = "Obvious advertising link"
    spam_result = LLMSpamAnalysisResult(
        is_spam=True, reason=spam_reason, input_tokens=50, output_tokens=10,
        model_name_used="mock-spam-detector", # This will be overwritten by real service if successful
        status="success"
    )
    # Mock the API call within the real LLM service used by event_handlers
    llm_service_instance = event_handlers.llm_service
    strategy_instance = llm_service_instance.provider_strategies.get(default_provider)
    assert strategy_instance is not None, f"Strategy for provider '{default_provider}' not found/initialized."

    # --- Ensure the service thinks the client is initialized ---
    if strategy_instance.client is None:
        strategy_instance.client = MagicMock()
        test_logger.debug(f"Patched strategy_instance.client for provider {default_provider} to bypass initialization check.")
    # ----------------------------------------------------------------

    # Mock the actual 'analyze' method which makes the external call
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
    test_logger.info(f"Handler finished, session state should be flushed/committed by its context.")


    # Assert
    mocked_analyze.assert_called_once()

    pre_ban_call_found = any(
        call["args"][0] == TEST_CHAT_ID and call["args"][1] == spam_reason
        for call in mock_telegram_client.sent_messages_log
    )
    assert pre_ban_call_found, f"Pre-ban message '{spam_reason}' not sent to chat {TEST_CHAT_ID}"

    mock_telegram_client.kick_participant.assert_called_once_with(TEST_CHAT_ID, TEST_NEW_USER_ID)
    mock_telegram_client.delete_messages.assert_called_once_with(TEST_CHAT_ID, [message_id])

    # --- Fetch scalar log attributes BEFORE expire_all ---
    test_logger.debug(f"Querying for LLMLog attributes user={TEST_NEW_USER_ID}, msg={message_id}")
    log_result = await db_session.execute(
        select(LLMLog.llm_is_spam, LLMLog.llm_reason, LLMLog.model_id, LLMLog.prompt_id)
        .where(LLMLog.user_id == TEST_NEW_USER_ID, LLMLog.message_id == message_id)
    )
    log_data = log_result.fetchone()
    assert log_data is not None, f"LLMLog record not found for user {TEST_NEW_USER_ID}, msg {message_id}"
    logged_is_spam, logged_reason, logged_model_id, logged_prompt_id = log_data
    # ---------------------------------------------------------

    db_session.expire_all() # Expire session state

    # --- Query for other records AFTER expire_all to ensure fresh state ---
    test_logger.debug(f"Querying for BannedUser user={TEST_NEW_USER_ID}, chat={TEST_CHAT_ID}")
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
    test_logger.debug(f"Asserting LLMLog attributes fetched before expire_all")
    assert logged_is_spam is True
    assert logged_reason == spam_reason
    assert logged_model_id == expected_model_id # Check correct model was logged
    assert logged_prompt_id == expected_prompt_id # Check correct prompt was logged
    # ----------------------------------------------------------------------

    admin_notification_found = any(
        call["args"][0] == TEST_SUPER_ADMIN_ID and f"User {TEST_NEW_USER_ID} has been banned" in call["args"][1]
        for call in mock_telegram_client.sent_messages_log
    )
    assert admin_notification_found, "Admin notification for ban not found"
    test_logger.info("Finished test_new_user_sends_spam_auto_ban.")


async def test_new_user_sends_spam_admin_approval(
        db_session, mock_telegram_client, event_handlers, action_service, command_handlers,
        monitored_group, new_user_in_group, setup_queue_test, mocker # Add setup_queue_test and mocker
):
    """
    GIVEN a new user in a monitored group configured for admin approval
    WHEN the user sends spam AND the admin approves the ban via reply
    THEN the user should be banned and records updated.
    """
    test_logger.info("Starting test_new_user_sends_spam_admin_approval...")
    # Arrange
    prompt, model = setup_queue_test # Ensures default prompt/model exist
    # --- FIX: Store IDs before potential expire_all ---
    expected_model_id = model.id
    expected_prompt_id = prompt.id
    # -------------------------------------------------
    default_provider = model.provider # Get the provider from the setup model

    group = await db_session.get(MonitoredGroup, TEST_CHAT_ID)
    assert group is not None, f"Monitored group {TEST_CHAT_ID} not found in setup."
    group.require_admin_approval_for_ban = True
    test_logger.debug(f"Group {TEST_CHAT_ID} configured for admin approval.")
    await db_session.flush()

    spam_reason = "Suspicious crypto link"
    spam_result = LLMSpamAnalysisResult(is_spam=True, reason=spam_reason, model_name_used="mock-detector-v2",
                                        status="success")
    # Mock the API call within the real LLM service used by event_handlers
    llm_service_instance = event_handlers.llm_service
    strategy_instance = llm_service_instance.provider_strategies.get(default_provider)
    assert strategy_instance is not None, f"Strategy for provider '{default_provider}' not found/initialized."

    # --- Ensure the service thinks the client is initialized ---
    if strategy_instance.client is None:
        strategy_instance.client = MagicMock()
        test_logger.debug(f"Patched strategy_instance.client for provider {default_provider} to bypass initialization check.")
    # ----------------------------------------------------------------

    # Mock the actual 'analyze' method which makes the external call
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
    test_logger.info(f"Handler finished, session state should be flushed/committed by its context.")


    # Assert 1: Admin notification sent, pending action created
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
        args, _ = call_info["args"], call_info["kwargs"]
        # Check if message sent TO admin and contains expected content
        if len(args) > 1 and args[0] == TEST_SUPER_ADMIN_ID and expected_admin_notification_content_partial in args[1]:
            admin_notification_call = call_info
            break
    assert admin_notification_call is not None, f"Admin notification for approval not found. Expected partial: {expected_admin_notification_content_partial}"

    # --- Fetch scalar log attributes BEFORE expire_all ---
    test_logger.debug(f"Querying for LLMLog attributes user={TEST_NEW_USER_ID}, msg={message_id} (stage 1)")
    log_result_stage1 = await db_session.execute(
        select(LLMLog.llm_is_spam, LLMLog.llm_reason)
        .where(LLMLog.user_id == TEST_NEW_USER_ID, LLMLog.message_id == message_id)
    )
    log_data_stage1 = log_result_stage1.fetchone()
    assert log_data_stage1 is not None, f"LLMLog record not found after stage 1 for user {TEST_NEW_USER_ID}, msg {message_id}"
    logged_is_spam_s1, logged_reason_s1 = log_data_stage1
    # ---------------------------------------------------------

    db_session.expire_all() # Expire session state

    # --- Query for other records AFTER expire_all ---
    test_logger.debug(f"Querying for PendingAdminAction user={TEST_NEW_USER_ID}, msg={message_id}")
    pending_action = await db_session.scalar(
        select(PendingAdminAction)
        .where(PendingAdminAction.user_to_act_on_id == TEST_NEW_USER_ID)
        .where(PendingAdminAction.original_message_id == message_id)
    )
    assert pending_action is not None, "PendingAdminAction not created or found after handler"
    test_logger.debug(
        f"Found PendingAdminAction ID: {pending_action.id}, Admin Msg ID: {pending_action.admin_message_id}")
    admin_notification_msg_id_for_reply = pending_action.admin_message_id
    pending_action_id = pending_action.id # Store ID before potential deletion

    # --- Assert log attributes using variables fetched before expire_all ---
    test_logger.debug(f"Asserting LLMLog attributes from stage 1")
    assert logged_is_spam_s1 is True
    assert logged_reason_s1 == spam_reason
    # ----------------------------------------------------------------------

    # Arrange 2: Prepare mock admin reply event
    mock_admin_reply = MagicMock(
        is_private=True, sender_id=TEST_SUPER_ADMIN_ID,
        reply_to_msg_id=admin_notification_msg_id_for_reply,
        text="yes", reply=AsyncMock()
    )

    # Act 2: Admin replies 'yes'
    test_logger.info(f"Calling command_handlers.admin_reply_handler for reply to {admin_notification_msg_id_for_reply}")
    await command_handlers.admin_reply_handler(mock_admin_reply)
    test_logger.info(f"Handler finished, session state should be flushed/committed by its context.")


    # Assert 2: Ban processed, pending action deleted
    mock_telegram_client.kick_participant.assert_called_once_with(TEST_CHAT_ID, TEST_NEW_USER_ID)
    mock_telegram_client.delete_messages.assert_called_once_with(TEST_CHAT_ID, [message_id])

    db_session.expire_all() # Expire session state again

    # --- Query for records AFTER expire_all and second action ---
    test_logger.debug(f"Querying for PendingAdminAction ID {pending_action_id} after processing")
    pending_action_after = await db_session.get(PendingAdminAction, pending_action_id)
    assert pending_action_after is None, "PendingAdminAction was not deleted after processing"

    test_logger.debug(f"Querying for BannedUser user={TEST_NEW_USER_ID}, chat={TEST_CHAT_ID} after approval")
    banned_user = await db_session.scalar(
        select(BannedUser).where(BannedUser.user_id == TEST_NEW_USER_ID, BannedUser.chat_id == TEST_CHAT_ID))
    assert banned_user is not None, "BannedUser record not created after admin approval"
    test_logger.debug(f"Found BannedUser record ID: {banned_user.id}")
    assert f"Admin approved ban. Original LLM reason: {spam_reason}" in banned_user.reason

    new_user_check = await db_session.get(NewUser, {"user_id": TEST_NEW_USER_ID, "chat_id": TEST_CHAT_ID})
    assert new_user_check is None, "NewUser record was not deleted after admin-approved ban"

    test_logger.info("Finished test_new_user_sends_spam_admin_approval.")


async def test_new_user_sends_non_spam(
        db_session, mock_telegram_client, event_handlers,
        monitored_group, new_user_in_group, setup_queue_test, mocker # Add setup_queue_test and mocker
):
    """
    GIVEN a new user in a monitored group
    WHEN the user sends a message identified as NOT spam by the LLM
    THEN the user should be approved (removed from NewUser table) and LLMLog created.
    """
    test_logger.info("Starting test_new_user_sends_non_spam...")
    # Arrange
    prompt, model = setup_queue_test # Ensures default prompt/model exist
    # --- FIX: Store IDs before potential expire_all ---
    expected_model_id = model.id
    expected_prompt_id = prompt.id
    # -------------------------------------------------
    default_provider = model.provider # Get the provider from the setup model

    non_spam_reason = "General discussion"
    non_spam_result = LLMSpamAnalysisResult(is_spam=False, reason=non_spam_reason,
                                            model_name_used="mock-detector-v3", status="success")
    # Mock the API call within the real LLM service used by event_handlers
    llm_service_instance = event_handlers.llm_service
    strategy_instance = llm_service_instance.provider_strategies.get(default_provider)
    assert strategy_instance is not None, f"Strategy for provider '{default_provider}' not found/initialized."

    # --- Ensure the service thinks the client is initialized ---
    if strategy_instance.client is None:
        strategy_instance.client = MagicMock()
        test_logger.debug(f"Patched strategy_instance.client for provider {default_provider} to bypass initialization check.")
    # ----------------------------------------------------------------

    # Mock the actual 'analyze' method which makes the external call
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
    test_logger.info(f"Handler finished, session state should be flushed/committed by its context.")


    # Assert
    mocked_analyze.assert_called_once() # Check LLM was called
    mock_telegram_client.kick_participant.assert_not_called()
    mock_telegram_client.delete_messages.assert_not_called()

    admin_notification_call_found = any(
        call["args"][0] == TEST_SUPER_ADMIN_ID and "Potential Spam Alert" in call["args"][1]
        for call in mock_telegram_client.sent_messages_log
    )
    assert not admin_notification_call_found, "Admin approval should not have been requested for non-spam."

    # --- Fetch scalar log attributes BEFORE expire_all ---
    test_logger.debug(f"Querying for LLMLog attributes user={TEST_NEW_USER_ID}, msg={message_id} (non-spam)")
    log_result = await db_session.execute(
        select(LLMLog.llm_is_spam, LLMLog.llm_reason, LLMLog.model_id, LLMLog.prompt_id)
        .where(LLMLog.user_id == TEST_NEW_USER_ID, LLMLog.message_id == message_id)
    )
    log_data = log_result.fetchone()
    assert log_data is not None, f"LLMLog record not found for user {TEST_NEW_USER_ID}, msg {message_id}"
    logged_is_spam, logged_reason, logged_model_id, logged_prompt_id = log_data
    # ---------------------------------------------------------

    db_session.expire_all() # Expire session state

    # --- Query for other records AFTER expire_all ---
    new_user_check = await db_session.get(NewUser, {"user_id": TEST_NEW_USER_ID, "chat_id": TEST_CHAT_ID})
    assert new_user_check is None, "NewUser record was not deleted for non-spam message"

    # --- Assert log attributes using variables fetched before expire_all ---
    test_logger.debug(f"Asserting LLMLog attributes fetched before expire_all (non-spam)")
    assert logged_is_spam is False
    assert logged_reason == non_spam_reason
    assert logged_model_id == expected_model_id # Check correct model was logged
    assert logged_prompt_id == expected_prompt_id # Check correct prompt was logged
    # ----------------------------------------------------------------------

    test_logger.info("Finished test_new_user_sends_non_spam.")
