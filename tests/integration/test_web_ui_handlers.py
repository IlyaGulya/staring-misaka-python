# tests/integration/test_web_ui_handlers.py
from unittest.mock import AsyncMock

import pandas as pd
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from staring_misaka.config import Settings # Required for setup_web_ui_globals
import staring_misaka.web_ui as web_ui_module # For setting _app_settings in dashboard tests

from staring_misaka.db_models import GlobalBotSettings, LLMModel, NewUser, Prompt, QueuedLLMCheck
from staring_misaka.web_ui import (
    # Dashboard
    get_bot_status,
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
    # Queue Management
    list_queued_checks_data,
    handle_discard_queued_item,
    handle_reprocess_queued_item,
)
from tests.conftest import TEST_CHAT_ID, TEST_NEW_USER_ID

pytestmark = pytest.mark.asyncio


# Helper to check DataFrame content
def assert_df_contains_record(df: pd.DataFrame, column: str, value: any):
    assert not df.empty, "DataFrame is empty"
    assert column in df.columns, f"Column '{column}' not in DataFrame"
    df_column_values = df[column].tolist()
    assert value in df_column_values, f"Value '{value}' ({type(value)}) not found in DataFrame column '{column}' values: {df_column_values}"


def get_record_from_df(df: pd.DataFrame, column: str, value: any) -> pd.Series | None:
    if df.empty or column not in df.columns:
        return None
    try:
        if isinstance(value, int) and pd.api.types.is_numeric_dtype(df[column]):
            df_col_int = pd.to_numeric(df[column], errors='coerce').astype('Int64')
            filtered_df = df[df_col_int == value]
        else:
            filtered_df = df[df[column] == value]
        if not filtered_df.empty:
            return filtered_df.iloc[0]
    except Exception:
        for i, item_val in enumerate(df[column]):
            try:
                if type(item_val) == type(value) and item_val == value:
                    return df.iloc[i]
                if isinstance(value, int) and pd.api.types.is_integer(item_val) and int(item_val) == value:
                    return df.iloc[i]
            except (TypeError, ValueError):
                continue
    return None


@pytest.mark.usefixtures("setup_web_ui_globals")
class TestWebUIDashboardHandlers:
    async def test_get_bot_status_db_ok_no_queue(self, db_session: AsyncSession, mocker, test_settings: Settings):
        web_ui_module._app_settings = test_settings # Ensure _app_settings is set for get_bot_status
        status_str = await get_bot_status()
        assert "DB Connected" in status_str
        assert "Items needing admin action in queue: 0" in status_str
        if test_settings.loaded_pricing_config and test_settings.loaded_pricing_config.models:
            assert "Pricing Config: Loaded" in status_str
        else:
            assert f"Pricing Config: Not loaded or empty (Path: {test_settings.pricing_config_file_path})" in status_str

    async def test_get_bot_status_db_ok_with_queue_items(self, db_session: AsyncSession, mocker, test_settings: Settings):
        from staring_misaka.dto import MessageContext
        gs = await db_session.get(GlobalBotSettings, 1)
        assert gs and gs.default_model_id and gs.default_prompt_id
        context1 = MessageContext(user_id=1, chat_id=1, message_id=1, message_text="q1")
        item1 = QueuedLLMCheck(
            message_context_json=context1.model_dump(mode='json'), reason_for_queueing="reason1",
            original_model_id_attempted=gs.default_model_id, original_prompt_id_attempted=gs.default_prompt_id,
            status="pending_admin_action"
        )
        db_session.add(item1) # Corrected from add_all
        await db_session.flush()
        web_ui_module._app_settings = test_settings
        status_str = await get_bot_status()
        assert "DB Connected" in status_str
        assert "Items needing admin action in queue: 1" in status_str
        if test_settings.loaded_pricing_config and test_settings.loaded_pricing_config.models:
            assert "Pricing Config: Loaded" in status_str
        else:
            assert f"Pricing Config: Not loaded or empty (Path: {test_settings.pricing_config_file_path})" in status_str

    async def test_get_bot_status_db_error(self, db_session: AsyncSession, mocker, test_settings: Settings):
        mock_session_ctx_mgr = AsyncMock()
        mock_session_instance = AsyncMock(spec=AsyncSession)
        mock_session_instance.execute = AsyncMock(side_effect=ConnectionRefusedError("Simulated DB error"))
        mock_session_ctx_mgr.__aenter__.return_value = mock_session_instance
        mock_session_ctx_mgr.__aexit__ = AsyncMock(return_value=None)
        mocker.patch('staring_misaka.web_ui.get_db_session', return_value=mock_session_ctx_mgr)
        web_ui_module._app_settings = test_settings
        status_str = await get_bot_status()
        assert "DB Connection Error: ConnectionRefusedError" in status_str
        assert "Items needing admin action in queue:" in status_str # Count might be 0 or more depending on mock
        if test_settings.loaded_pricing_config and test_settings.loaded_pricing_config.models:
            assert "Pricing Config: Loaded" in status_str
        else:
            assert f"Pricing Config: Not loaded or empty (Path: {test_settings.pricing_config_file_path})" in status_str


@pytest.mark.usefixtures("setup_web_ui_globals")
class TestWebUILLMModelHandlers:
    async def test_list_llm_models_empty(self, db_session: AsyncSession):
        await db_session.execute(delete(LLMModel))
        gs = await db_session.get(GlobalBotSettings, 1)
        if gs:
            gs.default_model_id = None
        await db_session.flush()
        df = await list_llm_models_data()
        assert df.empty

    async def test_create_llm_model(self, db_session: AsyncSession):
        model_name = "Test Model UI Create"
        api_id = "test-model-ui-create-v1"
        provider = "Anthropic"
        # The handler now returns the DataFrame, which is what we test.
        df_after_create = await handle_create_llm_model(model_name, api_id, provider)
        assert_df_contains_record(df_after_create, "Name", model_name)
        created_model_db = await db_session.scalar(select(LLMModel).where(LLMModel.name == model_name))
        assert created_model_db is not None
        assert created_model_db.api_identifier == api_id
        assert created_model_db.provider == provider

    async def test_create_llm_model_duplicate_name(self, db_session: AsyncSession, mocker):
        model_name = "Test Duplicate Model"
        await handle_create_llm_model(model_name, "api-id-1", "OpenAI")
        mock_gr_error = mocker.patch('gradio.Error')
        # This call will trigger gr.Error internally, and the function returns the current state of the DataFrame
        await handle_create_llm_model(model_name, "api-id-2", "Anthropic")
        mock_gr_error.assert_called_once_with(f"Error: LLM Model with name '{model_name}' already exists.")
        models_db = (await db_session.execute(select(LLMModel).where(LLMModel.name == model_name))).scalars().all()
        assert len(models_db) == 1
        assert models_db[0].api_identifier == "api-id-1"

    async def test_update_llm_model(self, db_session: AsyncSession):
        original_name = "Test Model UI Original"
        original_api_id = "original-api-v1"
        original_provider = "Anthropic"
        await handle_create_llm_model(original_name, original_api_id, original_provider)
        model_db_initial = await db_session.scalar(select(LLMModel).where(LLMModel.name == original_name))
        assert model_db_initial is not None
        model_id_to_update = model_db_initial.id
        updated_name = "Test Model UI Updated"
        updated_api_id = "updated-api-v2"
        updated_provider = "OpenAI"
        # The handler returns the updated DataFrame
        df_after_update = await handle_update_llm_model(model_id_to_update, updated_name, updated_api_id, updated_provider)
        assert_df_contains_record(df_after_update, "Name", updated_name) # Check the returned DF
        db_session.expire_all()
        updated_model_db = await db_session.get(LLMModel, model_id_to_update)
        assert updated_model_db is not None
        assert updated_model_db.name == updated_name
        assert updated_model_db.api_identifier == updated_api_id
        assert updated_model_db.provider == updated_provider

    async def test_delete_llm_model(self, db_session: AsyncSession):
        model_name = "Test Model UI To Delete"
        await handle_create_llm_model(model_name, "delete-me", "Anthropic")
        model_db_initial = await db_session.scalar(select(LLMModel).where(LLMModel.name == model_name))
        assert model_db_initial is not None
        model_id_to_delete = model_db_initial.id
        # The handler returns the DataFrame after deletion
        df_after_delete = await handle_delete_llm_model(model_id_to_delete)
        assert get_record_from_df(df_after_delete, "ID", model_id_to_delete) is None # Check DF
        db_session.expire_all()
        deleted_model_db = await db_session.get(LLMModel, int(model_id_to_delete))
        assert deleted_model_db is None

    async def test_set_global_default_model(self, db_session: AsyncSession):
        model_name = "Test New Default Model UI"
        await handle_create_llm_model(model_name, "new-default-api", "OpenAI")
        model_db_initial = await db_session.scalar(select(LLMModel).where(LLMModel.name == model_name))
        assert model_db_initial is not None
        new_default_model_id = model_db_initial.id
        # The handler returns the DataFrame with the new default marked
        df_after_set_default = await handle_set_global_default_model(new_default_model_id)
        default_record = get_record_from_df(df_after_set_default, "ID", new_default_model_id)
        assert default_record is not None # Check the record exists
        assert default_record["Default"] == "✅" # Check the default marker in DF
        db_session.expire_all()
        gs = await db_session.get(GlobalBotSettings, 1)
        assert gs is not None
        assert gs.default_model_id == new_default_model_id


@pytest.mark.usefixtures("setup_web_ui_globals")
class TestWebUIPromptHandlers:
    async def test_list_prompts_initial(self, db_session: AsyncSession):
        df = await list_prompts_data()
        assert len(df) >= 1
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
        assert prompt_db_initial is not None
        prompt_id_to_update = prompt_db_initial.id
        updated_name = "Updated Prompt UI"
        updated_text = "New text {message_text} here."
        df_after_update = await handle_update_prompt(prompt_id_to_update, updated_name, updated_text)
        assert_df_contains_record(df_after_update, "Name", updated_name)
        db_session.expire_all()
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
        df_after_delete = await handle_delete_prompt(prompt_id_to_delete)
        record = get_record_from_df(df_after_delete, "ID", prompt_id_to_delete)
        assert record is None
        db_session.expire_all()
        deleted_prompt_db = await db_session.get(Prompt, prompt_id_to_delete)
        assert deleted_prompt_db is None

    async def test_set_global_default_prompt(self, db_session: AsyncSession):
        prompt_name = "New Default Prompt UI"
        await handle_create_prompt(prompt_name, "This is the new default {message_text}.")
        prompt_db_initial = await db_session.scalar(select(Prompt).where(Prompt.name == prompt_name))
        assert prompt_db_initial is not None
        new_default_prompt_id = prompt_db_initial.id
        df_after_set_default = await handle_set_global_default_prompt(new_default_prompt_id)
        default_record = get_record_from_df(df_after_set_default, "ID", new_default_prompt_id)
        assert default_record is not None
        assert default_record["Default"] == "✅"
        db_session.expire_all()
        gs = await db_session.get(GlobalBotSettings, 1)
        assert gs is not None
        assert gs.default_prompt_id == new_default_prompt_id
        updated_prompt_db = await db_session.get(Prompt, new_default_prompt_id)
        assert updated_prompt_db is not None
        assert updated_prompt_db.is_global_default is True


# Model Pricing tests are removed as this functionality is now YAML-based and UI is removed.


@pytest.mark.usefixtures("setup_web_ui_globals", "monitored_group", "new_user_in_group", "setup_queue_test")
class TestWebUIQueueManagementHandlers:

    async def _create_test_queued_item(self, db_session: AsyncSession, reason: str,
                                       status: str = "pending", user_id_override: int | None = None) -> QueuedLLMCheck:
        from staring_misaka.dto import MessageContext  # Local import

        gs = await db_session.get(GlobalBotSettings, 1)
        assert gs
        assert gs.default_model_id
        assert gs.default_prompt_id

        user_id_to_use = user_id_override if user_id_override else TEST_NEW_USER_ID
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
        assert len(df_all) >= 2 

        df_pending_admin = await list_queued_checks_data("pending_admin_action")
        assert len(df_pending_admin) >= 1 
        assert all(df_pending_admin["Status"] == "pending_admin_action")
        assert_df_contains_record(df_pending_admin, "Reason (Preview)", "Test reason for admin action"[:150])

        df_pending = await list_queued_checks_data("pending")
        assert len(df_pending) >= 1 
        assert all(df_pending["Status"] == "pending")

    async def test_discard_queued_item(self, db_session: AsyncSession, test_settings: Settings):
        item_user_id = TEST_NEW_USER_ID + 55 
        db_session.add(NewUser(user_id=item_user_id, chat_id=TEST_CHAT_ID)) 
        await db_session.flush()

        item = await self._create_test_queued_item(db_session, "Item to discard via UI", "pending_admin_action", user_id_override=item_user_id)
        item_id_to_discard = item.id

        df_after_discard = await handle_discard_queued_item(item_id_to_discard, "pending_admin_action") 

        record = get_record_from_df(df_after_discard, "ID", item_id_to_discard)
        assert record is None

        db_session.expire_all() 
        discarded_item_db = await db_session.get(QueuedLLMCheck, item_id_to_discard)
        assert discarded_item_db is None
        assert await db_session.get(NewUser, {"user_id": item_user_id, "chat_id": TEST_CHAT_ID}) is None


    async def test_reprocess_queued_item_success_not_spam(self, db_session: AsyncSession, mocker,
                                                          test_settings: Settings):
        import staring_misaka.web_ui as web_ui_module_local  # Local import to avoid conflict
        from staring_misaka.dto import LLMSpamAnalysisResult  # Local import

        item_user_id = TEST_NEW_USER_ID + 66 
        db_session.add(NewUser(user_id=item_user_id, chat_id=TEST_CHAT_ID)) 
        await db_session.flush()
        item = await self._create_test_queued_item(db_session, "Reprocess - not spam", "pending", user_id_override=item_user_id)
        item_id_to_reprocess = item.id

        llm_service_to_mock = web_ui_module_local._llm_service_instance # Use aliased import
        assert llm_service_to_mock is not None

        mock_analyze_result = LLMSpamAnalysisResult(
            is_spam=False, reason="Reprocessed: Looks fine.",
            model_name_used="mock-reprocess-model", status="success"
        )
        mocker.patch.object(llm_service_to_mock, 'analyze_message_for_spam', return_value=mock_analyze_result)

        df_after_reprocess = await handle_reprocess_queued_item(item_id_to_reprocess, "pending") 

        record_in_df = get_record_from_df(df_after_reprocess, "ID", item_id_to_reprocess)
        assert record_in_df is None 

        db_session.expire_all() 
        reprocessed_item_db = await db_session.get(QueuedLLMCheck, item_id_to_reprocess)
        assert reprocessed_item_db is None 
        assert await db_session.get(NewUser, {"user_id": item_user_id, "chat_id": TEST_CHAT_ID}) is None

    async def test_reprocess_queued_item_success_is_spam(self, db_session: AsyncSession, mocker,
                                                          test_settings: Settings):
        import staring_misaka.web_ui as web_ui_module_local  # Local import
        from staring_misaka.dto import LLMSpamAnalysisResult  # Local import

        item_user_id = TEST_NEW_USER_ID + 77
        db_session.add(NewUser(user_id=item_user_id, chat_id=TEST_CHAT_ID)) 
        await db_session.flush()
        item = await self._create_test_queued_item(db_session, "Reprocess - is spam", "pending", user_id_override=item_user_id)
        item_id_to_reprocess = item.id

        llm_service_to_mock = web_ui_module_local._llm_service_instance # Use aliased import
        assert llm_service_to_mock is not None

        mock_analyze_result = LLMSpamAnalysisResult(
            is_spam=True, reason="Reprocessed: Found to be spam.",
            model_name_used="mock-reprocess-model", status="success"
        )
        mocker.patch.object(llm_service_to_mock, 'analyze_message_for_spam', return_value=mock_analyze_result)
        mock_request_approval = mocker.patch.object(web_ui_module_local._action_service_instance, 'request_admin_approval_for_ban', new_callable=AsyncMock) # Use aliased


        df_after_reprocess = await handle_reprocess_queued_item(item_id_to_reprocess, "pending") 

        record_in_df = get_record_from_df(df_after_reprocess, "ID", item_id_to_reprocess)
        assert record_in_df is None 

        db_session.expire_all() 
        reprocessed_item_db = await db_session.get(QueuedLLMCheck, item_id_to_reprocess)
        assert reprocessed_item_db is None 
        mock_request_approval.assert_called_once() 
        assert await db_session.get(NewUser, {"user_id": item_user_id, "chat_id": TEST_CHAT_ID}) is not None


    async def test_reprocess_queued_item_fails_llm(self, db_session: AsyncSession, mocker,
                                                          test_settings: Settings):
        import staring_misaka.web_ui as web_ui_module_local  # Local import
        from staring_misaka.dto import LLMSpamAnalysisResult  # Local import

        item = await self._create_test_queued_item(db_session, "Reprocess - will fail LLM", "pending")
        item_id_to_reprocess = item.id

        llm_service_to_mock = web_ui_module_local._llm_service_instance # Use aliased import
        assert llm_service_to_mock is not None
        mock_analyze_fail_result = LLMSpamAnalysisResult(status="critical_error_no_check", error_message="LLM Reprocess API Failed")
        mocker.patch.object(llm_service_to_mock, 'analyze_message_for_spam', return_value=mock_analyze_fail_result)

        df_after_reprocess_pending_filter = await handle_reprocess_queued_item(item_id_to_reprocess, "pending")
        record_in_pending_df = get_record_from_df(df_after_reprocess_pending_filter, "ID", item_id_to_reprocess)
        assert record_in_pending_df is None, "Item should not be in DataFrame filtered by 'pending' after status change"

        df_after_reprocess_correct_filter = await list_queued_checks_data("failed_reprocessing_attempt")
        reprocessed_item_df_record = get_record_from_df(df_after_reprocess_correct_filter, "ID", item_id_to_reprocess)
        assert reprocessed_item_df_record is not None, \
            f"Item {item_id_to_reprocess} not found in DataFrame when filtering by 'failed_reprocessing_attempt'"
        assert reprocessed_item_df_record["Status"] == "failed_reprocessing_attempt"

        db_session.expire_all() 
        reprocessed_item_db = await db_session.get(QueuedLLMCheck, item_id_to_reprocess)
        assert reprocessed_item_db is not None
        assert reprocessed_item_db.status == "failed_reprocessing_attempt"
        assert "Reprocess critical error: LLM Reprocess API Failed" in reprocessed_item_db.reason_for_queueing