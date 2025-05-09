# tests/integration/test_queue_flow.py
import logging # Add logging
from unittest.mock import AsyncMock, MagicMock

import httpx # For request in APIError
import pytest
from anthropic import APIError as AnthropicAPIError # For specific error type
from openai import APIError as OpenAIAPIError # Import for OpenAI specific error if needed
from sqlalchemy import select, func # Added func

from staring_misaka import metrics_service as metrics_module # For mocking metrics
from staring_misaka.config import Settings
from staring_misaka.db_models import NewUser, PendingAdminAction, QueuedLLMCheck, LLMLog # For new test
from staring_misaka.dto import LLMSpamAnalysisResult, MessageContext  # For new test
from staring_misaka.event_handlers import EventHandlers
from staring_misaka.llm_service import LLMService
from tests.conftest import TEST_CHAT_ID, TEST_NEW_USER_ID, TEST_SUPER_ADMIN_ID  # For new test

pytestmark = pytest.mark.asyncio
test_logger = logging.getLogger(__name__) # Use __name__ for logger


async def test_llm_failure_queues_check_and_notifies_admin(
        db_session, mock_telegram_client, action_service,
        test_settings: Settings,
        setup_queue_test, mocker
):
    """
    GIVEN a new user sends a message
    WHEN the LLM analysis fails critically (e.g., ConnectionError simulated by mock)
    THEN the check should be added to QueuedLLMCheck and admin notified.
    """
    test_logger.info("Starting test_llm_failure_queues_check_and_notifies_admin...")
    # --- Metric Mocks ---
    mock_messages_processed_labels = mocker.patch.object(metrics_module.MESSAGES_PROCESSED, 'labels')
    mock_messages_processed_inc = MagicMock()
    mock_messages_processed_labels.return_value = MagicMock(inc=mock_messages_processed_inc)

    mock_llm_api_requests_labels = mocker.patch.object(metrics_module.LLM_API_REQUESTS, 'labels')
    mock_llm_api_requests_inc = MagicMock()
    mock_llm_api_requests_labels.return_value = MagicMock(inc=mock_llm_api_requests_inc)

    mock_llm_api_errors_labels = mocker.patch.object(metrics_module.LLM_API_ERRORS, 'labels')
    mock_llm_api_errors_inc = MagicMock()
    mock_llm_api_errors_labels.return_value = MagicMock(inc=mock_llm_api_errors_inc)
    # --- End Metric Mocks ---

    prompt, model = setup_queue_test
    test_logger.info(f"Using Prompt ID: {prompt.id}, Model ID: {model.id} (Provider: {model.provider})")

    expected_error_message = "Simulated Authentication Error from LLM Provider"
    simulated_exception = AnthropicAPIError( # Or OpenAIAPIError depending on default model provider
        message=expected_error_message,
        request=httpx.Request('POST', 'http://dummy.anthropic.com/v1/messages'), # Dummy request
        body={"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}
    )

    # This is how LLMService formats the reason when queuing due to an API error
    actual_failure_reason = f"API Error with provider {model.provider} for model {model.api_identifier}: {simulated_exception!s}"

    real_llm_service_instance = LLMService(test_settings, mock_telegram_client)
    strategy_instance = real_llm_service_instance.provider_strategies.get(model.provider)
    assert strategy_instance is not None, f"Strategy for {model.provider} not found."
    if strategy_instance.client is None: # Ensure client is mocked if it didn't init
        strategy_instance.client = MagicMock()
    mocker.patch.object(strategy_instance, 'analyze', side_effect=simulated_exception)


    specific_event_handlers = EventHandlers(
        settings=test_settings,
        client=mock_telegram_client,
        llm_service=real_llm_service_instance, # Using the instance with mocked strategy
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

    # Assert Metrics
    mock_messages_processed_labels.assert_called_once_with(chat_id=str(TEST_CHAT_ID))
    mock_messages_processed_inc.assert_called_once_with()

    mock_llm_api_requests_labels.assert_called_once_with(
        model_name=model.api_identifier, chat_id_label=str(TEST_CHAT_ID)
    )
    mock_llm_api_requests_inc.assert_called_once_with()

    mock_llm_api_errors_labels.assert_called_once_with(
        model_name=model.api_identifier, error_type=f"api_error_{model.provider.lower()}" # Adjusted to match LLMService
    )
    mock_llm_api_errors_inc.assert_called_once_with()


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
    assert actual_failure_reason == queued_item.reason_for_queueing, \
        f"Failure reason mismatch. Expected: '{actual_failure_reason}', Got: '{queued_item.reason_for_queueing}'"


    ctx = queued_item.message_context_json
    assert isinstance(ctx, dict), "Stored context is not a dictionary"
    assert ctx.get("user_id") == TEST_NEW_USER_ID

    expected_admin_msg_content_partial = (
        f"⚠️ LLM Check Failed & Queued ⚠️\n"
        f"Reason: {actual_failure_reason}"
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

    # Check LLMLog was created and indicates failure
    llm_log_entry = await db_session.scalar(
        select(LLMLog).where(LLMLog.message_id == message_id, LLMLog.user_id == TEST_NEW_USER_ID)
    )
    assert llm_log_entry is not None, "LLMLog entry not created for failed check"
    assert llm_log_entry.llm_is_spam is False # Default on error before result
    assert actual_failure_reason in llm_log_entry.llm_reason # Check if the reason is part of the log
    assert llm_log_entry.model_id == model.id
    assert llm_log_entry.prompt_id == prompt.id


    db_session.expire_all() # Use expire_all for safety
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
    # --- Metric Mocks ---
    mock_llm_api_errors_labels = mocker.patch.object(metrics_module.LLM_API_ERRORS, 'labels')
    mock_llm_api_errors_inc = MagicMock()
    mock_llm_api_errors_labels.return_value = MagicMock(inc=mock_llm_api_errors_inc)

    mock_llm_tokens_used_labels = mocker.patch.object(metrics_module.LLM_TOKENS_USED, 'labels')
    mock_llm_tokens_used_inc = MagicMock() # Not strictly needed if only .labels called
    mock_llm_tokens_used_labels.return_value = MagicMock(inc=mock_llm_tokens_used_inc)


    mock_llm_cost_labels = mocker.patch.object(metrics_module.LLM_ESTIMATED_COST_CENTS, 'labels')
    mock_llm_cost_inc = MagicMock() # Not strictly needed
    mock_llm_cost_labels.return_value = MagicMock(inc=mock_llm_cost_inc)
    # --- End Metric Mocks ---


    prompt_obj, model_obj = setup_queue_test
    model_provider_val = model_obj.provider
    model_api_identifier_val = model_obj.api_identifier
    default_provider = model_provider_val
    strategy_instance = real_llm_service.provider_strategies.get(default_provider)

    if not strategy_instance:
        pytest.skip(f"Skipping test: Strategy for provider '{default_provider}' not found.")
    if strategy_instance.client is None:
        strategy_instance.client = MagicMock()
        test_logger.debug(f"Patched strategy_instance.client for provider {default_provider} as it was None.")

    spam_text = "URGENT WINNER CLICK HERE SPAM"
    spam_user_id = TEST_NEW_USER_ID + 10; spam_msg_id = 301
    spam_context = MessageContext(user_id=spam_user_id, chat_id=TEST_CHAT_ID, message_id=spam_msg_id, message_text=spam_text, is_new_user=True)
    q_spam = QueuedLLMCheck(message_context_json=spam_context.model_dump(mode='json'), reason_for_queueing="Initial failure", original_model_id_attempted=model_obj.id, original_prompt_id_attempted=prompt_obj.id)
    db_session.add(NewUser(user_id=spam_user_id, chat_id=TEST_CHAT_ID))

    not_spam_text = "Hello, this is a normal message."
    not_spam_user_id = TEST_NEW_USER_ID + 11; not_spam_msg_id = 302
    not_spam_context = MessageContext(user_id=not_spam_user_id, chat_id=TEST_CHAT_ID, message_id=not_spam_msg_id, message_text=not_spam_text, is_new_user=True)
    q_not_spam = QueuedLLMCheck(message_context_json=not_spam_context.model_dump(mode='json'), reason_for_queueing="Initial failure", original_model_id_attempted=model_obj.id, original_prompt_id_attempted=prompt_obj.id)
    db_session.add(NewUser(user_id=not_spam_user_id, chat_id=TEST_CHAT_ID))

    fail_text = "This message will cause a reprocessing error."
    fail_user_id = TEST_NEW_USER_ID + 12; fail_msg_id = 303
    fail_context = MessageContext(user_id=fail_user_id, chat_id=TEST_CHAT_ID, message_id=fail_msg_id, message_text=fail_text, is_new_user=True)
    q_fail = QueuedLLMCheck(
        message_context_json=fail_context.model_dump(mode='json'), reason_for_queueing="Initial failure",
        original_model_id_attempted=model_obj.id, original_prompt_id_attempted=prompt_obj.id,
        retry_count=test_settings.queue.max_automatic_retries - 1 # Will hit max on next retry
    )
    db_session.add(NewUser(user_id=fail_user_id, chat_id=TEST_CHAT_ID))

    # For "LLM Fails, Not Max Retries"
    fail_not_max_text = "This will also fail, but not max retries yet."
    fail_not_max_user_id = TEST_NEW_USER_ID + 13; fail_not_max_msg_id = 304
    fail_not_max_context = MessageContext(user_id=fail_not_max_user_id, chat_id=TEST_CHAT_ID, message_id=fail_not_max_msg_id, message_text=fail_not_max_text, is_new_user=True)
    q_fail_not_max = QueuedLLMCheck(
        message_context_json=fail_not_max_context.model_dump(mode='json'), reason_for_queueing="Initial failure",
        original_model_id_attempted=model_obj.id, original_prompt_id_attempted=prompt_obj.id,
        retry_count=0 # Will not hit max on next retry
    )
    db_session.add(NewUser(user_id=fail_not_max_user_id, chat_id=TEST_CHAT_ID))


    db_session.add_all([q_spam, q_not_spam, q_fail, q_fail_not_max])
    await db_session.flush()
    q_spam_id, q_not_spam_id, q_fail_id, q_fail_not_max_id = q_spam.id, q_not_spam.id, q_fail.id, q_fail_not_max.id
    test_logger.debug(
        f"Created QueuedLLMCheck items: Spam ID={q_spam_id}, NotSpam ID={q_not_spam_id}, Fail ID={q_fail_id}, FailNotMax ID={q_fail_not_max_id}")


    simulated_error_message = "Simulated API error during reprocessing"
    simulated_api_error_exception = AnthropicAPIError(
        message=simulated_error_message,
        request=httpx.Request('POST', 'http://dummy.anthropic.com/v1/messages'),
        body={"type": "error", "error": {"type": "simulated_reprocessing_error", "message": simulated_error_message}}
    ) if default_provider == "Anthropic" else OpenAIAPIError(
        message=simulated_error_message, request=httpx.Request('POST', 'http://dummy.openai.com/v1/chat/completions'),
        body={"error": {"message": "Simulated detail", "type": "server_error", "code": "500"}}, code="server_error",
    )


    async def analyze_side_effect(model_api_identifier_param: str, formatted_prompt_str: str, response_pydantic_model):
        test_logger.debug(f"Mocked analyze called with prompt containing: '{formatted_prompt_str[:100]}...'")
        if spam_text in formatted_prompt_str:
            return LLMSpamAnalysisResult(is_spam=True, reason="AUTO: Reprocessed as spam", model_name_used=model_api_identifier_param, status="success", input_tokens=10, output_tokens=5)
        elif not_spam_text in formatted_prompt_str:
            return LLMSpamAnalysisResult(is_spam=False, reason="AUTO: Reprocessed as not spam", model_name_used=model_api_identifier_param, status="success", input_tokens=10, output_tokens=5)
        elif fail_text in formatted_prompt_str or fail_not_max_text in formatted_prompt_str: # Both fail cases
            raise simulated_api_error_exception
        return LLMSpamAnalysisResult(is_spam=False, reason="Default mock response", model_name_used=model_api_identifier_param, status="success")

    mocker.patch.object(strategy_instance, 'analyze', side_effect=analyze_side_effect)

    # Process items until all are handled or no progress is made
    processed_count_total = 0
    max_items_to_process = 4 # spam, not_spam, fail, fail_not_max
    loop_breaker = 0 # Safety break for the loop
    # Loop enough times to process items based on batch size and potential retries
    # Each item might be processed once, or multiple times if it fails and retries.
    # A simple loop for a fixed number of iterations, assuming batch size allows all items to be picked up.
    # Max retries is 2, so an item might be picked up 3 times.
    # (max_items_to_process * (test_settings.queue.max_automatic_retries + 1)) / test_settings.queue.batch_size
    # This can be complex. A simpler way for test is to run it enough times.
    num_loops = (max_items_to_process // test_settings.queue.batch_size) + test_settings.queue.max_automatic_retries + 2 # Ensure enough runs

    for i in range(num_loops):
        test_logger.debug(f"Queue processing batch run {i+1}/{num_loops}")
        processed_in_this_batch = await real_llm_service.process_llm_queue_batch(db_session, action_service)
        test_logger.debug(f"Items resolved in this batch: {processed_in_this_batch}")
        # Check if all items are in a final (non-retryable) state
        remaining_retryable_count = await db_session.scalar(
            select(func.count(QueuedLLMCheck.id)).where(
                QueuedLLMCheck.id.in_([q_spam_id, q_not_spam_id, q_fail_id, q_fail_not_max_id]),
                QueuedLLMCheck.status.in_(["pending", "failed_reprocessing_attempt", "processing"])
            )
        )
        if remaining_retryable_count == 0:
            test_logger.debug("All test items are in a final state or deleted. Breaking queue processing loop.")
            break
    else: # If loop finishes without break
        test_logger.warning("Queue processing loop finished by iteration limit, not by all items reaching final state.")



    # --- Assert ---
    db_session.expire_all() # Use expire_all for safety

    # Spam case
    assert await db_session.get(QueuedLLMCheck, q_spam_id) is None, "Spam QueuedLLMCheck item was not deleted."
    pending_action_spam = await db_session.scalar(select(PendingAdminAction).where(PendingAdminAction.user_to_act_on_id == spam_user_id))
    assert pending_action_spam is not None, "PendingAdminAction for spam item was not created."
    assert "(From Reprocessed Queue) AUTO: Reprocessed as spam" in pending_action_spam.llm_reason_for_action

    # Not-spam case
    assert await db_session.get(QueuedLLMCheck, q_not_spam_id) is None, "Not-spam QueuedLLMCheck item was not deleted."
    assert await db_session.get(NewUser, {"user_id": not_spam_user_id, "chat_id": TEST_CHAT_ID}) is None, "NewUser for not-spam item was not deleted."

    # Fail (max retries) case
    q_fail_final_state = await db_session.get(QueuedLLMCheck, q_fail_id)
    assert q_fail_final_state is not None, "Failed (max_retries) QueuedLLMCheck item should still exist."
    assert q_fail_final_state.status == "pending_admin_action", f"Failed item status is {q_fail_final_state.status}, expected pending_admin_action."
    assert q_fail_final_state.retry_count == test_settings.queue.max_automatic_retries
    expected_fail_reason_core = f"API Error with provider {model_provider_val} for model {model_api_identifier_val}: {simulated_error_message}"
    # The reason for queueing is set by analyze_message_for_spam when it returns critical_error_no_check
    expected_fail_reason = f"Reprocess critical error: {expected_fail_reason_core}"
    assert expected_fail_reason == q_fail_final_state.reason_for_queueing, f"Expected reason '{expected_fail_reason}', got '{q_fail_final_state.reason_for_queueing}'"

    max_retry_admin_notification_found = any(
        call["args"][0] == TEST_SUPER_ADMIN_ID and "Max Auto-Retries Reached" in call["args"][1] and f"Item ID: {q_fail_id}" in call["args"][1]
        for call in mock_telegram_client.sent_messages_log
    )
    assert max_retry_admin_notification_found, "Admin notification for max retries not found for the failed item."

    # Check metric for the q_fail item (failed at max_retries)
    # It would have been called for each retry attempt that failed.
    # The analyze_message_for_spam increments this, called by reprocess_queued_item
    # q_fail had retry_count = max_automatic_retries - 1, so it retried once more to hit max.
    # q_fail_not_max had retry_count = 0, so it retried once.
    # Total 2 calls to analyze that raised API error.
    assert mock_llm_api_errors_labels.call_count >= 2 # At least two calls raising API error.
    mock_llm_api_errors_labels.assert_any_call(
         model_name=model_api_identifier_val, error_type=f"api_error_{model_provider_val.lower()}"
    )
    assert mock_llm_api_errors_inc.call_count >= 2


    # Fail (not max retries) case
    q_fail_not_max_final_state = await db_session.get(QueuedLLMCheck, q_fail_not_max_id)
    assert q_fail_not_max_final_state is not None, "Failed (not_max_retries) QueuedLLMCheck item should still exist."
    assert q_fail_not_max_final_state.status == "pending_admin_action" # Corrected for max_retries=1 from test_settings
    assert q_fail_not_max_final_state.retry_count == test_settings.queue.max_automatic_retries # Incremented once to hit max
    expected_fail_not_max_reason = f"Reprocess critical error: {expected_fail_reason_core}"
    assert expected_fail_not_max_reason == q_fail_not_max_final_state.reason_for_queueing
    # No admin notification for this one yet for max retries
    no_max_retry_admin_notification_found_for_not_max = any(
        call["args"][0] == TEST_SUPER_ADMIN_ID and "Max Auto-Retries Reached" in call["args"][1] and f"Item ID: {q_fail_not_max_id}" in call["args"][1]
        for call in mock_telegram_client.sent_messages_log
    )
    # Since max_automatic_retries is 1 in test_settings, this item will also reach max retries
    # and an admin notification *should* be sent.
    assert no_max_retry_admin_notification_found_for_not_max, "Admin notification for max retries was NOT sent for q_fail_not_max_id, but it should have been with max_retries=1."


    test_logger.info("Finished test_process_llm_queue_batch_flow.")