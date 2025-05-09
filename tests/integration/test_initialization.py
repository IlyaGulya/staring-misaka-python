# tests/integration/test_initialization.py
import pytest
from sqlalchemy import select, func # Added func

from staring_misaka.db_models import GlobalBotSettings, Prompt, LLMModel
from staring_misaka.db_utils import initialize_default_data, create_tables # Assuming init_db is called by db_engine fixture
from tests.conftest import TEST_SUPER_ADMIN_ID # Import from conftest

pytestmark = pytest.mark.asyncio

async def test_bot_startup_and_default_data_initialization(
    db_session, # Uses the session-scoped db_engine which calls init_db, create_tables
    test_settings
):
    """
    GIVEN an empty or partially initialized database
    WHEN initialize_default_data is called
    THEN essential default records (GlobalBotSettings, default Prompt, default LLMModel) should exist and be correctly configured.
    """
    # The db_engine fixture already calls create_tables and initialize_default_data once per session.
    # This test essentially verifies the outcome of that session-scoped initialization.
    # To test re-running initialize_default_data for idempotency, we might need a more function-scoped DB setup.
    # For now, let's verify the state after the session-scoped setup.

    # 1. Verify GlobalBotSettings
    gs = await db_session.get(GlobalBotSettings, 1)
    assert gs is not None, "GlobalBotSettings record not found."
    assert gs.super_admin_id == test_settings.admin_id, "Super admin ID in GlobalBotSettings is incorrect."
    assert gs.default_prompt_id is not None, "Default prompt ID not set in GlobalBotSettings."
    assert gs.default_model_id is not None, "Default model ID not set in GlobalBotSettings."

    # 2. Verify Default Prompt
    default_prompt = await db_session.get(Prompt, gs.default_prompt_id)
    assert default_prompt is not None, "Default prompt record not found using ID from GlobalBotSettings."
    assert default_prompt.name == "Global Default Spam Check", "Default prompt name is incorrect."
    assert default_prompt.is_global_default is True, "Default prompt is_global_default flag is not True."
    assert "{message_text}" in default_prompt.text, "Default prompt text is missing placeholder."

    # 3. Verify Default LLMModel
    default_model = await db_session.get(LLMModel, gs.default_model_id)
    assert default_model is not None, "Default LLMModel record not found using ID from GlobalBotSettings."
    assert default_model.name == "Claude 3 Haiku", "Default model name is incorrect."
    assert default_model.api_identifier == "claude-3-haiku-20240307", "Default model API identifier is incorrect."
    assert default_model.provider == "Anthropic", "Default model provider is incorrect."

    # 4. Test Idempotency (Optional: more involved if strict re-run is tested)
    # Calling it again should not create duplicates or fail.
    # The current initialize_default_data has checks to prevent duplicate creation.
    # We can simulate this by calling it again and then re-verifying counts or specific fields.
    current_prompt_count = await db_session.scalar(select(func.count(Prompt.id)))
    current_model_count = await db_session.scalar(select(func.count(LLMModel.id)))

    await initialize_default_data(test_settings) # Call it again (uses its own session)
    # await db_session.flush() # Not strictly needed as initialize_default_data commits.
    db_session.expire_all() # Crucial to see changes made by another session context


    new_prompt_count = await db_session.scalar(select(func.count(Prompt.id)))
    new_model_count = await db_session.scalar(select(func.count(LLMModel.id)))

    assert new_prompt_count == current_prompt_count, "initialize_default_data created duplicate prompts."
    assert new_model_count == current_model_count, "initialize_default_data created duplicate models."

    # Re-verify gs still points to the same defaults (IDs should not have changed)
    gs_after_rerun = await db_session.get(GlobalBotSettings, 1) # Fetch fresh gs
    assert gs_after_rerun.default_prompt_id == gs.default_prompt_id
    assert gs_after_rerun.default_model_id == gs.default_model_id