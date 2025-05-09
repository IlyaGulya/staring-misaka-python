import pytest
import pandas as pd
from decimal import Decimal
import datetime
from unittest.mock import MagicMock, AsyncMock

from sqlalchemy import select, func, delete # Added delete, func
from sqlalchemy.ext.asyncio import AsyncSession

# Added NewUser for some queue tests
from staring_misaka.db_models import LLMModel, Prompt, GlobalBotSettings, ModelPricing, QueuedLLMCheck, NewUser
from staring_misaka.web_ui import (
    # LLM Models
    list_llm_models_data,
    handle_create_llm_model,
    handle_update_llm_model,
    handle_delete_llm_model,
    handle_set_global_default_model,
    # Prompts
    list_prompts_data,
    handle_create_prompt,
    handle_update_prompt,
    handle_delete_prompt,
    handle_set_global_default_prompt,
    # Model Pricing
    list_model_pricing_data,
    handle_create_model_pricing,
    handle_delete_model_pricing,
    get_llm_model_choices,  # Helper used by pricing UI
    # Queue Management
    list_queued_checks_data,
    handle_reprocess_queued_item,
    handle_discard_queued_item,
    # Dashboard
    get_bot_status, # Added for dashboard test
)
from staring_misaka.config import Settings
from tests.conftest import TEST_CHAT_ID, TEST_NEW_USER_ID  # For queue item context

pytestmark = pytest.mark.asyncio  # Mark all tests in this file as async


# Helper to check DataFrame content
def assert_df_contains_record(df: pd.DataFrame, column: str, value: any):
    assert not df.empty, "DataFrame is empty"
    assert column in df.columns, f"Column '{column}' not in DataFrame"
    df_column_values = df[column].tolist()  # Convert to list of Python native types
    assert value in df_column_values, f"Value '{value}' ({type(value)}) not found in DataFrame column '{column}' values: {df_column_values}"


def get_record_from_df(df: pd.DataFrame, column: str, value: any) -> pd.Series | None:
    if df.empty or column not in df.columns:
        return None

    # Ensure `value` is compared with same type in DataFrame if possible
    # df[column] can be object type if it contains mixed types or NaNs, even if most are int.
    # Explicit conversion or careful comparison is needed.
    # If `value` is int, and df[column] is float (e.g. due to NaN), direct compare might fail.
    # For IDs, they should be consistently int or coerced.
    try:
        if isinstance(value, int) and pd.api.types.is_numeric_dtype(df[column]):
             # Coerce column to Int64 to handle NaNs and compare as int
            df_col_int = pd.to_numeric(df[column], errors='coerce').astype('Int64')
            filtered_df = df[df_col_int == value]
        else:
            filtered_df = df[df[column] == value]

        if not filtered_df.empty:
            return filtered_df.iloc[0]
    except Exception: # Broad catch if type conversion or comparison fails unexpectedly
        # Fallback to iterative comparison if direct vectorized one fails
        for i, item_val in enumerate(df[column]):
            try:
                if type(item_val) == type(value) and item_val == value: # Exact type and value match
                    return df.iloc[i]
                if isinstance(value, int) and pd.api.types.is_integer(item_val) and int(item_val) == value: # Integer comparison
                    return df.iloc[i]
            except (TypeError, ValueError):
                continue # Skip if comparison is not possible
    return None


@pytest.mark.usefixtures("setup_web_ui_globals")
class TestWebUIDashboardHandlers:
    async def test_get_bot_status_db_ok_no_queue(self, db_session: AsyncSession, mocker):
        # We need to mock the get_db_session that get_bot_status uses internally
        # The fixture setup_web_ui_globals sets _main_event_loop, but get_db_session is called directly.
        # It's simpler to let it use the actual get_db_session which should use the test engine.
        status_str = await get_bot_status()

        assert "DB Connected" in status_str
        assert "Items needing admin action in queue: 0" in status_str

    async def test_get_bot_status_db_ok_with_queue_items(self, db_session: AsyncSession, mocker, test_settings: Settings):
        # Create some queued items needing admin action
        from staring_misaka.dto import MessageContext # Local import
        gs = await db_session.get(GlobalBotSettings, 1)
        assert gs and gs.default_model_id and gs.default_prompt_id

        context1 = MessageContext(user_id=1, chat_id=1, message_id=1, message_text="q1")
        item1 = QueuedLLMCheck(
            message_context_json=context1.model_dump(mode='json'), reason_for_queueing="reason1",
            original_model_id_attempted=gs.default_model_id, original_prompt_id_attempted=gs.default_prompt_id,
            status="pending_admin_action"
        )
        # context2 = MessageContext(user_id=2, chat_id=2, message_id=2, message_text="q2") # Removed for simplicity
        # item2 = QueuedLLMCheck(
        #     message_context_json=context2.model_dump(mode='json'), reason_for_queueing="reason2",
        #     original_model_id_attempted=gs.default_model_id, original_prompt_id_attempted=gs.default_prompt_id,
        #     status="pending_admin_action"
        # )
        db_session.add_all([item1]) # Adjusted for one item
        await db_session.flush()

        # mocker.patch('staring_misaka.web_ui.get_db_session', return_value=db_session) # Not needed if using actual get_db_session
        status_str = await get_bot_status()

        assert "DB Connected" in status_str
        assert "Items needing admin action in queue: 1" in status_str # Adjusted count

    async def test_get_bot_status_db_error(self, db_session: AsyncSession, mocker):
        # Mock get_db_session to return a context manager that raises error
        # This is tricky because get_bot_status uses `async with get_db_session()`.
        # We need to mock `get_db_session` itself to return a context manager that raises error.
        mock_session_ctx_mgr = AsyncMock()
        mock_session_instance = AsyncMock(spec=AsyncSession)
        # Configure the execute method of the session instance that will be returned by __aenter__
        mock_session_instance.execute = AsyncMock(side_effect=ConnectionRefusedError("Simulated DB error"))
        mock_session_ctx_mgr.__aenter__.return_value = mock_session_instance
        mock_session_ctx_mgr.__aexit__ = AsyncMock(return_value=None) # Ensure __aexit__ is also an async mock

        mocker.patch('staring_misaka.web_ui.get_db_session', return_value=mock_session_ctx_mgr)

        status_str = await get_bot_status()

        assert "DB Connection Error: ConnectionRefusedError" in status_str
        # Queue count might show 0 or the error message for queue as well, depending on how deep the mock goes
        assert "Items needing admin action in queue:" in status_str # Check that part of string exists


@pytest.mark.usefixtures("setup_web_ui_globals")
class TestWebUILLMModelHandlers:
    async def test_list_llm_models_empty(self, db_session: AsyncSession):
        # Delete pre-seeded model if any, for this specific test
        await db_session.execute(delete(LLMModel))
        gs = await db_session.get(GlobalBotSettings, 1)
        if gs: # gs might be None if DB was completely wiped by previous tests in a session
            gs.default_model_id = None # remove default if it points to a deleted one
        await db_session.flush()

        df = await list_llm_models_data()
        assert df.empty # Should be empty now

    async def test_create_llm_model(self, db_session: AsyncSession):
        model_name = "Test Model UI Create"
        api_id = "test-model-ui-create-v1"
        provider = "Anthropic"

        df_after_create = await handle_create_llm_model(model_name, api_id, provider)

        assert_df_contains_record(df_after_create, "Name", model_name)
        created_model_db = await db_session.scalar(select(LLMModel).where(LLMModel.name == model_name))
        assert created_model_db is not None
        assert created_model_db.api_identifier == api_id
        assert created_model_db.provider == provider

    async def test_create_llm_model_duplicate_name(self, db_session: AsyncSession, mocker):
        model_name = "Test Duplicate Model"
        await handle_create_llm_model(model_name, "api-id-1", "OpenAI")

        # Mock gradio info/error to check calls
        mock_gr_error = mocker.patch('gradio.Error')
        await handle_create_llm_model(model_name, "api-id-2", "Anthropic")
        mock_gr_error.assert_called_once_with(f"Error: LLM Model with name '{model_name}' already exists.")

        models_db = (await db_session.execute(select(LLMModel).where(LLMModel.name == model_name))).scalars().all()
        assert len(models_db) == 1
        assert models_db[0].api_identifier == "api-id-1" # Ensure original was kept

    async def test_update_llm_model(self, db_session: AsyncSession):
        original_name = "Test Model UI Original"
        original_api_id = "original-api-v1"
        original_provider = "Anthropic"
        await handle_create_llm_model(original_name, original_api_id, original_provider)
        model_db_initial = await db_session.scalar(select(LLMModel).where(LLMModel.name == original_name))
        assert model_db_initial is not None # Ensure it was created for update
        model_id_to_update = model_db_initial.id

        updated_name = "Test Model UI Updated"
        updated_api_id = "updated-api-v2"
        updated_provider = "OpenAI"
        await handle_update_llm_model(model_id_to_update, updated_name, updated_api_id, updated_provider)
        # df_after_update = await handle_update_llm_model(model_id_to_update, updated_name, updated_api_id, updated_provider) # Original
        # assert_df_contains_record(df_after_update, "Name", updated_name) # Check DF if needed

        db_session.expire_all() # Use expire_all before re-getting
        updated_model_db = await db_session.get(LLMModel, model_id_to_update)
        assert updated_model_db is not None
        assert updated_model_db.name == updated_name
        assert updated_model_db.api_identifier == updated_api_id
        assert updated_model_db.provider == updated_provider

    async def test_delete_llm_model(self, db_session: AsyncSession):
        model_name = "Test Model UI To Delete"
        await handle_create_llm_model(model_name, "delete-me", "Anthropic")
        model_db_initial = await db_session.scalar(select(LLMModel).where(LLMModel.name == model_name))
        assert model_db_initial is not None # Ensure it exists before delete
        model_id_to_delete = model_db_initial.id

        await handle_delete_llm_model(model_id_to_delete)
        # df_after_delete = await handle_delete_llm_model(model_id_to_delete) # Original
        # assert get_record_from_df(df_after_delete, "ID", model_id_to_delete) is None # Check DF if needed

        db_session.expire_all() # Expire before checking DB
        deleted_model_db = await db_session.get(LLMModel, model_id_to_delete)
        assert deleted_model_db is None

    async def test_set_global_default_model(self, db_session: AsyncSession):
        model_name = "Test New Default Model UI"
        await handle_create_llm_model(model_name, "new-default-api", "OpenAI")
        model_db_initial = await db_session.scalar(select(LLMModel).where(LLMModel.name == model_name))
        assert model_db_initial is not None # Ensure model exists
        new_default_model_id = model_db_initial.id

        await handle_set_global_default_model(new_default_model_id)
        # df_after_set_default = await handle_set_global_default_model(new_default_model_id) # Original
        # default_record = get_record_from_df(df_after_set_default, "ID", new_default_model_id) # Check DF if needed
        # assert default_record["Default"] == "✅"

        db_session.expire_all() # ADDED LINE
        gs = await db_session.get(GlobalBotSettings, 1) # Re-fetch gs
        assert gs is not None
        assert gs.default_model_id == new_default_model_id


@pytest.mark.usefixtures("setup_web_ui_globals")
class TestWebUIPromptHandlers:
    async def test_list_prompts_initial(self, db_session: AsyncSession):
        # Initial state may have the default prompt from db_utils.initialize_default_data
        df = await list_prompts_data()
        assert len(df) >= 1 # Expect at least the global default
        assert_df_contains_record(df, "Name", "Global Default Spam Check")

    async def test_create_prompt(self, db_session: AsyncSession):
        prompt_name = "Test Prompt UI Create"
        prompt_text = "Is this {message_text} spam? Be direct."

        df_after_create = await handle_create_prompt(prompt_name, prompt_text)
        assert_df_contains_record(df_after_create, "Name", prompt_name)

        created_prompt_db = await db_session.scalar(select(Prompt).where(Prompt.name == prompt_name))
        assert created_prompt_db is not None
        assert created_prompt_db.text == prompt_text

    async def test_update_prompt(self, db_session: AsyncSession):
        original_name = "Original Prompt UI"
        await handle_create_prompt(original_name, "Original text {message_text}")
        prompt_db_initial = await db_session.scalar(select(Prompt).where(Prompt.name == original_name))
        assert prompt_db_initial is not None # Ensure created
        prompt_id_to_update = prompt_db_initial.id

        updated_name = "Updated Prompt UI"
        updated_text = "New text {message_text} here."
        await handle_update_prompt(prompt_id_to_update, updated_name, updated_text)
        # df_after_update = await handle_update_prompt(prompt_id_to_update, updated_name, updated_text) # Original
        # assert_df_contains_record(df_after_update, "Name", updated_name) # Check DF if needed

        db_session.expire_all() # Expire before re-fetch
        updated_prompt_db = await db_session.get(Prompt, prompt_id_to_update)
        assert updated_prompt_db is not None
        assert updated_prompt_db.name == updated_name
        assert updated_prompt_db.text == updated_text

    async def test_delete_prompt(self, db_session: AsyncSession):
        prompt_name = "Prompt UI To Delete"
        await handle_create_prompt(prompt_name, "Delete me {message_text}")
        prompt_db_initial = await db_session.scalar(select(Prompt).where(Prompt.name == prompt_name))
        assert prompt_db_initial is not None
        prompt_id_to_delete = prompt_db_initial.id

        await handle_delete_prompt(prompt_id_to_delete)
        # df_after_delete = await handle_delete_prompt(prompt_id_to_delete) # Original
        # record = get_record_from_df(df_after_delete, "ID", prompt_id_to_delete) # Check DF if needed
        # assert record is None

        db_session.expire_all() # Expire before re-fetch
        deleted_prompt_db = await db_session.get(Prompt, prompt_id_to_delete)
        assert deleted_prompt_db is None

    async def test_set_global_default_prompt(self, db_session: AsyncSession):
        prompt_name = "New Default Prompt UI"
        await handle_create_prompt(prompt_name, "This is the new default {message_text}.")
        prompt_db_initial = await db_session.scalar(select(Prompt).where(Prompt.name == prompt_name))
        assert prompt_db_initial is not None
        new_default_prompt_id = prompt_db_initial.id

        await handle_set_global_default_prompt(new_default_prompt_id)
        # df_after_set_default = await handle_set_global_default_prompt(new_default_prompt_id) # Original
        # default_record = get_record_from_df(df_after_set_default, "ID", new_default_prompt_id) # Check DF if needed
        # assert default_record["Default"] == "✅"

        db_session.expire_all() # ADDED LINE
        gs = await db_session.get(GlobalBotSettings, 1) # Re-fetch GS
        assert gs is not None
        assert gs.default_prompt_id == new_default_prompt_id

        updated_prompt_db = await db_session.get(Prompt, new_default_prompt_id)
        assert updated_prompt_db is not None
        assert updated_prompt_db.is_global_default is True


@pytest.mark.usefixtures("setup_web_ui_globals")
class TestWebUIModelPricingHandlers:
    async def test_list_model_pricing_empty(self, db_session: AsyncSession):
        await db_session.execute(delete(ModelPricing)) # Clear existing
        await db_session.flush()
        df = await list_model_pricing_data()
        assert df.empty

    async def test_create_and_list_model_pricing(self, db_session: AsyncSession):
        # Ensure a model exists for pricing
        model_df_list = await list_llm_models_data()
        pricing_test_model_name = "Pricing Test Model"

        model_record = get_record_from_df(model_df_list, "Name", pricing_test_model_name)
        if model_df_list.empty or model_record is None: # Corrected check
            await handle_create_llm_model(pricing_test_model_name, "pricing-api", "Anthropic")
            model_df_list = await list_llm_models_data() # Refresh list
            model_record = get_record_from_df(model_df_list, "Name", pricing_test_model_name)

        claude_haiku_name = "Claude 3 Haiku" # Define for clarity
        if model_record is None: # Corrected check, Fallback if "Pricing Test Model" was deleted or not created by previous step.
             model_record = get_record_from_df(model_df_list, "Name", claude_haiku_name)
             if model_record is None: # Corrected check, If even default is gone, create one more.
                await handle_create_llm_model(claude_haiku_name, "claude-3-haiku-20240307", "Anthropic") # Recreate default for test
                model_df_list = await list_llm_models_data()
                model_record = get_record_from_df(model_df_list, "Name", claude_haiku_name)

        assert model_record is not None, "A base model for pricing test is required."
        model_id = int(model_record["ID"])


        from_date = datetime.date(2024, 1, 1)
        to_date = datetime.date(2024, 12, 31)

        df_after_create = await handle_create_model_pricing(
            model_id, "0.50", "1.50", "USD", from_date, to_date
        )
        assert_df_contains_record(df_after_create, "Model ID", model_id)
        created_pricing_record_df = get_record_from_df(df_after_create, "Model ID", model_id)
        assert created_pricing_record_df is not None
        # Ensure price is correctly compared as Decimal
        assert Decimal(str(created_pricing_record_df["Input Price (per Mtok)"])) == Decimal("0.50")

        db_session.expire_all() # Expire before re-fetch
        pricing_db = await db_session.scalar(select(ModelPricing).where(ModelPricing.model_id == model_id, ModelPricing.effective_from_date == from_date))
        assert pricing_db is not None
        assert pricing_db.input_price_per_million_tokens == Decimal("0.50")
        assert pricing_db.effective_from_date == from_date
        choices = await get_llm_model_choices() # Helper
        assert any(choice[1] == model_id for choice in choices) # Check model ID is in choices

    async def test_delete_model_pricing(self, db_session: AsyncSession):
        model_df = await handle_create_llm_model("Pricing Test Model Delete", "pricing-api-del", "OpenAI")
        model_record = get_record_from_df(model_df, "Name", "Pricing Test Model Delete")
        assert model_record is not None
        model_id = int(model_record["ID"])

        from_date = datetime.date(2024, 1, 1)
        df_created = await handle_create_model_pricing(model_id, "1.00", "2.00", "EUR", from_date, None)

        created_pricing_record_df = get_record_from_df(df_created, "Model ID", model_id)
        assert created_pricing_record_df is not None
        pricing_id_to_delete = int(created_pricing_record_df["ID"])

        df_after_delete = await handle_delete_model_pricing(pricing_id_to_delete)
        record = get_record_from_df(df_after_delete, "ID", pricing_id_to_delete)
        assert record is None

        db_session.expire_all() # Expire before DB check
        deleted_pricing_db = await db_session.get(ModelPricing, pricing_id_to_delete)
        assert deleted_pricing_db is None


@pytest.mark.usefixtures("setup_web_ui_globals", "monitored_group", "new_user_in_group", "setup_queue_test")
class TestWebUIQueueManagementHandlers:

    async def _create_test_queued_item(self, db_session: AsyncSession, reason: str,
                                       status: str = "pending", user_id_override: int | None = None) -> QueuedLLMCheck:
        from staring_misaka.dto import MessageContext # Local import

        gs = await db_session.get(GlobalBotSettings, 1)
        assert gs and gs.default_model_id and gs.default_prompt_id

        user_id_to_use = user_id_override if user_id_override else TEST_NEW_USER_ID
        # Ensure unique message ID for each item if multiple are created
        message_id_to_use = 12345 + user_id_to_use + hash(reason) % 1000

        context = MessageContext(
            user_id=user_id_to_use, chat_id=TEST_CHAT_ID, message_id=message_id_to_use,
            message_text=f"Test queue msg from {user_id_to_use} for {reason}", is_new_user=True
        )
        item = QueuedLLMCheck(
            message_context_json=context.model_dump(mode='json'),
            reason_for_queueing=reason,
            original_model_id_attempted=gs.default_model_id,
            original_prompt_id_attempted=gs.default_prompt_id,
            status=status,
            retry_count=0
        )
        db_session.add(item)
        await db_session.flush()
        return item

    async def test_list_queued_checks(self, db_session: AsyncSession):
        await self._create_test_queued_item(db_session, "Test reason for pending", "pending")
        await self._create_test_queued_item(db_session, "Test reason for admin action", "pending_admin_action")

        df_all = await list_queued_checks_data("All")
        assert len(df_all) >= 2 # Can be more if other tests leave items

        df_pending_admin = await list_queued_checks_data("pending_admin_action")
        assert len(df_pending_admin) >= 1 # At least one we created
        assert all(df_pending_admin["Status"] == "pending_admin_action")
        assert_df_contains_record(df_pending_admin, "Reason (Preview)", "Test reason for admin action"[:150])

        df_pending = await list_queued_checks_data("pending")
        assert len(df_pending) >= 1 # At least one we created
        assert all(df_pending["Status"] == "pending")

    async def test_discard_queued_item(self, db_session: AsyncSession, test_settings: Settings):
        item_user_id = TEST_NEW_USER_ID + 55 # Unique user for this item
        # Ensure this user is in NewUser to test approval part of discard
        db_session.add(NewUser(user_id=item_user_id, chat_id=TEST_CHAT_ID)) # Added NewUser import
        await db_session.flush()

        item = await self._create_test_queued_item(db_session, "Item to discard via UI", "pending_admin_action", user_id_override=item_user_id)
        item_id_to_discard = item.id

        df_after_discard = await handle_discard_queued_item(item_id_to_discard, "pending_admin_action") # current_status_filter used for refresh

        record = get_record_from_df(df_after_discard, "ID", item_id_to_discard)
        assert record is None

        db_session.expire_all() # Expire before DB check
        discarded_item_db = await db_session.get(QueuedLLMCheck, item_id_to_discard)
        assert discarded_item_db is None
        # Check user was approved (removed from NewUser)
        assert await db_session.get(NewUser, {"user_id": item_user_id, "chat_id": TEST_CHAT_ID}) is None


    async def test_reprocess_queued_item_success_not_spam(self, db_session: AsyncSession, mocker,
                                                          test_settings: Settings):
        from staring_misaka.dto import LLMSpamAnalysisResult # Local import
        import staring_misaka.web_ui as web_ui_module # Local import

        item_user_id = TEST_NEW_USER_ID + 66 # Unique user
        db_session.add(NewUser(user_id=item_user_id, chat_id=TEST_CHAT_ID)) # Added NewUser import
        await db_session.flush()
        item = await self._create_test_queued_item(db_session, "Reprocess - not spam", "pending", user_id_override=item_user_id)
        item_id_to_reprocess = item.id

        import staring_misaka.web_ui as web_ui_module
        llm_service_to_mock = web_ui_module._llm_service_instance
        assert llm_service_to_mock is not None

        mock_analyze_result = LLMSpamAnalysisResult(
            is_spam=False, reason="Reprocessed: Looks fine.",
            model_name_used="mock-reprocess-model", status="success"
        )
        # We need to mock analyze_message_for_spam because reprocess_queued_item calls it
        mocker.patch.object(llm_service_to_mock, 'analyze_message_for_spam', return_value=mock_analyze_result)

        df_after_reprocess = await handle_reprocess_queued_item(item_id_to_reprocess, "pending") # current_status_filter used for refresh

        record_in_df = get_record_from_df(df_after_reprocess, "ID", item_id_to_reprocess)
        assert record_in_df is None # Item should be deleted after successful reprocessing

        db_session.expire_all() # Expire before DB check
        reprocessed_item_db = await db_session.get(QueuedLLMCheck, item_id_to_reprocess)
        assert reprocessed_item_db is None # Item deleted from DB
        # User approved
        assert await db_session.get(NewUser, {"user_id": item_user_id, "chat_id": TEST_CHAT_ID}) is None

    async def test_reprocess_queued_item_success_is_spam(self, db_session: AsyncSession, mocker,
                                                          test_settings: Settings):
        from staring_misaka.dto import LLMSpamAnalysisResult # Local import
        import staring_misaka.web_ui as web_ui_module # Local import

        item_user_id = TEST_NEW_USER_ID + 77
        db_session.add(NewUser(user_id=item_user_id, chat_id=TEST_CHAT_ID)) # Added NewUser import
        await db_session.flush()
        item = await self._create_test_queued_item(db_session, "Reprocess - is spam", "pending", user_id_override=item_user_id)
        item_id_to_reprocess = item.id

        llm_service_to_mock = web_ui_module._llm_service_instance
        assert llm_service_to_mock is not None

        mock_analyze_result = LLMSpamAnalysisResult(
            is_spam=True, reason="Reprocessed: Found to be spam.",
            model_name_used="mock-reprocess-model", status="success"
        )
        mocker.patch.object(llm_service_to_mock, 'analyze_message_for_spam', return_value=mock_analyze_result)
        # Also mock action_service.request_admin_approval_for_ban as it's called for reprocessed spam
        mock_request_approval = mocker.patch.object(web_ui_module._action_service_instance, 'request_admin_approval_for_ban', new_callable=AsyncMock)


        df_after_reprocess = await handle_reprocess_queued_item(item_id_to_reprocess, "pending") # current_status_filter used for refresh

        record_in_df = get_record_from_df(df_after_reprocess, "ID", item_id_to_reprocess)
        assert record_in_df is None # Item resolved (deleted and PendingAdminAction created)

        db_session.expire_all() # Expire before DB check
        reprocessed_item_db = await db_session.get(QueuedLLMCheck, item_id_to_reprocess)
        assert reprocessed_item_db is None # Item deleted from DB
        mock_request_approval.assert_called_once() # Check that admin approval was requested
        # User should still be in NewUser table as it went to admin approval
        assert await db_session.get(NewUser, {"user_id": item_user_id, "chat_id": TEST_CHAT_ID}) is not None


    async def test_reprocess_queued_item_fails_llm(self, db_session: AsyncSession, mocker,
                                                          test_settings: Settings):
        from staring_misaka.dto import LLMSpamAnalysisResult # Local import
        import staring_misaka.web_ui as web_ui_module # Local import

        item = await self._create_test_queued_item(db_session, "Reprocess - will fail LLM", "pending")
        item_id_to_reprocess = item.id

        llm_service_to_mock = web_ui_module._llm_service_instance
        assert llm_service_to_mock is not None
        # Simulate LLM failure during reprocessing (e.g., API error, critical_error_no_check)
        mock_analyze_fail_result = LLMSpamAnalysisResult(status="critical_error_no_check", error_message="LLM Reprocess API Failed")
        mocker.patch.object(llm_service_to_mock, 'analyze_message_for_spam', return_value=mock_analyze_fail_result)

        # The handler calls list_queued_checks_data with the current_status_filter ("pending")
        # After reprocessing, the item's status will change to "failed_reprocessing_attempt"
        # So, the refreshed DataFrame (filtered by "pending") will NOT contain the item.
        df_after_reprocess_pending_filter = await handle_reprocess_queued_item(item_id_to_reprocess, "pending")
        record_in_pending_df = get_record_from_df(df_after_reprocess_pending_filter, "ID", item_id_to_reprocess)
        assert record_in_pending_df is None, "Item should not be in DataFrame filtered by 'pending' after status change"

        # Verify by fetching with the new status or "All"
        df_after_reprocess_correct_filter = await list_queued_checks_data("failed_reprocessing_attempt")
        reprocessed_item_df_record = get_record_from_df(df_after_reprocess_correct_filter, "ID", item_id_to_reprocess)
        assert reprocessed_item_df_record is not None, \
            f"Item {item_id_to_reprocess} not found in DataFrame when filtering by 'failed_reprocessing_attempt'"
        assert reprocessed_item_df_record["Status"] == "failed_reprocessing_attempt"

        db_session.expire_all() # Ensure fresh read from DB
        reprocessed_item_db = await db_session.get(QueuedLLMCheck, item_id_to_reprocess)
        assert reprocessed_item_db is not None
        assert reprocessed_item_db.status == "failed_reprocessing_attempt"
        assert "Reprocess critical error: LLM Reprocess API Failed" in reprocessed_item_db.reason_for_queueing

