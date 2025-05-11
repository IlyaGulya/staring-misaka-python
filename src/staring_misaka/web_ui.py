# src/staring_misaka/web_ui.py
import asyncio
import datetime  # For date inputs
import logging
from decimal import Decimal  # For pricing
from typing import TYPE_CHECKING, Any

import gradio as gr
import pandas as pd  # For gr.DataFrame
import uvicorn  # For running Gradio with Uvicorn in the same loop
from fastapi import FastAPI  # Import FastAPI
from sqlalchemy import func, select, text  # Added func for count
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession  # Import AsyncSession
from sqlalchemy.orm import selectinload

from .db_models import GlobalBotSettings, LLMModel, Prompt, QueuedLLMCheck  # ModelPricing removed
from .db_utils import get_db_session

if TYPE_CHECKING:
    from .action_service import ActionService
    from .config import Settings
    from .llm_service import LLMService
    # from telethon import TelegramClient # If client status is needed directly

logger = logging.getLogger(__name__)

# --- Globals ---
_app_settings: "Settings | None" = None
_main_event_loop: "asyncio.AbstractEventLoop | None" = None
_llm_service_instance: "LLMService | None" = None  # For queue reprocessing
_action_service_instance: "ActionService | None" = None  # For discard action potentially approving user

# Constants
PROVIDER_CHOICES = ["Anthropic", "OpenAI"]  # Add more as supported
# CURRENCY_CHOICES = ["USD", "EUR", "GBP", "JPY", "CAD", "AUD"] # Common currencies - Removed, pricing from YAML
QUEUE_STATUS_FILTER_CHOICES = ["All", "pending_admin_action", "pending", "failed_reprocessing_attempt", "processing"]


# --- Helper Functions ---
async def _get_global_settings(session: AsyncSession) -> GlobalBotSettings | None:  # Accept session
    # This function now relies on the calling handler to provide an active session.
    gs = await session.get(GlobalBotSettings, 1)
    if not gs:
        logger.error("Gradio UI: GlobalBotSettings not found in DB!")
        # Avoid gr.Warning here if it's called during initial UI build or from non-handler context
    return gs


# --- Async Wrappers for Queue Handlers ---
async def _refresh_queue_data_async(status_filter: str):
    return await list_queued_checks_data(status_filter)


async def _handle_reprocess_queued_item_async(item_id: int, status_filter: str):
    return await handle_reprocess_queued_item(item_id, status_filter)


async def _handle_discard_queued_item_async(item_id: int, status_filter: str):
    return await handle_discard_queued_item(item_id, status_filter)


# --- LLM Model Management ---
async def list_llm_models_data() -> pd.DataFrame:
    async with get_db_session() as session:
        gs = await _get_global_settings(session)  # Pass session
        default_model_id = gs.default_model_id if gs else None

        stmt = select(LLMModel.id, LLMModel.name, LLMModel.api_identifier, LLMModel.provider, LLMModel.created_at)
        result = await session.execute(stmt)
        models = []
        for row in result.mappings().all():
            is_default = "✅" if row['id'] == default_model_id else ""
            models.append({
                "ID": row['id'],
                "Name": row['name'],
                "API Identifier": row['api_identifier'],
                "Provider": row['provider'],
                "Default": is_default,
                "Created At": row['created_at'].strftime('%Y-%m-%d %H:%M') if row['created_at'] else '',
            })
    df = pd.DataFrame(models)
    if "ID" in df.columns and not df.empty:
        df["ID"] = pd.to_numeric(df["ID"], errors='coerce').astype('Int64')
    return df


async def handle_create_llm_model(name: str, api_id: str, provider: str):
    if not all([name, api_id, provider]):
        gr.Warning("Name, API Identifier, and Provider are required.")
        return await list_llm_models_data()

    async with get_db_session() as session:
        try:
            new_model = LLMModel(name=name, api_identifier=api_id, provider=provider)
            session.add(new_model)
            await session.flush()

            gr.Info(f"LLM Model '{name}' created successfully with ID: {new_model.id}.")
        except IntegrityError:
            gr.Error(f"Error: LLM Model with name '{name}' already exists.")
            await session.rollback()
        except Exception as e:
            gr.Error(f"Failed to create LLM Model: {e}")
            logger.error(f"Gradio: Failed to create LLM model '{name}': {e}", exc_info=True)
    return await list_llm_models_data()


async def handle_update_llm_model(model_id: int, name: str, api_id: str, provider: str):
    if not model_id:
        gr.Warning("No model ID provided for update.")
        return await list_llm_models_data()
    if not all([name, api_id, provider]):
        gr.Warning("Name, API Identifier, and Provider are required for update.")
        return await list_llm_models_data()

    async with get_db_session() as session:
        try:
            model = await session.get(LLMModel, model_id)
            if not model:
                gr.Error(f"LLM Model with ID {model_id} not found.")
                return await list_llm_models_data()

            model.name = name
            model.api_identifier = api_id
            model.provider = provider

            gr.Info(f"LLM Model ID {model_id} ('{name}') updated successfully.")
        except IntegrityError:
            gr.Error(f"Error: Another LLM Model with name '{name}' might already exist.")
            await session.rollback()
        except Exception as e:
            gr.Error(f"Failed to update LLM Model ID {model_id}: {e}")
            logger.error(f"Gradio: Failed to update LLM model ID {model_id}: {e}", exc_info=True)
    return await list_llm_models_data()


async def handle_delete_llm_model(model_id: int):
    if not model_id:
        gr.Warning("No model ID provided for deletion.")
        return await list_llm_models_data()

    async with get_db_session() as session:
        gs = await _get_global_settings(session)
        if not gs:
            gr.Error("Global settings not found, cannot proceed with deletion safety checks.")
            logger.error("Gradio: handle_delete_llm_model - Global settings not found.")
            return await list_llm_models_data()

        logger.info(
            f"Gradio: Attempting to delete LLM Model ID {model_id}. Current global default model ID: {gs.default_model_id}")

        if gs.default_model_id == model_id:
            gr.Error(f"Cannot delete Model ID {model_id} as it's the current global default. Change the default first.")
            logger.warning(f"Gradio: Denied deletion of Model ID {model_id} - it's the global default.")
            return await list_llm_models_data()

        # Pricing is now in YAML, no need to check ModelPricing table before deleting LLMModel
        # pricing_exists_stmt = select(ModelPricing.id).where(ModelPricing.model_id == model_id).limit(1) # Removed
        # pricing_exists = await session.scalar(pricing_exists_stmt) # Removed
        # if pricing_exists: # Removed
        #     gr.Error( # Removed
        #         f"Cannot delete Model ID {model_id} as it has associated pricing records. Delete pricing records first.") # Removed
        #     logger.warning(f"Gradio: Denied deletion of Model ID {model_id} - has associated pricing.") # Removed
        #     return await list_llm_models_data() # Removed

        model = await session.get(LLMModel, model_id)
        if not model:
            gr.Error(f"LLM Model with ID {model_id} not found.")
            logger.warning(f"Gradio: Model ID {model_id} not found for deletion.")
            return await list_llm_models_data()

        try:
            await session.delete(model)
            gr.Info(f"LLM Model ID {model_id} ('{model.name}') deleted successfully.")
            logger.info(f"Gradio: LLM Model ID {model_id} ('{model.name}') deleted.")
        except Exception as e:
            gr.Error(f"Failed to delete LLM Model ID {model_id}: {e}")
            logger.error(f"Gradio: Failed to delete LLM model ID {model_id}: {e}", exc_info=True)
    return await list_llm_models_data()


async def handle_set_global_default_model(model_id: int):
    if not model_id:
        gr.Warning("No model ID provided to set as default.")
        return await list_llm_models_data()

    async with get_db_session() as session:
        gs = await _get_global_settings(session)
        if not gs:
            gr.Error("Global settings not found.")
            return await list_llm_models_data()

        model = await session.get(LLMModel, model_id)
        if not model:
            gr.Error(f"LLM Model with ID {model_id} not found.")
            return await list_llm_models_data()

        gs.default_model_id = model.id
        gr.Info(f"LLM Model '{model.name}' (ID: {model.id}) is now the global default.")
    return await list_llm_models_data()


def _build_llm_models_tab(ui_blocks: gr.Blocks):
    with gr.TabItem("LLM Models") as llm_models_tab:
        gr.Markdown("## LLM Model Management")

        # --- State Variables ---
        selected_model_id_state = gr.State(None)
        # For form fields, using gr.State for each to preserve values when forms are hidden/reshown
        form_model_id_state = gr.State(None)  # Stores ID for edit, or None for create
        form_model_name_state = gr.State("")
        form_model_api_id_state = gr.State("")
        form_model_provider_state = gr.State(PROVIDER_CHOICES[0])
        model_id_pending_deletion_state = gr.State(None)

        # --- Main View Components (List of models) - Always Visible ---
        with gr.Column() as list_models_view: # Default visible=True
            with gr.Row():
                create_new_model_btn = gr.Button("➕ Create New Model")
                edit_selected_model_btn = gr.Button("✏️ Edit Selected Model", interactive=False)
                refresh_models_btn = gr.Button("🔄 Refresh Models")

            model_data_df = gr.DataFrame(
                value=pd.DataFrame(columns=["ID", "Name", "API Identifier", "Provider", "Default", "Created At"]),
                label="LLM Models",
                interactive=False,  # Selection is handled by .select event
                key="llm_models_df"
            )

        # --- Form View Components (Create/Edit Model) - Toggles Visibility ---
        with gr.Column(visible=False) as model_form_view: # Initially hidden
            form_title = gr.Markdown("### Create New Model")  # Title will change for edit
            # Hidden field to store actual model_id for updates, not directly user-editable in form
            form_current_editing_id_hidden = gr.Textbox(label="Editing ID", visible=False, interactive=False)

            form_model_name_input = gr.Textbox(label="Model Name (Unique)", value="")
            form_model_api_id_input = gr.Textbox(label="API Identifier (e.g., claude-3-haiku-20240307)", value="")
            form_model_provider_dropdown = gr.Dropdown(PROVIDER_CHOICES, label="Provider", value=PROVIDER_CHOICES[0])

            with gr.Row():
                save_model_btn = gr.Button("💾 Save Model")
                cancel_form_btn = gr.Button("❌ Cancel")
            # Delete and Set Default only make sense in "Edit" mode, will be conditionally visible/active
            delete_model_from_form_btn = gr.Button("🗑️ Delete This Model", variant="stop", visible=False)
            set_default_model_from_form_btn = gr.Button("🌟 Set as Global Default", visible=False)

        # --- Confirm Deletion View Components - Toggles Visibility ---
        with gr.Column(visible=False) as confirm_delete_view: # Initially hidden
            gr.Markdown("### Confirm Deletion")
            confirm_delete_text = gr.Markdown("Are you sure you want to delete this model?")
            with gr.Row():
                confirm_delete_yes_btn = gr.Button("✔️ Yes, Delete Permanently", variant="stop")
                confirm_delete_no_btn = gr.Button("❌ No, Cancel Deletion")

        # --- Helper functions for UI state transitions ---
        def hide_forms_and_confirmations(): # Renamed from show_list_view
            return {
                # list_models_view is always visible
                model_form_view: gr.update(visible=False),
                confirm_delete_view: gr.update(visible=False),
                edit_selected_model_btn: gr.update(interactive=False),  # Reset edit button
                selected_model_id_state: None  # Clear selection
            }

        def show_create_form():
            # Clear form states for a new entry
            return {
                # list_models_view remains visible
                model_form_view: gr.update(visible=True),
                confirm_delete_view: gr.update(visible=False),
                form_title: gr.update(value="### Create New Model"),
                form_current_editing_id_hidden: None,  # No ID for create
                form_model_name_input: "",
                form_model_api_id_input: "",
                form_model_provider_dropdown: PROVIDER_CHOICES[0],
                delete_model_from_form_btn: gr.update(visible=False),
                set_default_model_from_form_btn: gr.update(visible=False),
                form_model_id_state: None,  # Store mode/id
                form_model_name_state: "",
                form_model_api_id_state: "",
                form_model_provider_state: PROVIDER_CHOICES[0]
            }

        def show_edit_form(model_id, name, api_id, provider):
            return {
                # list_models_view remains visible
                model_form_view: gr.update(visible=True),
                confirm_delete_view: gr.update(visible=False),
                form_title: gr.update(value=f"### Edit Model (ID: {model_id})"),
                form_current_editing_id_hidden: model_id,  # Store the ID being edited
                form_model_name_input: name,
                form_model_api_id_input: api_id,
                form_model_provider_dropdown: provider,
                delete_model_from_form_btn: gr.update(visible=True, interactive=True),
                set_default_model_from_form_btn: gr.update(visible=True, interactive=True),
                form_model_id_state: model_id,
                form_model_name_state: name,
                form_model_api_id_state: api_id,
                form_model_provider_state: provider
            }

        # --- Event Handlers for UI interactions ---
        def on_select_model_from_df(evt: gr.SelectData, df_data: pd.DataFrame):
            try:
                logger.info(f"Gradio on_select_model_from_df event: {evt}, df_data empty: {df_data.empty}")
                if evt.index is None or not isinstance(evt.index, list) or len(evt.index) == 0:
                    logger.info("Gradio on_select_model_from_df: Invalid event index or deselection.")
                    return None, "", "", PROVIDER_CHOICES[0], gr.update(interactive=False)

                selected_row_index = evt.index[0]
                if not (0 <= selected_row_index < len(df_data)):
                    logger.error(f"Gradio on_select_model_from_df: Row index {selected_row_index} out of bounds for df_data len {len(df_data)}.")
                    return None, "", "", PROVIDER_CHOICES[0], gr.update(interactive=False)

                selected_row = df_data.iloc[selected_row_index]
                model_id = selected_row.get("ID")
                name_val = selected_row.get("Name", "")
                api_id_val = selected_row.get("API Identifier", "")
                provider_val = selected_row.get("Provider", PROVIDER_CHOICES[0])

                if model_id is pd.NA or model_id is None:
                    logger.warning(f"Gradio on_select_model_from_df: Selected model_id is {model_id}. Treating as invalid selection for edit.")
                    return None, name_val, api_id_val, provider_val, gr.update(interactive=False)

                logger.info(
                    f"Gradio on_select_model_from_df: Selected ID={model_id}, Name='{name_val}'. Enabling edit button.")
                return model_id, name_val, api_id_val, provider_val, gr.update(interactive=True)
            except Exception as e:
                logger.error(f"Error in on_select_model_from_df: {e}", exc_info=True)
                return None, "", "", PROVIDER_CHOICES[0], gr.update(interactive=False)

        async def save_model_action(editing_id, name, api_id, provider):
            if editing_id is not None:  # Edit mode
                df_result = await handle_update_llm_model(editing_id, name, api_id, provider)
            else:  # Create mode
                df_result = await handle_create_llm_model(name, api_id, provider)

            updates_to_hide_forms = hide_forms_and_confirmations()
            updates_to_hide_forms[model_data_df] = df_result
            return updates_to_hide_forms

        async def delete_confirmed_action(model_id_to_delete):
            df_result = await handle_delete_llm_model(model_id_to_delete)
            updates_to_hide_forms = hide_forms_and_confirmations()
            updates_to_hide_forms[model_data_df] = df_result
            updates_to_hide_forms[model_id_pending_deletion_state] = None
            return updates_to_hide_forms

        def prepare_for_delete_confirmation(model_id, model_name):
            return {
                list_models_view: gr.update(visible=False),
                model_form_view: gr.update(visible=False),
                confirm_delete_view: gr.update(visible=True),
                confirm_delete_text: gr.update(
                    value=f"Are you sure you want to delete Model '{model_name}' (ID: {model_id})? This action cannot be undone."),
                model_id_pending_deletion_state: model_id
            }

        async def set_default_action_from_form(model_id):
            # This function is called when "Set as Global Default" is clicked IN THE FORM
            # It should set the default, refresh the main list, and return to the list view.
            df_result_after_default_set = await handle_set_global_default_model(model_id)
            updates_to_hide_forms = hide_forms_and_confirmations()
            updates_to_hide_forms[model_data_df] = df_result_after_default_set
            return updates_to_hide_forms

        # --- Wire up event handlers ---
        refresh_models_btn.click(list_llm_models_data, outputs=[model_data_df])

        create_new_model_btn.click(
            show_create_form,
            outputs=[list_models_view, model_form_view, confirm_delete_view, form_title,
                     form_current_editing_id_hidden, form_model_name_input, form_model_api_id_input, # list_models_view removed from here
                     form_model_provider_dropdown, delete_model_from_form_btn, set_default_model_from_form_btn,
                     form_model_id_state, form_model_name_state, form_model_api_id_state, form_model_provider_state
                     ]
        )

        model_data_df.select(on_select_model_from_df,
                             inputs=[model_data_df],
                             outputs=[selected_model_id_state, form_model_name_state, form_model_api_id_state,
                                      form_model_provider_state, edit_selected_model_btn]
                             )

        edit_selected_model_btn.click(
            show_edit_form,
            inputs=[selected_model_id_state, form_model_name_state, form_model_api_id_state, form_model_provider_state],
            outputs=[model_form_view, confirm_delete_view, form_title, # list_models_view removed
                     form_current_editing_id_hidden, form_model_name_input, form_model_api_id_input,
                     form_model_provider_dropdown, delete_model_from_form_btn, set_default_model_from_form_btn,
                     form_model_id_state, form_model_name_state, form_model_api_id_state, form_model_provider_state
                     ]
        )

        save_model_btn.click(
            save_model_action,
            inputs=[form_current_editing_id_hidden, form_model_name_input, form_model_api_id_input,
                    form_model_provider_dropdown],
            outputs=[model_form_view, confirm_delete_view, model_data_df, edit_selected_model_btn,
                     selected_model_id_state] # list_models_view removed
        )

        cancel_form_btn.click(hide_forms_and_confirmations, # Renamed function
                              outputs=[model_form_view, confirm_delete_view, edit_selected_model_btn,
                                       selected_model_id_state]) # list_models_view removed

        delete_model_from_form_btn.click(
            prepare_for_delete_confirmation,
            inputs=[form_current_editing_id_hidden, form_model_name_input],  # Pass ID and Name for confirm message
            outputs=[model_form_view, confirm_delete_view, confirm_delete_text, # list_models_view removed
                     model_id_pending_deletion_state]
        )

        set_default_model_from_form_btn.click(
            set_default_action_from_form,
            inputs=[form_current_editing_id_hidden],  # Pass the ID of the model being edited
            outputs=[model_form_view, confirm_delete_view, model_data_df, edit_selected_model_btn, # list_models_view removed
                     selected_model_id_state]
        )

        confirm_delete_yes_btn.click(
            delete_confirmed_action,
            inputs=[model_id_pending_deletion_state],
            outputs=[model_form_view, confirm_delete_view, model_data_df, edit_selected_model_btn, # list_models_view removed
                     selected_model_id_state, model_id_pending_deletion_state]
        )
        confirm_delete_no_btn.click(  # If "No" on delete confirmation, go back to edit form
            show_edit_form,
            inputs=[form_model_id_state, form_model_name_state, form_model_api_id_state, form_model_provider_state],
            # Use states that were set when edit form was shown
            outputs=[model_form_view, confirm_delete_view, form_title,
                     form_current_editing_id_hidden, form_model_name_input, form_model_api_id_input,
                     form_model_provider_dropdown, delete_model_from_form_btn, set_default_model_from_form_btn,
                     form_model_id_state, form_model_name_state, form_model_api_id_state, form_model_provider_state
                     ]
        )
    ui_blocks.load(list_llm_models_data, outputs=[model_data_df])


# --- Prompt Management ---
async def list_prompts_data() -> pd.DataFrame:
    async with get_db_session() as session:
        gs = await _get_global_settings(session)
        default_prompt_id = gs.default_prompt_id if gs else None

        stmt = select(Prompt.id, Prompt.name, Prompt.text, Prompt.is_global_default, Prompt.created_at)
        result = await session.execute(stmt)
        prompts = []
        for row in result.mappings().all():
            is_default = "✅" if row['is_global_default'] else ""
            prompts.append({
                "ID": row['id'],
                "Name": row['name'],
                "Text (Preview)": row['text'][:100] + "..." if len(row['text']) > 100 else row['text'],
                "Full Text": row['text'],
                "Default": is_default,
                "Created At": row['created_at'].strftime('%Y-%m-%d %H:%M') if row['created_at'] else '',
            })
    df = pd.DataFrame(prompts)
    if "ID" in df.columns and not df.empty:
        df["ID"] = pd.to_numeric(df["ID"], errors='coerce').astype('Int64')
    return df


async def handle_create_prompt(name: str, text_content: str):
    if not all([name, text_content]):
        gr.Warning("Name and Text are required.")
        return await list_prompts_data()
    if "{message_text}" not in text_content:
        gr.Warning("Prompt text does not contain the required '{message_text}' placeholder. This might cause issues.")

    async with get_db_session() as session:
        try:
            new_prompt = Prompt(name=name, text=text_content)
            session.add(new_prompt)
            await session.flush()

            gr.Info(f"Prompt '{name}' created successfully with ID: {new_prompt.id}.")
        except IntegrityError:
            gr.Error(f"Error: Prompt with name '{name}' already exists.")
            await session.rollback()
        except Exception as e:
            gr.Error(f"Failed to create Prompt: {e}")
            logger.error(f"Gradio: Failed to create prompt '{name}': {e}", exc_info=True)
    return await list_prompts_data()


async def handle_update_prompt(prompt_id: int, name: str, text_content: str):
    if not prompt_id:
        gr.Warning("No prompt ID provided for update.")
        return await list_prompts_data()
    if not all([name, text_content]):
        gr.Warning("Name and Text are required for update.")
        return await list_prompts_data()
    if "{message_text}" not in text_content:
        gr.Warning("Prompt text does not contain the required '{message_text}' placeholder. This might cause issues.")

    async with get_db_session() as session:
        try:
            prompt = await session.get(Prompt, prompt_id)
            if not prompt:
                gr.Error(f"Prompt with ID {prompt_id} not found.")
                return await list_prompts_data()

            prompt.name = name
            prompt.text = text_content

            gr.Info(f"Prompt ID {prompt_id} ('{name}') updated successfully.")
        except IntegrityError:
            gr.Error(f"Error: Another Prompt with name '{name}' might already exist.")
            await session.rollback()
        except Exception as e:
            gr.Error(f"Failed to update Prompt ID {prompt_id}: {e}")
            logger.error(f"Gradio: Failed to update prompt ID {prompt_id}: {e}", exc_info=True)
    return await list_prompts_data()


async def handle_delete_prompt(prompt_id: int):
    if not prompt_id:
        gr.Warning("No prompt ID provided for deletion.")
        return await list_prompts_data()

    async with get_db_session() as session:
        gs = await _get_global_settings(session)
        if not gs:
            gr.Error("Global settings not found, cannot proceed with deletion safety checks.")
            return await list_prompts_data()

        if gs.default_prompt_id == prompt_id:
            gr.Error(
                f"Cannot delete Prompt ID {prompt_id} as it's the current global default. Change the default first.")
            return await list_prompts_data()

        prompt = await session.get(Prompt, prompt_id)
        if not prompt:
            gr.Error(f"Prompt with ID {prompt_id} not found.")
            return await list_prompts_data()

        try:
            if prompt.is_global_default:  # Should not happen due to check above, but good practice
                prompt.is_global_default = False  # Ensure it's not default before deleting
            await session.delete(prompt)

            gr.Info(f"Prompt ID {prompt_id} ('{prompt.name}') deleted successfully.")
        except Exception as e:
            gr.Error(f"Failed to delete Prompt ID {prompt_id}: {e}")
            logger.error(f"Gradio: Failed to delete prompt ID {prompt_id}: {e}", exc_info=True)
    return await list_prompts_data()


async def handle_set_global_default_prompt(prompt_id: int):
    if not prompt_id:
        gr.Warning("No prompt ID provided to set as default.")
        return await list_prompts_data()

    async with get_db_session() as session:
        gs = await _get_global_settings(session)
        if not gs:
            gr.Error("Global settings not found.")
            return await list_prompts_data()

        new_default_prompt = await session.get(Prompt, prompt_id)
        if not new_default_prompt:
            gr.Error(f"Prompt with ID {prompt_id} not found.")
            return await list_prompts_data()

        if gs.default_prompt_id and gs.default_prompt_id != new_default_prompt.id:
            old_default_prompt = await session.get(Prompt, gs.default_prompt_id)
            if old_default_prompt:
                old_default_prompt.is_global_default = False

        new_default_prompt.is_global_default = True
        gs.default_prompt_id = new_default_prompt.id

        gr.Info(f"Prompt '{new_default_prompt.name}' (ID: {new_default_prompt.id}) is now the global default.")
    return await list_prompts_data()


def _build_prompts_tab(ui_blocks: gr.Blocks):
    with gr.TabItem("Prompts"):
        gr.Markdown("## Prompt Management")
        # --- Main View Components (List of prompts & action buttons) - Always Visible ---

        prompt_data_df = gr.DataFrame(
            value=pd.DataFrame(columns=["ID", "Name", "Text (Preview)", "Full Text", "Default", "Created At"]),
            label="Prompts",
            interactive=False,
            headers=["ID", "Name", "Text (Preview)", "Full Text", "Default", "Created At"],
            column_widths=["5%", "20%", "45%", "0%", "5%", "25%"],  # Hiding "Full Text" visually but keeping data
            key="prompts_df"
        )

        selected_prompt_id_state = gr.State(None)

        def on_select_prompt(evt: gr.SelectData, df_data: pd.DataFrame):
            try:
                logger.debug(f"Gradio on_select_prompt event: {evt}, df_data empty: {df_data.empty}")
                if evt.index is None or not isinstance(evt.index, list) or len(evt.index) == 0:
                    logger.debug("Gradio on_select_prompt: Invalid event index or deselection.")
                    return None, "", "", gr.update(interactive=False)

                selected_row_index = evt.index[0]
                if not (0 <= selected_row_index < len(df_data)):
                    logger.error(f"Gradio on_select_prompt: Row index {selected_row_index} out of bounds for df_data len {len(df_data)}.")
                    return None, "", "", gr.update(interactive=False)

                selected_row = df_data.iloc[selected_row_index]
                prompt_id = selected_row.get("ID")
                name_val = selected_row.get("Name", "")
                full_text_content = selected_row.get("Full Text", selected_row.get("Text (Preview)", ""))

                if prompt_id is pd.NA or prompt_id is None:
                    logger.warning(f"Gradio on_select_prompt: Selected prompt_id is {prompt_id}. Treating as invalid selection for edit.")
                    return None, name_val, full_text_content, gr.update(interactive=False)

                logger.debug(
                    f"Gradio on_select_prompt: Selected ID={prompt_id}, Name='{name_val}'. Enabling edit button.")
                return prompt_id, name_val, full_text_content, gr.update(interactive=True)
            except Exception as e:
                logger.error(f"Error in on_select_prompt: {e}", exc_info=True)
                return None, "", "", gr.update(interactive=False)

        with gr.Row():
            create_new_prompt_btn_main = gr.Button("➕ Create New Prompt")
            edit_selected_prompt_btn_main = gr.Button("✏️ Edit Selected Prompt", interactive=False)
            refresh_prompts_btn = gr.Button("🔄 Refresh Prompts")

        # --- Prompt Form (for Create/Edit) - Toggles Visibility ---
        with gr.Column(visible=False) as prompt_form_view:
            prompt_form_title = gr.Markdown("### Create New Prompt")
            prompt_form_editing_id_hidden = gr.Textbox(label="Editing Prompt ID", visible=False, interactive=False)

            prompt_form_name_input = gr.Textbox(label="Prompt Name (Unique)")
            prompt_form_text_input = gr.Textbox(label="Prompt Text (must include {message_text})", lines=5,
                                                max_lines=20)

            with gr.Row():
                save_prompt_btn = gr.Button("💾 Save Prompt")
                cancel_prompt_form_btn = gr.Button("❌ Cancel")
            delete_prompt_from_form_btn = gr.Button("🗑️ Delete This Prompt", variant="stop", visible=False)
            set_default_prompt_from_form_btn = gr.Button("🌟 Set as Global Default", visible=False)

        # --- Prompt Delete Confirmation - Toggles Visibility ---
        with gr.Column(visible=False) as confirm_prompt_delete_view:
            gr.Markdown("### Confirm Prompt Deletion")
            confirm_prompt_delete_text = gr.Markdown("Are you sure?")
            with gr.Row():
                confirm_prompt_delete_yes_btn = gr.Button("✔️ Yes, Delete", variant="stop")
                confirm_prompt_delete_no_btn = gr.Button("❌ No, Cancel")

        # --- Prompt State Variables (similar to LLM Models) ---
        prompt_form_id_state = gr.State(None)
        prompt_form_name_state = gr.State("")
        prompt_form_text_state = gr.State("")
        prompt_id_pending_deletion_state = gr.State(None)

        # --- Prompt UI State Transition Functions ---
        def hide_prompt_forms_and_confirmations(): # Renamed from show_prompt_list_view
            return {
                # Main list and its buttons are always visible
                prompt_form_view: gr.update(visible=False),
                confirm_prompt_delete_view: gr.update(visible=False),
                selected_prompt_id_state: None
            }

        def show_create_prompt_form():
            return {
                prompt_form_view: gr.update(visible=True),
                confirm_prompt_delete_view: gr.update(visible=False),
                prompt_form_title: "### Create New Prompt",
                prompt_form_editing_id_hidden: None,
                prompt_form_name_input: "",
                prompt_form_text_input: "Is this {message_text} spam? Be direct.",  # Default text
                delete_prompt_from_form_btn: gr.update(visible=False),
                set_default_prompt_from_form_btn: gr.update(visible=False),
                prompt_form_id_state: None,
                prompt_form_name_state: "",
                prompt_form_text_state: "Is this {message_text} spam? Be direct."
            }

        def show_edit_prompt_form(prompt_id, name, text_content):
            return {
                prompt_form_view: gr.update(visible=True),
                confirm_prompt_delete_view: gr.update(visible=False),
                prompt_form_title: f"### Edit Prompt (ID: {prompt_id})",
                prompt_form_editing_id_hidden: prompt_id,
                prompt_form_name_input: name,
                prompt_form_text_input: text_content,
                delete_prompt_from_form_btn: gr.update(visible=True, interactive=True),
                set_default_prompt_from_form_btn: gr.update(visible=True, interactive=True),
                prompt_form_id_state: prompt_id,
                prompt_form_name_state: name,
                prompt_form_text_state: text_content
            }

        def prepare_prompt_for_delete(prompt_id, prompt_name):
            return {
                prompt_form_view: gr.update(visible=False),
                confirm_prompt_delete_view: gr.update(visible=True),
                confirm_prompt_delete_text: f"Are you sure you want to delete Prompt '{prompt_name}' (ID: {prompt_id})? This action cannot be undone.",
                prompt_id_pending_deletion_state: prompt_id
            }

        # --- Prompt Event Handler Wirings ---
        refresh_prompts_btn.click(list_prompts_data, outputs=[prompt_data_df])

        create_new_prompt_btn_main.click(
            show_create_prompt_form,
            outputs=[prompt_form_view, confirm_prompt_delete_view, prompt_form_title, prompt_form_editing_id_hidden,
                     prompt_form_name_input, prompt_form_text_input,
                     delete_prompt_from_form_btn, set_default_prompt_from_form_btn,
                     prompt_form_id_state, prompt_form_name_state, prompt_form_text_state]
        )

        prompt_data_df.select(
            on_select_prompt,
            inputs=[prompt_data_df],
            outputs=[selected_prompt_id_state, prompt_form_name_state, prompt_form_text_state,
                     edit_selected_prompt_btn_main]
        )

        edit_selected_prompt_btn_main.click(
            show_edit_prompt_form,
            inputs=[selected_prompt_id_state, prompt_form_name_state, prompt_form_text_state],
            outputs=[prompt_form_view, confirm_prompt_delete_view, prompt_form_title, prompt_form_editing_id_hidden,
                     prompt_form_name_input, prompt_form_text_input,
                     delete_prompt_from_form_btn, set_default_prompt_from_form_btn,
                     prompt_form_id_state, prompt_form_name_state, prompt_form_text_state]
        )

        async def save_prompt_action_wrapper(editing_id, name, text_content):
            if editing_id is not None:  # Edit
                df_result = await handle_update_prompt(editing_id, name, text_content)
            else:  # Create
                df_result = await handle_create_prompt(name, text_content)

            updates = hide_prompt_forms_and_confirmations()
            updates[prompt_data_df] = df_result
            return updates

        save_prompt_btn.click(
            save_prompt_action_wrapper,
            inputs=[prompt_form_editing_id_hidden, prompt_form_name_input, prompt_form_text_input],
            outputs=[prompt_form_view, confirm_prompt_delete_view, selected_prompt_id_state, prompt_data_df, edit_selected_prompt_btn_main]
        )

        cancel_prompt_form_btn.click(
            hide_prompt_forms_and_confirmations,
            outputs=[prompt_form_view, confirm_prompt_delete_view, selected_prompt_id_state, edit_selected_prompt_btn_main]

        )

        delete_prompt_from_form_btn.click(
            prepare_prompt_for_delete,
            inputs=[prompt_form_editing_id_hidden, prompt_form_name_input],
            outputs=[prompt_form_view, confirm_prompt_delete_view, confirm_prompt_delete_text,
                     prompt_id_pending_deletion_state]
        )

        async def set_default_prompt_action_wrapper(prompt_id):
            df_result = await handle_set_global_default_prompt(prompt_id)
            updates = hide_prompt_forms_and_confirmations()
            updates[prompt_data_df] = df_result
            return updates

        set_default_prompt_from_form_btn.click(
            set_default_prompt_action_wrapper,
            inputs=[prompt_form_editing_id_hidden],
            outputs=[prompt_form_view, confirm_prompt_delete_view, selected_prompt_id_state, prompt_data_df, edit_selected_prompt_btn_main]
        )

        async def delete_prompt_confirmed_action_wrapper(prompt_id_to_delete):
            df_result = await handle_delete_prompt(prompt_id_to_delete)
            updates = hide_prompt_forms_and_confirmations()
            updates[prompt_data_df] = df_result
            updates[prompt_id_pending_deletion_state] = None
            return updates

        confirm_prompt_delete_yes_btn.click(
            delete_prompt_confirmed_action_wrapper,
            inputs=[prompt_id_pending_deletion_state],
            outputs=[prompt_form_view, confirm_prompt_delete_view, selected_prompt_id_state, prompt_data_df, edit_selected_prompt_btn_main, prompt_id_pending_deletion_state]
        )

        confirm_prompt_delete_no_btn.click(  # Go back to edit form
            show_edit_prompt_form,
            inputs=[prompt_form_id_state, prompt_form_name_state, prompt_form_text_state],
        )

    ui_blocks.load(list_prompts_data, outputs=[prompt_data_df])

# --- Queue Management ---
async def list_queued_checks_data(status_filter: str) -> pd.DataFrame:
    async with get_db_session() as session:
        stmt = select(QueuedLLMCheck)
        if status_filter != "All":
            stmt = stmt.where(QueuedLLMCheck.status == status_filter)

        stmt = stmt.order_by(QueuedLLMCheck.status.desc(), QueuedLLMCheck.queued_at.asc()).limit(100)
        result = await session.execute(stmt)
        items = []
        for item in result.scalars().all():
            ctx = item.message_context_json if isinstance(item.message_context_json, dict) else {}
            items.append({
                "ID": item.id,
                "Status": item.status,
                "Chat ID": ctx.get('chat_id', 'N/A'),
                "User ID": ctx.get('user_id', 'N/A'),
                "Message ID": ctx.get('message_id', 'N/A'),
                "Retries": item.retry_count,
                "Queued At": item.queued_at.strftime('%Y-%m-%d %H:%M') if item.queued_at else '',
                "Last Attempt": item.last_attempted_at.strftime('%Y-%m-%d %H:%M') if item.last_attempted_at else '',
                "Reason (Preview)": item.reason_for_queueing[:150] + "..." if len(
                    item.reason_for_queueing) > 150 else item.reason_for_queueing,
                "Full Reason": item.reason_for_queueing,
                "Message (Preview)": ctx.get('message_text', '')[:100] + "..." if ctx.get('message_text') else '',
                "Full Message": ctx.get('message_text', ''),
            })
    df = pd.DataFrame(items)
    if "ID" in df.columns and not df.empty:
        df["ID"] = pd.to_numeric(df["ID"], errors='coerce').astype('Int64')
    return df


async def handle_reprocess_queued_item(item_id: int, current_status_filter: str):
    if not item_id:
        gr.Warning("No queued item ID provided for reprocessing.")
        return await list_queued_checks_data(current_status_filter)
    if not _llm_service_instance or not _action_service_instance:
        gr.Error("LLMService or ActionService not available for reprocessing.")
        logger.error("Gradio: LLMService or ActionService not initialized for queue reprocessing.")
        return await list_queued_checks_data(current_status_filter)

    async with get_db_session() as session:
        queued_item_for_handler_scope = await session.get(QueuedLLMCheck, item_id)
        if not queued_item_for_handler_scope:
            gr.Error(f"Queued item with ID {item_id} not found (or was just processed).")
            return await list_queued_checks_data(current_status_filter)

        if queued_item_for_handler_scope.status not in ["pending", "failed_reprocessing_attempt",
                                                        "pending_admin_action"]:
            gr.Warning(
                f"Item {item_id} is currently '{queued_item_for_handler_scope.status}' and cannot be manually reprocessed now.")
            return await list_queued_checks_data(current_status_filter)

        gr.Info(f"Attempting to manually reprocess queued item ID: {item_id}...")

        await _llm_service_instance.reprocess_queued_item(session, item_id, _action_service_instance)

        final_item_state = await session.get(QueuedLLMCheck, item_id)

        if not final_item_state:
            gr.Info(f"Item {item_id} successfully resolved (processed, sent for admin approval, or user approved).")
        else:
            gr.Warning(
                f"Item {item_id} reprocessing did not lead to immediate resolution. Current status: '{final_item_state.status}'. Reason: {final_item_state.reason_for_queueing}")

    return await list_queued_checks_data(current_status_filter)


async def handle_discard_queued_item(item_id: int, current_status_filter: str):
    if not item_id:
        gr.Warning("No queued item ID provided for discarding.")
        return await list_queued_checks_data(current_status_filter)
    if not _action_service_instance:
        gr.Error("ActionService not available for discarding item (user approval).")
        logger.error("Gradio: ActionService not initialized for queue discarding.")
        return await list_queued_checks_data(current_status_filter)

    async with get_db_session() as session:
        queued_item = await session.get(QueuedLLMCheck, item_id)
        if not queued_item:
            gr.Error(f"Queued item with ID {item_id} not found.")
            return await list_queued_checks_data(current_status_filter)

        ctx_data = queued_item.message_context_json if isinstance(queued_item.message_context_json, dict) else {}
        user_id_to_approve = ctx_data.get('user_id')
        chat_id_to_approve = ctx_data.get('chat_id')

        reply_message = f"Queued item {item_id} discarded."
        if user_id_to_approve and chat_id_to_approve:
            await _action_service_instance.process_user_approval(session, user_id_to_approve, chat_id_to_approve)
            reply_message += f" User {user_id_to_approve} in chat {chat_id_to_approve} marked as approved."

        await session.delete(queued_item)
        gr.Info(reply_message)
    return await list_queued_checks_data(current_status_filter)


def _build_queue_management_tab(ui_blocks: gr.Blocks):
    with gr.TabItem("Queue Management", id="queue_tab"):
        gr.Markdown("## LLM Check Queue Management")
        gr.Markdown(
            "View and manage items that failed initial LLM processing and are queued for retry or admin action.")

        queue_status_filter_dd = gr.Dropdown(
            QUEUE_STATUS_FILTER_CHOICES,
            value="pending_admin_action",
            label="Filter by Status"
        )

        queue_data_df = gr.DataFrame(
            value=pd.DataFrame(
                columns=["ID", "Status", "Chat ID", "User ID", "Msg ID", "Retries", "Queued", "Last Attempt",
                         "Reason (Preview)", "Full Reason", "Msg (Preview)", "Full Message"]),
            label="Queued LLM Checks (Max 100 shown)",
            interactive=False,
            headers=["ID", "Status", "Chat ID", "User ID", "Msg ID", "Retries", "Queued", "Last Attempt",
                     "Reason (Preview)", "Full Reason", "Msg (Preview)", "Full Message"],
            column_widths=["3%", "10%", "8%", "8%", "8%", "5%", "10%", "10%", "20%", "0%", "18%", "0%"],
            key="queue_df"
        )

        selected_queue_item_id_state = gr.State(None)
        selected_queue_item_full_reason_state = gr.State("")
        selected_queue_item_full_message_state = gr.State("")

        def on_select_queue_item(evt: gr.SelectData, df_data: pd.DataFrame):
            if evt.index is None or not isinstance(evt.index, list) or len(evt.index) == 0:
                return None, "", "", gr.update(interactive=False), gr.update(interactive=False), gr.Textbox(
                    visible=False), gr.Textbox(visible=False), ""

            selected_row_index = evt.index[0]
            if not (0 <= selected_row_index < len(df_data)):
                return None, "", "", gr.update(interactive=False), gr.update(interactive=False), gr.Textbox(
                    visible=False), gr.Textbox(visible=False), ""

            selected_row = df_data.iloc[selected_row_index]
            item_id = selected_row["ID"]
            full_reason = selected_row.get("Full Reason", "")
            full_message = selected_row.get("Full Message", "")

            can_reprocess = selected_row["Status"] in ["pending", "failed_reprocessing_attempt", "pending_admin_action"]

            return item_id, full_reason, full_message, gr.update(interactive=can_reprocess), gr.update(
                interactive=True), gr.update(visible=True, value=full_reason), gr.update(visible=True,
                                                                                         value=full_message), str(
                item_id)

        with gr.Row():
            refresh_queue_btn = gr.Button("🔄 Refresh Queue Data")

        with gr.Accordion("Selected Item Details & Actions", open=False) as details_accordion:
            queue_item_id_display = gr.Textbox(label="Selected Item ID", interactive=False)
            gr.Label("Full Reason for Queuing:")
            selected_queue_item_full_reason_display = gr.Textbox(interactive=False, lines=3, max_lines=10,
                                                                 show_label=False)
            gr.Label("Full Message Text:")
            selected_queue_item_full_message_display = gr.Textbox(interactive=False, lines=3, max_lines=10,
                                                                  show_label=False)

            with gr.Row():
                reprocess_item_btn = gr.Button("♻️ Reprocess Selected Item", interactive=False)
                discard_item_btn = gr.Button("🗑️ Discard Selected Item (and Approve User)", variant="stop",
                                             interactive=False)

        refresh_queue_btn.click(_refresh_queue_data_async, inputs=[queue_status_filter_dd], outputs=[queue_data_df])
        queue_status_filter_dd.change(_refresh_queue_data_async, inputs=[queue_status_filter_dd],
                                      outputs=[queue_data_df])

        queue_data_df.select(
            on_select_queue_item,
            inputs=[queue_data_df],
            outputs=[
                selected_queue_item_id_state,
                selected_queue_item_full_reason_state,
                selected_queue_item_full_message_state,
                reprocess_item_btn,
                discard_item_btn,
                selected_queue_item_full_reason_display,
                selected_queue_item_full_message_display,
                queue_item_id_display,
            ]
        ).then(lambda: gr.Accordion(open=True), outputs=[details_accordion])  # Keep accordion open after selection

        reprocess_item_btn.click(
            _handle_reprocess_queued_item_async,
            inputs=[selected_queue_item_id_state, queue_status_filter_dd],
            outputs=[queue_data_df]
        ).then(lambda: gr.Accordion(open=False), outputs=[details_accordion])  # Close accordion after action

        discard_item_btn.click(
            _handle_discard_queued_item_async,
            inputs=[selected_queue_item_id_state, queue_status_filter_dd],
            outputs=[queue_data_df]
        ).then(lambda: gr.Accordion(open=False), outputs=[details_accordion])  # Close accordion after action

    ui_blocks.load(_refresh_queue_data_async, inputs=[queue_status_filter_dd], outputs=[queue_data_df])


# --- Dashboard ---
async def get_bot_status() -> str:
    if not _app_settings:
        return "Bot Status: Error - App settings not initialized for UI."
    db_status = "DB Status: Unknown"
    try:
        async with get_db_session() as session:
            await session.execute(text("SELECT 1"))
        db_status = "DB Connected"
    except Exception as e:
        db_status = f"DB Connection Error: {type(e).__name__}"
        logger.warning(f"Gradio UI: DB connection check failed: {e}")

    queue_admin_count = 0
    try:
        async with get_db_session() as session:
            stmt = select(func.count(QueuedLLMCheck.id)).where(QueuedLLMCheck.status == "pending_admin_action")
            count_result = await session.execute(stmt)
            queue_admin_count = count_result.scalar_one_or_none() or 0
    except Exception as e:
        logger.warning(f"Gradio UI: Failed to count admin-action queue items: {e}")

    pricing_config_status = "Pricing Config: Loaded"
    if not _app_settings.loaded_pricing_config or not _app_settings.loaded_pricing_config.models:
        pricing_config_status = f"Pricing Config: Not loaded or empty (Path: {_app_settings.pricing_config_file_path})"

    return f"Bot Status:\n- {db_status}\n- Items needing admin action in queue: {queue_admin_count}\n- {pricing_config_status}"


# --- Main UI Construction ---
def create_main_ui_layout():
    with gr.Blocks(title="Staring Misaka Admin", theme=gr.themes.Soft()) as ui:
        gr.Markdown("# Staring Misaka Admin UI")

        with gr.Tabs():
            with gr.TabItem("Dashboard", id="dashboard_tab"):
                gr.Markdown("## Dashboard")
                status_output = gr.Textbox(label="Bot Status", interactive=False, lines=5,
                                           # Increased lines for pricing status
                                           elem_id="dashboard_status_output")
                refresh_status_btn = gr.Button("Refresh Status")
                refresh_status_btn.click(get_bot_status, outputs=status_output)

            _build_llm_models_tab(ui)
            _build_prompts_tab(ui)
            # _build_model_pricing_tab(ui) # Removed
            _build_queue_management_tab(ui)

            with gr.TabItem("Monitored Groups", id="groups_tab"):  # Kept as placeholder
                gr.Markdown("## Monitored Group Management")
                gr.Markdown("_(Functionality to be implemented)_")

            with gr.TabItem("LLM Logs", id="logs_tab"):  # Kept as placeholder
                gr.Markdown("## LLM Logs Viewer")
                gr.Markdown("_(Functionality to be implemented)_")

            with gr.TabItem("Pricing Configuration (Read-Only)", id="pricing_view_tab"):
                gr.Markdown("## View Pricing Configuration")
                gr.Markdown(
                    "Model pricing is managed via the `pricing_config.yaml` file. Restart the bot to apply changes from the YAML.")
                pricing_yaml_display = gr.Code(label="Current pricing_config.yaml content (if loaded)", language="yaml",
                                               interactive=False)

                async def load_pricing_yaml_content():
                    if _app_settings and _app_settings.pricing_config_file_path:
                        try:
                            with open(_app_settings.pricing_config_file_path, 'r') as f:
                                return f.read()
                        except FileNotFoundError:
                            return f"File not found: {_app_settings.pricing_config_file_path}"
                        except Exception as e:
                            return f"Error reading file: {e}"
                    return "Pricing config path not set or file unreadable."

                ui.load(load_pricing_yaml_content, outputs=[pricing_yaml_display])  # Load on tab/UI load
                refresh_pricing_view_btn = gr.Button("🔄 Refresh View from YAML File")
                refresh_pricing_view_btn.click(load_pricing_yaml_content, outputs=[pricing_yaml_display])

        ui.load(get_bot_status, outputs=status_output)  # Initial load for dashboard status
    return ui


# --- Gradio Launch Logic ---
async def launch_gradio_ui(
        settings: "Settings",
        llm_service: "LLMService",
        action_service: "ActionService",
) -> asyncio.Task | None:
    global _app_settings, _main_event_loop, _llm_service_instance, _action_service_instance
    _app_settings = settings
    _main_event_loop = asyncio.get_running_loop()
    _llm_service_instance = llm_service
    _action_service_instance = action_service

    logger.info("Initializing Gradio Web UI components...")
    admin_ui_blocks: gr.Blocks = create_main_ui_layout()

    # Create a new FastAPI app instance
    fastapi_app = FastAPI(title="Staring Misaka Gradio UI")

    # Mount the Gradio Blocks instance to the FastAPI app
    auth_creds = None
    if settings.gradio_username and settings.gradio_password and settings.gradio_password.get_secret_value():
        auth_creds = (settings.gradio_username, settings.gradio_password.get_secret_value())
        logger.info("Gradio UI will be configured with authentication.")
    else:
        logger.warning(
            "Gradio UI will be configured WITHOUT authentication. "
            "Set GRADIO_USERNAME and GRADIO_PASSWORD environment variables to enable."
        )

    try:
        # gr.mount_gradio_app handles setting up the Blocks' auth, auth_message, etc.
        # and correctly configures the FastAPI app for Gradio.
        # The path="/", means Gradio will be served at the root of this FastAPI instance.
        fastapi_app = gr.mount_gradio_app(
            app=fastapi_app,
            blocks=admin_ui_blocks,
            path="/",  # Gradio UI will be at the root of this FastAPI app
            auth=auth_creds,
            auth_message="Enter credentials to access the Staring Misaka Admin UI.",  # Optional custom message
            app_kwargs={  # Passed to FastAPI constructor if gradio creates a new one, here it modifies `fastapi_app`
                "title": "Staring Misaka Admin UI (Mounted)",  # This title is for the FastAPI app itself
            }
        )
    except Exception as e:
        logger.error(f"Failed to mount Gradio app to FastAPI: {e}", exc_info=True)
        return None

    # Configure Uvicorn server to run the combined FastAPI app
    config = uvicorn.Config(
        app=fastapi_app,
        host="0.0.0.0",
        port=settings.gradio_port,
        log_level=settings.log_level.lower(),
    )
    server = uvicorn.Server(config)

    async def run_uvicorn_server():
        try:
            logger.info(f"Starting Uvicorn server for Gradio UI on http://0.0.0.0:{settings.gradio_port}")
            await server.serve()
        except asyncio.CancelledError:
            logger.info("Uvicorn server task for Gradio UI cancelled. Attempting shutdown...")
        except Exception as e_uvicorn:
            logger.error(f"Uvicorn server for Gradio UI error: {e_uvicorn}", exc_info=True)
        finally:
            if server.started and hasattr(server, 'should_exit') and not server.should_exit:
                logger.info("Uvicorn server for Gradio UI initiating shutdown sequence.")
                await server.shutdown()
            logger.info("Uvicorn server for Gradio UI has stopped.")

    gradio_task = asyncio.create_task(run_uvicorn_server())
    gradio_task.set_name("GradioUIServerTask")
    logger.info(f"Gradio UI Uvicorn task created. Access at http://<your_ip>:{settings.gradio_port}")
    return gradio_task