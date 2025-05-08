from unittest.mock import AsyncMock, MagicMock
import logging  # Add logging

import pytest
import pytest_asyncio
from sqlalchemy import select
from anthropic import APIError as AnthropicAPIError # For specific error simulation
import httpx # Import httpx to create a mock request for the error

from staring_misaka.db_models import GlobalBotSettings, LLMModel, NewUser, Prompt, QueuedLLMCheck
from staring_misaka.event_handlers import EventHandlers # Import for creating specific instance
from staring_misaka.llm_service import LLMService # Import for creating specific instance and patching
from staring_misaka.config import Settings # Import for LLMService init
from tests.conftest import TEST_CHAT_ID, TEST_NEW_USER_ID, TEST_SUPER_ADMIN_ID

pytestmark = pytest.mark.asyncio
test_logger = logging.getLogger("pytest_queue_flow")


@pytest_asyncio.fixture
async def setup_queue_test(db_session, monitored_group, new_user_in_group):
    """Common setup for queue tests: ensures group, user, prompt, model exist."""
    test_logger.debug("Setting up queue test data...")
    gs = await db_session.get(GlobalBotSettings, 1)
    assert gs is not None

    prompt = await db_session.get(Prompt, gs.default_prompt_id) if gs.default_prompt_id else None
    if not prompt:
        prompt_name = f"DefaultQueueTestPrompt_{id(db_session)}"
        prompt_id_to_use = gs.default_prompt_id if gs.default_prompt_id else 100
        existing_prompt_with_id = await db_session.get(Prompt, prompt_id_to_use)
        if existing_prompt_with_id and existing_prompt_with_id.name != prompt_name:
            prompt_id_to_use += 1 # Basic collision avoidance for test IDs
        prompt = Prompt(id=prompt_id_to_use, name=prompt_name, text="Test: {message_text}", is_global_default=False)
        if not gs.default_prompt_id:
            prompt.is_global_default = True
        db_session.add(prompt)
        await db_session.flush()
        if not gs.default_prompt_id: gs.default_prompt_id = prompt.id
        test_logger.debug(f"Created/set prompt: ID={prompt.id}, Name={prompt.name}")

    model = await db_session.get(LLMModel, gs.default_model_id) if gs.default_model_id else None
    if not model:
        model_name = f"DefaultQueueTestModel_{id(db_session)}"
        model_id_to_use = gs.default_model_id if gs.default_model_id else 100
        existing_model_with_id = await db_session.get(LLMModel, model_id_to_use)
        if existing_model_with_id and existing_model_with_id.name != model_name:
            model_id_to_use += 1 # Basic collision avoidance for test IDs
        model = LLMModel(id=model_id_to_use, name=model_name, api_identifier="test-m-queue", provider="Anthropic") # Default to Anthropic for test
        db_session.add(model)
        await db_session.flush()
        if not gs.default_model_id: gs.default_model_id = model.id
        test_logger.debug(f"Created/set model: ID={model.id}, Name={model.name}")

    # FIX: Revert back to flush - setup should ideally happen within the main test transaction
    await db_session.flush()
    test_logger.debug(f"Setup complete. Yielding prompt (ID={prompt.id}) and model (ID={model.id})")
    yield prompt, model


async def test_llm_failure_queues_check_and_notifies_admin(
        db_session, mock_telegram_client, action_service, # Use action_service from conftest
        test_settings: Settings, # Use test_settings from conftest
        setup_queue_test
):
    """
    GIVEN a new user sends a message
    WHEN the LLM analysis fails critically (e.g., ConnectionError simulated by mock)
    THEN the check should be added to QueuedLLMCheck and admin notified.
    """
    test_logger.info("Starting test_llm_failure_queues_check_and_notifies_admin...")
    actual_failure_reason = "LLM client for provider 'Anthropic' is not initialized (API key or setup issue)."

    prompt, model = setup_queue_test
    test_logger.info(f"Using Prompt ID: {prompt.id}, Model ID: {model.id}")

    real_llm_service_instance = LLMService(test_settings, mock_telegram_client)

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
    await db_session.flush() # Flushed within new_message_handler or its callees if needed, or commit by context manager
    test_logger.info(f"Session flushed by context manager or explicit call after handler.")


    # Query after operations complete (session commit happens on exit of get_db_session in handler)
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

    # --- Assertions ---
    assert queued_item is not None, "QueuedLLMCheck item was not created or found"
    test_logger.info(f"Found queued item ID: {queued_item.id}")
    assert queued_item.status == "pending", f"Queued item status is '{queued_item.status}', expected 'pending'"
    assert actual_failure_reason == queued_item.reason_for_queueing, f"Expected reason '{actual_failure_reason}' not found in '{queued_item.reason_for_queueing}'"
    assert queued_item.original_model_id_attempted == model.id, "Incorrect model ID stored"
    assert queued_item.original_prompt_id_attempted == prompt.id, "Incorrect prompt ID stored"
    ctx = queued_item.message_context_json
    assert isinstance(ctx, dict), "Stored context is not a dictionary"
    assert ctx.get("user_id") == TEST_NEW_USER_ID, "Incorrect user ID in context"
    assert ctx.get("message_text") == text, "Incorrect message text in context"
    assert ctx.get("message_id") == message_id, "Incorrect message ID in context"

    expected_admin_msg_content_partial = (
        f"⚠️ LLM Check Failed & Queued ⚠️\n"
        f"Reason: {actual_failure_reason}"
    )

    call_found = False
    test_logger.debug(f"Checking sent messages log for admin notification...")
    for call_info in mock_telegram_client.sent_messages_log:
        args_call, _ = call_info["args"], call_info["kwargs"]
        test_logger.debug(f"Checking call: args={args_call}")
        if len(args_call) > 1 and args_call[0] == TEST_SUPER_ADMIN_ID:
            if expected_admin_msg_content_partial in args_call[1]:
                call_found = True
                test_logger.debug(f"Admin notification found.")
                break
    assert call_found, f"Admin notification not found or content mismatch. Expected partial: '{expected_admin_msg_content_partial}'. Log: {mock_telegram_client.sent_messages_log}"

    db_session.expire_all() # Expire cache before checking user status
    new_user_check = await db_session.get(NewUser, {"user_id": TEST_NEW_USER_ID, "chat_id": TEST_CHAT_ID})
    assert new_user_check is not None, "User was incorrectly removed from NewUser table"

    mock_telegram_client.kick_participant.assert_not_called()
    test_logger.info("Finished test_llm_failure_queues_check_and_notifies_admin.")
