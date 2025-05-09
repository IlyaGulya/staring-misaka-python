# tests/integration/test_queue_flow.py
import logging  # Add logging
from unittest.mock import AsyncMock, MagicMock
import asyncio  # For running process_llm_queue_batch multiple times

import httpx  # For request in APIError
import pytest
import pytest_asyncio  # For async fixtures
from anthropic import APIError as AnthropicAPIError  # For specific error type
from openai import APIError as OpenAIAPIError  # Import for OpenAI specific error if needed
from sqlalchemy import select, func  # Added func

from staring_misaka import metrics_service as metrics_module  # For mocking metrics
from staring_misaka.config import Settings
from staring_misaka.db_models import NewUser, PendingAdminAction, QueuedLLMCheck, LLMLog  # For new test
from staring_misaka.dto import LLMSpamAnalysisResult, MessageContext  # For new test
from staring_misaka.event_handlers import EventHandlers
from staring_misaka.llm_service import LLMService
from tests.conftest import (
    TEST_CHAT_ID, TEST_NEW_USER_ID, TEST_SUPER_ADMIN_ID,
    SPAM_MESSAGE_TEXT, NON_SPAM_MESSAGE_TEXT  # Import text constants
)

pytestmark = pytest.mark.asyncio
test_logger = logging.getLogger(__name__)  # Use __name__ for logger


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
    simulated_exception = AnthropicAPIError(  # Or OpenAIAPIError depending on default model provider
        message=expected_error_message,
        request=httpx.Request('POST', 'http://dummy.anthropic.com/v1/messages'),  # Dummy request
        body={"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}
    )

    # This is how LLMService formats the reason when queuing due to an API error
    actual_failure_reason = f"API Error with provider {model.provider} for model {model.api_identifier}: {simulated_exception!s}"

    real_llm_service_instance = LLMService(test_settings, mock_telegram_client)
    strategy_instance = real_llm_service_instance.provider_strategies.get(model.provider)
    assert strategy_instance is not None, f"Strategy for {model.provider} not found."
    if strategy_instance.client is None:  # Ensure client is mocked if it didn't init
        strategy_instance.client = MagicMock()
    mocker.patch.object(strategy_instance, 'analyze', side_effect=simulated_exception)

    specific_event_handlers = EventHandlers(
        settings=test_settings,
        client=mock_telegram_client,
        llm_service=real_llm_service_instance,  # Using the instance with mocked strategy
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
        model_name=model.api_identifier, error_type=f"api_error_{model.provider.lower()}"
        # Adjusted to match LLMService
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
    assert llm_log_entry.llm_is_spam is False  # Default on error before result
    assert actual_failure_reason in llm_log_entry.llm_reason  # Check if the reason is part of the log
    assert llm_log_entry.model_id == model.id
    assert llm_log_entry.prompt_id == prompt.id

    db_session.expire_all()  # Use expire_all for safety
    new_user_check = await db_session.get(NewUser, {"user_id": TEST_NEW_USER_ID, "chat_id": TEST_CHAT_ID})
    assert new_user_check is not None, "User was incorrectly removed from NewUser table"
    mock_telegram_client.kick_participant.assert_not_called()
    test_logger.info("Finished test_llm_failure_queues_check_and_notifies_admin.")


@pytest_asyncio.fixture
async def setup_queued_items_for_processing(db_session, setup_queue_test, test_settings, mocker, real_llm_service):
    """
    Fixture to set up various QueuedLLMCheck items for batch processing tests.
    It also mocks the 'analyze' method of the LLM strategy.
    Yields a dictionary of item IDs for easy retrieval in tests.
    Also yields the prompt and model objects used for setup.
    """
    prompt_obj, model_obj = setup_queue_test
    default_provider = model_obj.provider
    strategy_instance = real_llm_service.provider_strategies.get(default_provider)
    if not strategy_instance:
        pytest.skip(f"Strategy for provider '{default_provider}' not found.")
    if strategy_instance.client is None:
        strategy_instance.client = MagicMock()

    # Texts for different scenarios
    spam_text_q = SPAM_MESSAGE_TEXT + " from queue"
    non_spam_text_q = NON_SPAM_MESSAGE_TEXT + " from queue"
    fail_max_retries_text_q = "This message will always fail and hit max retries."
    fail_not_max_retries_text_q = "This message will fail once but not hit max retries."

    # User IDs and Message IDs (ensure uniqueness)
    spam_user_id, spam_msg_id = TEST_NEW_USER_ID + 10, 301
    non_spam_user_id, non_spam_msg_id = TEST_NEW_USER_ID + 11, 302
    fail_max_user_id, fail_max_msg_id = TEST_NEW_USER_ID + 12, 303
    fail_not_max_user_id, fail_not_max_msg_id = TEST_NEW_USER_ID + 13, 304

    # Create QueuedLLMCheck items
    items_to_create = [
        (spam_user_id, spam_msg_id, spam_text_q, "q_spam", 0),
        (non_spam_user_id, non_spam_msg_id, non_spam_text_q, "q_non_spam", 0),
        (fail_max_user_id, fail_max_msg_id, fail_max_retries_text_q, "q_fail_max",
         test_settings.queue.max_automatic_retries - 1),  # Will hit max on next retry
        (fail_not_max_user_id, fail_not_max_msg_id, fail_not_max_retries_text_q, "q_fail_not_max", 0)
        # Will not hit max (if max_retries > 0)
    ]

    item_ids = {}
    for user_id, msg_id, text, key, retry_count_init in items_to_create:
        db_session.add(NewUser(user_id=user_id, chat_id=TEST_CHAT_ID))  # Ensure user is "new"
        context = MessageContext(user_id=user_id, chat_id=TEST_CHAT_ID, message_id=msg_id, message_text=text,
                                 is_new_user=True)
        item = QueuedLLMCheck(
            message_context_json=context.model_dump(mode='json'),
            reason_for_queueing="Initial test queue reason",
            original_model_id_attempted=model_obj.id,
            original_prompt_id_attempted=prompt_obj.id,
            retry_count=retry_count_init
        )
        db_session.add(item)
        await db_session.flush()
        item_ids[key] = item.id
        test_logger.debug(f"Created QueuedLLMCheck item '{key}' with ID={item.id}, initial retries={retry_count_init}")

    # Mock 'analyze' side effect
    simulated_error_message = "Simulated API error during reprocessing"
    simulated_api_error_exception = (
        AnthropicAPIError(
            message=simulated_error_message,
            request=httpx.Request('POST', 'http://dummy.anthropic.com/v1/messages'),
            body={"type": "error",
                  "error": {"type": "simulated_reprocessing_error", "message": simulated_error_message}}
        ) if default_provider == "Anthropic" else
        OpenAIAPIError(
            message=simulated_error_message,
            request=httpx.Request('POST', 'http://dummy.openai.com/v1/chat/completions'),
            body={"error": {"message": "Simulated detail", "type": "server_error", "code": "500"}}, code="server_error",
        )
    )

    async def analyze_side_effect_func(model_api_identifier_param: str, formatted_prompt_str: str,
                                       response_pydantic_model):
        if spam_text_q in formatted_prompt_str:
            return LLMSpamAnalysisResult(is_spam=True, reason="AUTO: Reprocessed as spam",
                                         model_name_used=model_api_identifier_param, status="success", input_tokens=10,
                                         output_tokens=5)
        elif non_spam_text_q in formatted_prompt_str:
            return LLMSpamAnalysisResult(is_spam=False, reason="AUTO: Reprocessed as not spam",
                                         model_name_used=model_api_identifier_param, status="success", input_tokens=10,
                                         output_tokens=5)
        elif fail_max_retries_text_q in formatted_prompt_str or fail_not_max_retries_text_q in formatted_prompt_str:
            raise simulated_api_error_exception
        return LLMSpamAnalysisResult(is_spam=False, reason="Default mock response",
                                     model_name_used=model_api_identifier_param, status="success")

    mocker.patch.object(strategy_instance, 'analyze', side_effect=analyze_side_effect_func)

    yield item_ids, prompt_obj, model_obj  # Yield prompt and model too


async def _run_queue_processor_loops(real_llm_service, db_session, action_service, num_loops, item_ids_to_check):
    """Helper to run the queue processor and check if all items are resolved."""
    for i in range(num_loops):
        test_logger.debug(f"Queue processing batch run {i + 1}/{num_loops}")
        await real_llm_service.process_llm_queue_batch(db_session, action_service)

        remaining_retryable_count = await db_session.scalar(
            select(func.count(QueuedLLMCheck.id)).where(
                QueuedLLMCheck.id.in_(list(item_ids_to_check.values())),  # Ensure it's a list of IDs
                QueuedLLMCheck.status.in_(["pending", "failed_reprocessing_attempt", "processing"])
            )
        )
        if remaining_retryable_count == 0:
            test_logger.debug("All test items are in a final state or deleted. Breaking queue processing loop.")
            break
        await asyncio.sleep(0.05)  # Small delay to allow async tasks to switch if needed
    else:
        test_logger.warning("Queue processing loop finished by iteration limit.")


async def test_queue_spam_item_processed(db_session, real_llm_service, action_service,
                                         setup_queued_items_for_processing, test_settings, mock_telegram_client):
    """Tests that a spam item in the queue is correctly processed and leads to PendingAdminAction."""
    item_ids, _, _ = setup_queued_items_for_processing  # Unpack all three
    spam_item_id = item_ids["q_spam"]
    spam_user_id = TEST_NEW_USER_ID + 10  # From fixture setup

    num_loops = (test_settings.queue.batch_size + test_settings.queue.max_automatic_retries + 2)
    await _run_queue_processor_loops(real_llm_service, db_session, action_service, num_loops, item_ids)

    db_session.expire_all()
    assert await db_session.get(QueuedLLMCheck, spam_item_id) is None, "Spam QueuedLLMCheck item was not deleted."
    pending_action_spam = await db_session.scalar(
        select(PendingAdminAction).where(PendingAdminAction.user_to_act_on_id == spam_user_id))
    assert pending_action_spam is not None, "PendingAdminAction for spam item was not created."
    assert "(From Reprocessed Queue) AUTO: Reprocessed as spam" in pending_action_spam.llm_reason_for_action


async def test_queue_non_spam_item_processed(db_session, real_llm_service, action_service,
                                             setup_queued_items_for_processing, test_settings, mock_telegram_client):
    """Tests that a non-spam item in the queue is correctly processed and the user is approved."""
    item_ids, _, _ = setup_queued_items_for_processing  # Unpack all three
    non_spam_item_id = item_ids["q_non_spam"]
    non_spam_user_id = TEST_NEW_USER_ID + 11  # From fixture setup

    num_loops = (test_settings.queue.batch_size + test_settings.queue.max_automatic_retries + 2)
    await _run_queue_processor_loops(real_llm_service, db_session, action_service, num_loops, item_ids)

    db_session.expire_all()
    assert await db_session.get(QueuedLLMCheck,
                                non_spam_item_id) is None, "Not-spam QueuedLLMCheck item was not deleted."
    assert await db_session.get(NewUser, {"user_id": non_spam_user_id,
                                          "chat_id": TEST_CHAT_ID}) is None, "NewUser for not-spam item was not deleted."


async def test_queue_item_reaches_max_retries(db_session, real_llm_service, action_service,
                                              setup_queued_items_for_processing,
                                              test_settings, mock_telegram_client):
    """Tests that an item failing repeatedly reaches max retries and admin is notified."""
    item_ids, prompt_obj, model_obj = setup_queued_items_for_processing  # Use all yielded values
    fail_max_item_id = item_ids["q_fail_max"]

    # Capture provider and api_identifier before db_session.expire_all() if model_obj becomes inaccessible
    model_provider = model_obj.provider
    model_api_id = model_obj.api_identifier

    num_loops = (test_settings.queue.batch_size + test_settings.queue.max_automatic_retries + 2)
    await _run_queue_processor_loops(real_llm_service, db_session, action_service, num_loops, item_ids)

    db_session.expire_all()
    q_fail_final_state = await db_session.get(QueuedLLMCheck, fail_max_item_id)
    assert q_fail_final_state is not None, "Failed (max_retries) QueuedLLMCheck item should still exist."
    assert q_fail_final_state.status == "pending_admin_action", f"Failed item status is {q_fail_final_state.status}, expected pending_admin_action."
    assert q_fail_final_state.retry_count == test_settings.queue.max_automatic_retries

    expected_fail_reason_core = f"API Error with provider {model_provider} for model {model_api_id}: Simulated API error during reprocessing"
    expected_fail_reason = f"Reprocess critical error: {expected_fail_reason_core}"
    assert expected_fail_reason == q_fail_final_state.reason_for_queueing

    max_retry_admin_notification_found = any(
        call["args"][0] == TEST_SUPER_ADMIN_ID and "Max Auto-Retries Reached" in call["args"][
            1] and f"Item ID: {fail_max_item_id}" in call["args"][1]
        for call in mock_telegram_client.sent_messages_log
    )
    assert max_retry_admin_notification_found, "Admin notification for max retries not found for the failed item."


async def test_queue_item_fails_not_max_retries(db_session, real_llm_service, action_service,
                                                setup_queued_items_for_processing,
                                                test_settings: Settings,
                                                mock_telegram_client, mocker):
    """
    Tests an item that fails LLM analysis during reprocessing.
    With test_settings.queue.max_automatic_retries = 1, this item will also hit max retries.
    """
    item_ids, prompt_obj, model_obj = setup_queued_items_for_processing
    fail_not_max_item_id = item_ids["q_fail_not_max"]

    model_provider = model_obj.provider
    model_api_id = model_obj.api_identifier

    num_loops = (test_settings.queue.batch_size + test_settings.queue.max_automatic_retries + 2)
    await _run_queue_processor_loops(real_llm_service, db_session, action_service, num_loops, item_ids)

    db_session.expire_all()
    q_fail_not_max_final_state = await db_session.get(QueuedLLMCheck, fail_not_max_item_id)
    assert q_fail_not_max_final_state is not None

    # With max_automatic_retries = 1, this item (starting at retry_count=0) will fail once,
    # its retry_count will become 1, which is equal to max_automatic_retries.
    # Thus, its status should become "pending_admin_action".
    assert q_fail_not_max_final_state.status == "pending_admin_action", \
        f"Expected 'pending_admin_action' with max_retries='{test_settings.queue.max_automatic_retries}', got '{q_fail_not_max_final_state.status}'"
    assert q_fail_not_max_final_state.retry_count == test_settings.queue.max_automatic_retries, \
        f"Expected retry_count=1, got {q_fail_not_max_final_state.retry_count}"

    admin_notif_found = any(
        call["args"][0] == TEST_SUPER_ADMIN_ID and "Max Auto-Retries Reached" in call["args"][
            1] and f"Item ID: {fail_not_max_item_id}" in call["args"][1]
        for call in mock_telegram_client.sent_messages_log
    )
    assert admin_notif_found, "Admin notification for max retries should be sent for fail_not_max_item_id with max_retries=1"

    expected_fail_reason_core = f"API Error with provider {model_provider} for model {model_api_id}: Simulated API error during reprocessing"
    expected_fail_reason = f"Reprocess critical error: {expected_fail_reason_core}"
    assert expected_fail_reason == q_fail_not_max_final_state.reason_for_queueing