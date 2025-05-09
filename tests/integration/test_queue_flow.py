import logging
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from anthropic import APIError as AnthropicAPIError
from openai import APIError as OpenAIAPIError  # Import for OpenAI specific error if needed
from sqlalchemy import select

from staring_misaka.config import Settings
from staring_misaka.db_models import NewUser, PendingAdminAction, QueuedLLMCheck
from staring_misaka.dto import LLMSpamAnalysisResult, MessageContext  # For new test
from staring_misaka.event_handlers import EventHandlers
from staring_misaka.llm_service import LLMService
from tests.conftest import TEST_CHAT_ID, TEST_NEW_USER_ID, TEST_SUPER_ADMIN_ID  # For new test

pytestmark = pytest.mark.asyncio
test_logger = logging.getLogger(__name__)


async def test_llm_failure_queues_check_and_notifies_admin(
        db_session, mock_telegram_client, action_service,
        test_settings: Settings,
        setup_queue_test
):
    """
    GIVEN a new user sends a message
    WHEN the LLM analysis fails critically (e.g., ConnectionError simulated by mock)
    THEN the check should be added to QueuedLLMCheck and admin notified.
    """
    test_logger.info("Starting test_llm_failure_queues_check_and_notifies_admin...")

    prompt, model = setup_queue_test
    test_logger.info(f"Using Prompt ID: {prompt.id}, Model ID: {model.id} (Provider: {model.provider})")

    # Expected failure reason when a 401 AuthenticationError occurs, wrapped by InstructorRetryException
    # The LLMService's `except Exception as e:` block will catch this.
    # str(e) for InstructorRetryException should give the underlying error string.
    # The underlying error string for anthropic.AuthenticationError is "Error code: 401 - {'type': 'error', 'error': {'type': 'authentication_error', 'message': 'invalid x-api-key'}}"
    # So the full error_msg in LLMService becomes:
    # f"Unexpected error during {provider_name} spam check for model {active_model.api_identifier}: {e}"
    expected_underlying_error_str = "Error code: 401 - {'type': 'error', 'error': {'type': 'authentication_error', 'message': 'invalid x-api-key'}}"
    actual_failure_reason = f"Unexpected error during {model.provider} spam check for model {model.api_identifier}: {expected_underlying_error_str}"

    real_llm_service_instance = LLMService(test_settings, mock_telegram_client)
    # Check if the specific strategy client is None, which this test relies on for Anthropic
    if model.provider == "Anthropic":
        anthropic_strategy = real_llm_service_instance.provider_strategies.get("Anthropic")
        if anthropic_strategy and anthropic_strategy.client is None:
            # This case would mean the API key was not provided at all.
            # The test uses a dummy key "test_anthropic_key", so client should be initialized.
            test_logger.warning("Anthropic client was NOT initialized. This is unexpected if a dummy key is provided.")
        elif anthropic_strategy and anthropic_strategy.client is not None:
            test_logger.info("Anthropic client IS initialized, as expected with a dummy key. API call should fail.")

    specific_event_handlers = EventHandlers(
        settings=test_settings,
        client=mock_telegram_client,
        llm_service=real_llm_service_instance,
        action_service=action_service
    )
    specific_event_handlers.monitored_chats_cache = [TEST_CHAT_ID]

    mock_sender = await mock_telegram_client.get_entity(TEST_NEW_USER_ID)
    message_id = 201
    text = "This message will fail processing due to LLM error"
    mock_event = MagicMock(chat_id=TEST_CHAT_ID, id=message_id, text=text, sender_id=TEST_NEW_USER_ID, is_private=False)
    mock_event.get_sender = AsyncMock(return_value=mock_sender)

    test_logger.info(f"Calling specific_event_handlers.new_message_handler for msg {message_id}")
    await specific_event_handlers.new_message_handler(mock_event)
    test_logger.info(f"Handler finished processing msg {message_id}.")

    test_logger.info(
        f"Querying for QueuedLLMCheck with model_id={model.id}, prompt_id={prompt.id}, user_id={TEST_NEW_USER_ID}")
    queued_item = await db_session.scalar(
        select(QueuedLLMCheck).where(
            QueuedLLMCheck.original_model_id_attempted == model.id,
            QueuedLLMCheck.original_prompt_id_attempted == prompt.id,
            QueuedLLMCheck.message_context_json['user_id'].as_integer() == TEST_NEW_USER_ID
        ).order_by(QueuedLLMCheck.queued_at.desc())
    )
    test_logger.info(f"Query result: queued_item={queued_item}")

    assert queued_item is not None, "QueuedLLMCheck item was not created or found"
    test_logger.info(f"Found queued item ID: {queued_item.id}")
    assert queued_item.status == "pending", f"Queued item status is '{queued_item.status}', expected 'pending'"
    assert actual_failure_reason == queued_item.reason_for_queueing, f"Failure reason mismatch. Expected: '{actual_failure_reason}', Got: '{queued_item.reason_for_queueing}'"
    assert queued_item.original_model_id_attempted == model.id, "Incorrect model ID stored"
    assert queued_item.original_prompt_id_attempted == prompt.id, "Incorrect prompt ID stored"
    ctx = queued_item.message_context_json
    assert isinstance(ctx, dict), "Stored context is not a dictionary"
    assert ctx.get("user_id") == TEST_NEW_USER_ID, "Incorrect user ID in context"
    assert ctx.get("message_text") == text, "Incorrect message text in context"
    assert ctx.get("message_id") == message_id, "Incorrect message ID in context"

    expected_admin_msg_content_partial = (
        f"⚠️ LLM Check Failed & Queued ⚠️\n"
        f"Reason: {actual_failure_reason}"  # Use the same expected reason
    )

    call_found = False
    test_logger.debug("Checking sent messages log for admin notification...")
    for call_info in mock_telegram_client.sent_messages_log:
        args_call, _ = call_info["args"], call_info["kwargs"]
        test_logger.debug(f"Checking call: args={args_call}")
        if len(args_call) > 1 and args_call[0] == TEST_SUPER_ADMIN_ID:
            if expected_admin_msg_content_partial in args_call[1]:
                call_found = True
                test_logger.debug("Admin notification found.")
                break
    assert call_found, f"Admin notification not found or content mismatch. Expected partial: '{expected_admin_msg_content_partial}'. Log: {mock_telegram_client.sent_messages_log}"

    db_session.expire_all()
    new_user_check = await db_session.get(NewUser, {"user_id": TEST_NEW_USER_ID, "chat_id": TEST_CHAT_ID})
    assert new_user_check is not None, "User was incorrectly removed from NewUser table"

    mock_telegram_client.kick_participant.assert_not_called()
    test_logger.info("Finished test_llm_failure_queues_check_and_notifies_admin.")


async def test_process_llm_queue_batch_flow(
        db_session,
        real_llm_service: LLMService,
        action_service,
        setup_queue_test,
        test_settings: Settings,
        mocker,
        mock_telegram_client
):
    test_logger.info("Starting test_process_llm_queue_batch_flow...")
    prompt_obj, model_obj = setup_queue_test
    # --- Store model attributes before potential expire_all ---
    model_provider_val = model_obj.provider
    model_api_identifier_val = model_obj.api_identifier
    # -------------------------------------------------------
    default_provider = model_provider_val
    strategy_instance = real_llm_service.provider_strategies.get(default_provider)

    if not strategy_instance:
        pytest.skip(
            f"Skipping test: Anthropic strategy for provider '{default_provider}' not found in LLMService. This might be due to the 'proxies' init error.")
        return

    if strategy_instance.client is None:
        strategy_instance.client = MagicMock()
        test_logger.debug(f"Patched strategy_instance.client for provider {default_provider} as it was None.")

    spam_text = "URGENT WINNER CLICK HERE SPAM"
    spam_user_id = TEST_NEW_USER_ID + 10
    spam_msg_id = 301
    spam_context = MessageContext(user_id=spam_user_id, chat_id=TEST_CHAT_ID, message_id=spam_msg_id,
                                  message_text=spam_text, is_new_user=True)
    q_spam = QueuedLLMCheck(message_context_json=spam_context.model_dump(mode='json'),
                            reason_for_queueing="Initial failure", original_model_id_attempted=model_obj.id,
                            original_prompt_id_attempted=prompt_obj.id)
    db_session.add(NewUser(user_id=spam_user_id, chat_id=TEST_CHAT_ID))

    not_spam_text = "Hello, this is a normal message."
    not_spam_user_id = TEST_NEW_USER_ID + 11
    not_spam_msg_id = 302
    not_spam_context = MessageContext(user_id=not_spam_user_id, chat_id=TEST_CHAT_ID, message_id=not_spam_msg_id,
                                      message_text=not_spam_text, is_new_user=True)
    q_not_spam = QueuedLLMCheck(message_context_json=not_spam_context.model_dump(mode='json'),
                                reason_for_queueing="Initial failure", original_model_id_attempted=model_obj.id,
                                original_prompt_id_attempted=prompt_obj.id)
    db_session.add(NewUser(user_id=not_spam_user_id, chat_id=TEST_CHAT_ID))

    fail_text = "This message will cause a reprocessing error."
    fail_user_id = TEST_NEW_USER_ID + 12
    fail_msg_id = 303
    fail_context = MessageContext(user_id=fail_user_id, chat_id=TEST_CHAT_ID, message_id=fail_msg_id,
                                  message_text=fail_text, is_new_user=True)
    q_fail = QueuedLLMCheck(
        message_context_json=fail_context.model_dump(mode='json'),
        reason_for_queueing="Initial failure",
        original_model_id_attempted=model_obj.id,
        original_prompt_id_attempted=prompt_obj.id,
        retry_count=test_settings.queue.max_automatic_retries - 1
    )
    db_session.add(NewUser(user_id=fail_user_id, chat_id=TEST_CHAT_ID))

    db_session.add_all([q_spam, q_not_spam, q_fail])
    await db_session.flush()

    q_spam_id, q_not_spam_id, q_fail_id = q_spam.id, q_not_spam.id, q_fail.id
    test_logger.debug(
        f"Created QueuedLLMCheck items: Spam ID={q_spam_id}, NotSpam ID={q_not_spam_id}, Fail ID={q_fail_id}")

    async def analyze_side_effect(model_api_identifier_param: str, formatted_prompt_str: str,
                                  response_pydantic_model):  # Renamed model_api_identifier to avoid clash
        test_logger.debug(f"Mocked analyze called with prompt containing: '{formatted_prompt_str[:100]}...'")
        if spam_text in formatted_prompt_str:
            return LLMSpamAnalysisResult(is_spam=True, reason="AUTO: Reprocessed as spam",
                                         model_name_used=model_api_identifier_param, status="success", input_tokens=10,
                                         output_tokens=5)
        elif not_spam_text in formatted_prompt_str:
            return LLMSpamAnalysisResult(is_spam=False, reason="AUTO: Reprocessed as not spam",
                                         model_name_used=model_api_identifier_param, status="success", input_tokens=10,
                                         output_tokens=5)
        elif fail_text in formatted_prompt_str:
            mock_request = httpx.Request('POST', 'http://dummy.anthropic.com/v1/messages')
            simulated_error_message = "Simulated API error during reprocessing"
            simulated_error_body = {"type": "error", "error": {"type": "simulated_reprocessing_error",
                                                               "message": simulated_error_message}}

            if default_provider == "Anthropic":
                raise AnthropicAPIError(message=simulated_error_message, request=mock_request,
                                        body=simulated_error_body)
            elif default_provider == "OpenAI":
                raise OpenAIAPIError(
                    message=simulated_error_message,
                    request=mock_request,
                    body={"error": {"message": "Simulated detail", "type": "server_error", "code": "500"}},
                    code="server_error",
                )
            else:
                raise Exception(f"Simulated generic error for provider {default_provider}")
        return LLMSpamAnalysisResult(is_spam=False, reason="Default mock response",
                                     model_name_used=model_api_identifier_param, status="success")

    mocker.patch.object(strategy_instance, 'analyze', side_effect=analyze_side_effect)

    processed_count_1 = await real_llm_service.process_llm_queue_batch(db_session, action_service)
    test_logger.info(f"First batch processing: {processed_count_1} items resolved.")
    processed_count_2 = await real_llm_service.process_llm_queue_batch(db_session, action_service)
    test_logger.info(f"Second batch processing: {processed_count_2} items resolved.")

    # --- Assert ---
    db_session.expire_all()

    assert await db_session.get(QueuedLLMCheck, q_spam_id) is None, "Spam QueuedLLMCheck item was not deleted."
    pending_action_spam = await db_session.scalar(
        select(PendingAdminAction).where(PendingAdminAction.user_to_act_on_id == spam_user_id)
    )
    assert pending_action_spam is not None, "PendingAdminAction for spam item was not created."
    assert "(From Reprocessed Queue) AUTO: Reprocessed as spam" in pending_action_spam.llm_reason_for_action
    assert await db_session.get(NewUser, {"user_id": spam_user_id, "chat_id": TEST_CHAT_ID}) is not None

    assert await db_session.get(QueuedLLMCheck, q_not_spam_id) is None, "Not-spam QueuedLLMCheck item was not deleted."
    assert await db_session.get(NewUser, {"user_id": not_spam_user_id,
                                          "chat_id": TEST_CHAT_ID}) is None, "NewUser for not-spam item was not deleted."
    pending_action_not_spam = await db_session.scalar(
        select(PendingAdminAction).where(PendingAdminAction.user_to_act_on_id == not_spam_user_id)
    )
    assert pending_action_not_spam is None, "PendingAdminAction should not be created for not-spam item."

    q_fail_final_state = await db_session.get(QueuedLLMCheck, q_fail_id)
    assert q_fail_final_state is not None, "Failed QueuedLLMCheck item should still exist."
    assert q_fail_final_state.status == "pending_admin_action", f"Failed item status is {q_fail_final_state.status}, expected pending_admin_action."
    assert q_fail_final_state.retry_count == test_settings.queue.max_automatic_retries

    # Use the stored model attributes
    expected_fail_reason_core = f"API Error with provider {model_provider_val} for model {model_api_identifier_val}: Simulated API error during reprocessing"
    expected_fail_reason = f"Reprocess critical error: {expected_fail_reason_core}"
    assert expected_fail_reason == q_fail_final_state.reason_for_queueing, f"Expected reason '{expected_fail_reason}', got '{q_fail_final_state.reason_for_queueing}'"

    max_retry_admin_notification_found = any(
        call["args"][0] == TEST_SUPER_ADMIN_ID and "Max Auto-Retries Reached" in call["args"][
            1] and f"Item ID: {q_fail_id}" in call["args"][1]
        for call in mock_telegram_client.sent_messages_log
    )
    assert max_retry_admin_notification_found, "Admin notification for max retries not found for the failed item."

    test_logger.info("Finished test_process_llm_queue_batch_flow.")
