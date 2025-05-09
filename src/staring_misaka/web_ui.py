# src/staring_misaka/web_ui.py
import asyncio
import datetime  # For date inputs
import logging
import threading  # For running Gradio in a separate thread
from typing import TYPE_CHECKING, Any, cast
from decimal import Decimal  # For pricing

import pandas as pd  # For gr.DataFrame
import gradio as gr
from sqlalchemy import delete, select, text, update, func  # Added func for count
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from .db_models import LLMModel, Prompt, GlobalBotSettings, ModelPricing, QueuedLLMCheck  # Added QueuedLLMCheck
from .db_utils import get_db_session

if TYPE_CHECKING:
    from .config import Settings
    from .llm_service import LLMService
    from .action_service import ActionService
    # from telethon import TelegramClient # If client status is needed directly

logger = logging.getLogger(__name__)

# --- Globals ---
_app_settings: "Settings | None" = None
_main_event_loop: "asyncio.AbstractEventLoop | None" = None
_llm_service_instance: "LLMService | None" = None  # For queue reprocessing
_action_service_instance: "ActionService | None" = None  # For discard action potentially approving user

# Constants
PROVIDER_CHOICES = ["Anthropic", "OpenAI"]  # Add more as supported
CURRENCY_CHOICES = ["USD", "EUR", "GBP", "JPY", "CAD", "AUD"]  # Common currencies
QUEUE_STATUS_FILTER_CHOICES = ["All", "pending_admin_action", "pending", "failed_reprocessing_attempt", "processing"]


# --- Helper Functions ---
async def _get_global_settings() -> GlobalBotSettings | None:
    async with get_db_session() as session:
        gs = await session.get(GlobalBotSettings, 1)
        if not gs:
            logger.error("Gradio UI: GlobalBotSettings not found in DB!")
            # Avoid gr.Warning here if it's called during initial UI build before loop is fully ready
            # It will be handled by functions that use gs and can show warnings.
        return gs


def df_to_list_of_dicts(df: pd.DataFrame | None) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    return df.to_dict(orient='records')


async def get_llm_model_choices() -> list[tuple[str, int]]:
    """Returns a list of (model_name, model_id) for dropdowns."""
    async with get_db_session() as session:
        stmt = select(LLMModel.id, LLMModel.name).order_by(LLMModel.name)
        result = await session.execute(stmt)
        return [(f"{row.name} (ID: {row.id})", row.id) for row in result.mappings().all()]


# --- LLM Model Management ---
async def list_llm_models_data() -> pd.DataFrame:
    async with get_db_session() as session:
        gs = await _get_global_settings()
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
    return pd.DataFrame(models)


async def handle_create_llm_model(name: str, api_id: str, provider: str):
    if not all([name, api_id, provider]):
        gr.Warning("Name, API Identifier, and Provider are required.")
        return await list_llm_models_data()  # Return current state

    async with get_db_session() as session:
        try:
            new_model = LLMModel(name=name, api_identifier=api_id, provider=provider)
            session.add(new_model)
            await session.flush()  # To get ID and check constraints
            await session.commit()  # Commit after flush since we might be done
            gr.Info(f"LLM Model '{name}' created successfully with ID: {new_model.id}.")
        except IntegrityError:
            await session.rollback()
            gr.Error(f"Error: LLM Model with name '{name}' already exists.")
        except Exception as e:
            await session.rollback()
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
            await session.commit()
            gr.Info(f"LLM Model ID {model_id} ('{name}') updated successfully.")
        except IntegrityError:
            await session.rollback()
            gr.Error(f"Error: Another LLM Model with name '{name}' might already exist.")
        except Exception as e:
            await session.rollback()
            gr.Error(f"Failed to update LLM Model ID {model_id}: {e}")
            logger.error(f"Gradio: Failed to update LLM model ID {model_id}: {e}", exc_info=True)
    return await list_llm_models_data()


async def handle_delete_llm_model(model_id: int):
    if not model_id:
        gr.Warning("No model ID provided for deletion.")
        return await list_llm_models_data()

    async with get_db_session() as session:
        gs = await _get_global_settings()
        if not gs:
            gr.Error("Global settings not found, cannot proceed with deletion safety checks.")
            return await list_llm_models_data()

        if gs.default_model_id == model_id:
            gr.Error(f"Cannot delete Model ID {model_id} as it's the current global default. Change the default first.")
            return await list_llm_models_data()

        # Check if model is used in any pricing records
        pricing_exists_stmt = select(ModelPricing.id).where(ModelPricing.model_id == model_id).limit(1)
        pricing_exists = await session.scalar(pricing_exists_stmt)
        if pricing_exists:
            gr.Error(
                f"Cannot delete Model ID {model_id} as it has associated pricing records. Delete pricing records first.")
            return await list_llm_models_data()

        # Add checks for MonitoredGroup usage later here

        model = await session.get(LLMModel, model_id)
        if not model:
            gr.Error(f"LLM Model with ID {model_id} not found.")
            return await list_llm_models_data()

        try:
            await session.delete(model)
            await session.commit()
            gr.Info(f"LLM Model ID {model_id} ('{model.name}') deleted successfully.")
        except Exception as e:
            await session.rollback()
            gr.Error(f"Failed to delete LLM Model ID {model_id}: {e}")
            logger.error(f"Gradio: Failed to delete LLM model ID {model_id}: {e}", exc_info=True)
    return await list_llm_models_data()


async def handle_set_global_default_model(model_id: int):
    if not model_id:
        gr.Warning("No model ID provided to set as default.")
        return await list_llm_models_data()

    async with get_db_session() as session:
        gs = await _get_global_settings()
        if not gs:
            gr.Error("Global settings not found.")
            return await list_llm_models_data()

        model = await session.get(LLMModel, model_id)
        if not model:
            gr.Error(f"LLM Model with ID {model_id} not found.")
            return await list_llm_models_data()

        gs.default_model_id = model.id
        await session.commit()
        gr.Info(f"LLM Model '{model.name}' (ID: {model.id}) is now the global default.")
    return await list_llm_models_data()


def _build_llm_models_tab():
    with gr.TabItem("LLM Models"):
        gr.Markdown("## LLM Model Management")

        model_data_df = gr.DataFrame(value=list_llm_models_data, label="LLM Models", interactive=False,
                                     key="llm_models_df")

        selected_model_id_state = gr.State(None)  # To store ID of selected model for edit/delete

        def on_select_model(evt: gr.SelectData, df_data: pd.DataFrame):
            if evt.index is None or not isinstance(evt.index, tuple) or len(evt.index) == 0:
                return None, "", "", PROVIDER_CHOICES[0], gr.Button(interactive=False), gr.Button(
                    interactive=False), gr.Button(interactive=False), ""

            selected_row_index = evt.index[0]
            if selected_row_index < 0 or selected_row_index >= len(df_data):
                return None, "", "", PROVIDER_CHOICES[0], gr.Button(interactive=False), gr.Button(
                    interactive=False), gr.Button(interactive=False), ""

            selected_row = df_data.iloc[selected_row_index]
            model_id = selected_row["ID"]
            return model_id, selected_row["Name"], selected_row["API Identifier"], selected_row["Provider"], gr.Button(
                interactive=True), gr.Button(interactive=True), gr.Button(interactive=True), str(model_id)

        with gr.Row():
            refresh_models_btn = gr.Button("🔄 Refresh Models")

        with gr.Accordion("Create New LLM Model", open=False):
            with gr.Row():
                new_model_name = gr.Textbox(label="Model Name (Unique)")
                new_model_api_id = gr.Textbox(label="API Identifier (e.g., claude-3-haiku-20240307)")
            new_model_provider = gr.Dropdown(PROVIDER_CHOICES, label="Provider", value=PROVIDER_CHOICES[0])
            create_model_btn = gr.Button("Create Model")

        with gr.Accordion("Edit/Delete Selected LLM Model", open=False):
            edit_model_id_display = gr.Textbox(label="Selected Model ID", interactive=False)  # For display
            with gr.Row():
                edit_model_name = gr.Textbox(label="Model Name (Unique)")
                edit_model_api_id = gr.Textbox(label="API Identifier")
            edit_model_provider = gr.Dropdown(PROVIDER_CHOICES, label="Provider")

            with gr.Row():
                update_model_btn = gr.Button("Update Selected Model", interactive=False)
                delete_model_btn = gr.Button("Delete Selected Model", variant="stop", interactive=False)
                set_default_model_btn = gr.Button("Set as Global Default", interactive=False)

        # Event Handlers for Model Tab
        refresh_models_btn.click(list_llm_models_data, outputs=[model_data_df])
        create_model_btn.click(
            handle_create_llm_model,
            inputs=[new_model_name, new_model_api_id, new_model_provider],
            outputs=[model_data_df]
        ).then(lambda: (None, "", "", PROVIDER_CHOICES[0]),
               outputs=[selected_model_id_state, new_model_name, new_model_api_id, new_model_provider])  # Clear fields

        model_data_df.select(
            on_select_model,
            inputs=[model_data_df],
            outputs=[selected_model_id_state, edit_model_name, edit_model_api_id, edit_model_provider, update_model_btn,
                     delete_model_btn, set_default_model_btn, edit_model_id_display],
            # Show selected ID in display box as well
        )

        update_model_btn.click(
            handle_update_llm_model,
            inputs=[selected_model_id_state, edit_model_name, edit_model_api_id, edit_model_provider],
            outputs=[model_data_df]
        )
        delete_model_btn.click(
            handle_delete_llm_model,
            inputs=[selected_model_id_state],
            outputs=[model_data_df]
        )
        set_default_model_btn.click(
            handle_set_global_default_model,
            inputs=[selected_model_id_state],
            outputs=[model_data_df]
        )


# --- Prompt Management ---
async def list_prompts_data() -> pd.DataFrame:
    async with get_db_session() as session:
        gs = await _get_global_settings()
        default_prompt_id = gs.default_prompt_id if gs else None

        stmt = select(Prompt.id, Prompt.name, Prompt.text, Prompt.is_global_default, Prompt.created_at)
        result = await session.execute(stmt)
        prompts = []
        for row in result.mappings().all():
            is_default = "✅" if row['is_global_default'] else ""  # or row['id'] == default_prompt_id
            prompts.append({
                "ID": row['id'],
                "Name": row['name'],
                "Text (Preview)": row['text'][:100] + "..." if len(row['text']) > 100 else row['text'],  # Preview
                "Full Text": row['text'],  # For editing
                "Default": is_default,
                "Created At": row['created_at'].strftime('%Y-%m-%d %H:%M') if row['created_at'] else '',
            })
    return pd.DataFrame(prompts)


async def handle_create_prompt(name: str, text_content: str):
    if not all([name, text_content]):
        gr.Warning("Name and Text are required.")
        return await list_prompts_data()
    if "{message_text}" not in text_content:
        gr.Warning("Prompt text does not contain the required '{message_text}' placeholder. This might cause issues.")
        # Proceed with creation anyway

    async with get_db_session() as session:
        try:
            new_prompt = Prompt(name=name, text=text_content)
            session.add(new_prompt)
            await session.flush()
            await session.commit()
            gr.Info(f"Prompt '{name}' created successfully with ID: {new_prompt.id}.")
        except IntegrityError:
            await session.rollback()
            gr.Error(f"Error: Prompt with name '{name}' already exists.")
        except Exception as e:
            await session.rollback()
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
            await session.commit()
            gr.Info(f"Prompt ID {prompt_id} ('{name}') updated successfully.")
        except IntegrityError:
            await session.rollback()
            gr.Error(f"Error: Another Prompt with name '{name}' might already exist.")
        except Exception as e:
            await session.rollback()
            gr.Error(f"Failed to update Prompt ID {prompt_id}: {e}")
            logger.error(f"Gradio: Failed to update prompt ID {prompt_id}: {e}", exc_info=True)
    return await list_prompts_data()


async def handle_delete_prompt(prompt_id: int):
    if not prompt_id:
        gr.Warning("No prompt ID provided for deletion.")
        return await list_prompts_data()

    async with get_db_session() as session:
        gs = await _get_global_settings()
        if not gs:
            gr.Error("Global settings not found, cannot proceed with deletion safety checks.")
            return await list_prompts_data()

        if gs.default_prompt_id == prompt_id:
            gr.Error(
                f"Cannot delete Prompt ID {prompt_id} as it's the current global default. Change the default first.")
            return await list_prompts_data()

        # Add checks for MonitoredGroup usage later here

        prompt = await session.get(Prompt, prompt_id)
        if not prompt:
            gr.Error(f"Prompt with ID {prompt_id} not found.")
            return await list_prompts_data()

        try:
            if prompt.is_global_default:  # Should be caught by gs.default_prompt_id check, but defensive
                prompt.is_global_default = False
            await session.delete(prompt)
            await session.commit()
            gr.Info(f"Prompt ID {prompt_id} ('{prompt.name}') deleted successfully.")
        except Exception as e:
            await session.rollback()
            gr.Error(f"Failed to delete Prompt ID {prompt_id}: {e}")
            logger.error(f"Gradio: Failed to delete prompt ID {prompt_id}: {e}", exc_info=True)
    return await list_prompts_data()


async def handle_set_global_default_prompt(prompt_id: int):
    if not prompt_id:
        gr.Warning("No prompt ID provided to set as default.")
        return await list_prompts_data()

    async with get_db_session() as session:
        gs = await _get_global_settings()
        if not gs:
            gr.Error("Global settings not found.")
            return await list_prompts_data()

        new_default_prompt = await session.get(Prompt, prompt_id)
        if not new_default_prompt:
            gr.Error(f"Prompt with ID {prompt_id} not found.")
            return await list_prompts_data()

        if gs.default_prompt_id and gs.default_prompt_id != new_default_prompt.id:
            current_default_prompt_obj = await session.get(Prompt, gs.default_prompt_id)
            if current_default_prompt_obj:
                current_default_prompt_obj.is_global_default = False

        new_default_prompt.is_global_default = True
        gs.default_prompt_id = new_default_prompt.id
        await session.commit()
        gr.Info(f"Prompt '{new_default_prompt.name}' (ID: {new_default_prompt.id}) is now the global default.")
    return await list_prompts_data()


def _build_prompts_tab():
    with gr.TabItem("Prompts"):
        gr.Markdown("## Prompt Management")

        prompt_data_df = gr.DataFrame(
            value=list_prompts_data,
            label="Prompts",
            interactive=False,
            headers=["ID", "Name", "Text (Preview)", "Full Text", "Default", "Created At"],
            column_widths=["5%", "20%", "45%", "0%", "5%", "25%"],  # Hide Full Text by making width 0%
            key="prompts_df"
        )

        selected_prompt_id_state = gr.State(None)

        def on_select_prompt(evt: gr.SelectData, df_data: pd.DataFrame):
            if evt.index is None or not isinstance(evt.index, tuple) or len(evt.index) == 0:
                return None, "", "", gr.Button(interactive=False), gr.Button(interactive=False), gr.Button(
                    interactive=False), ""

            selected_row_index = evt.index[0]
            if selected_row_index < 0 or selected_row_index >= len(df_data):
                return None, "", "", gr.Button(interactive=False), gr.Button(interactive=False), gr.Button(
                    interactive=False), ""

            selected_row = df_data.iloc[selected_row_index]
            prompt_id = selected_row["ID"]
            full_text_content = selected_row.get("Full Text", selected_row["Text (Preview)"])
            return prompt_id, selected_row["Name"], full_text_content, gr.Button(interactive=True), gr.Button(
                interactive=True), gr.Button(interactive=True), str(prompt_id)

        with gr.Row():
            refresh_prompts_btn = gr.Button("🔄 Refresh Prompts")

        with gr.Accordion("Create New Prompt", open=False):
            new_prompt_name = gr.Textbox(label="Prompt Name (Unique)")
            new_prompt_text = gr.Textbox(label="Prompt Text (must include {message_text})", lines=5, max_lines=20)
            create_prompt_btn = gr.Button("Create Prompt")

        with gr.Accordion("Edit/Delete Selected Prompt", open=False):
            edit_prompt_id_display = gr.Textbox(label="Selected Prompt ID", interactive=False)
            edit_prompt_name = gr.Textbox(label="Prompt Name (Unique)")
            edit_prompt_text = gr.Textbox(label="Prompt Text", lines=5, max_lines=20)
            with gr.Row():
                update_prompt_btn = gr.Button("Update Selected Prompt", interactive=False)
                delete_prompt_btn = gr.Button("Delete Selected Prompt", variant="stop", interactive=False)
                set_default_prompt_btn = gr.Button("Set as Global Default", interactive=False)

        refresh_prompts_btn.click(list_prompts_data, outputs=[prompt_data_df])
        create_prompt_btn.click(
            handle_create_prompt,
            inputs=[new_prompt_name, new_prompt_text],
            outputs=[prompt_data_df]
        ).then(lambda: (None, "", ""), outputs=[selected_prompt_id_state, new_prompt_name, new_prompt_text])

        prompt_data_df.select(
            on_select_prompt,
            inputs=[prompt_data_df],
            outputs=[selected_prompt_id_state, edit_prompt_name, edit_prompt_text, update_prompt_btn, delete_prompt_btn,
                     set_default_prompt_btn, edit_prompt_id_display]
        )

        update_prompt_btn.click(
            handle_update_prompt,
            inputs=[selected_prompt_id_state, edit_prompt_name, edit_prompt_text],
            outputs=[prompt_data_df]
        )
        delete_prompt_btn.click(
            handle_delete_prompt,
            inputs=[selected_prompt_id_state],
            outputs=[prompt_data_df]
        )
        set_default_prompt_btn.click(
            handle_set_global_default_prompt,
            inputs=[selected_prompt_id_state],
            outputs=[prompt_data_df]
        )


# --- Model Pricing Management ---
async def list_model_pricing_data() -> pd.DataFrame:
    async with get_db_session() as session:
        # Eager load model name
        stmt = select(ModelPricing).options(selectinload(ModelPricing.model))
        result = await session.execute(stmt)
        pricing_records = []
        for record in result.scalars().all():
            pricing_records.append({
                "ID": record.id,
                "Model Name": record.model.name if record.model else "N/A",
                "Model ID": record.model_id,
                "Input Price (per Mtok)": record.input_price_per_million_tokens,
                "Output Price (per Mtok)": record.output_price_per_million_tokens,
                "Currency": record.currency,
                "Effective From": record.effective_from_date.isoformat() if record.effective_from_date else '',
                "Effective To": record.effective_to_date.isoformat() if record.effective_to_date else 'Ongoing',
            })
    return pd.DataFrame(pricing_records)


async def handle_create_model_pricing(
        model_id: int,
        input_price_str: str,
        output_price_str: str,
        currency: str,
        from_date_obj: datetime.date,  # Gradio Date now passes datetime.date directly
        to_date_obj: datetime.date | None  # Gradio Date now passes datetime.date or None directly
):
    if not model_id:
        gr.Warning("Model must be selected.")
        return await list_model_pricing_data()
    if not all([input_price_str, output_price_str, currency, from_date_obj]):
        gr.Warning("Input Price, Output Price, Currency, and Effective From Date are required.")
        return await list_model_pricing_data()

    try:
        input_price = Decimal(input_price_str)
        output_price = Decimal(output_price_str)
        if input_price < 0 or output_price < 0:
            raise ValueError("Prices cannot be negative.")

        if to_date_obj and to_date_obj < from_date_obj:  # from_date_obj is already a date object
            raise ValueError("Effective 'to' date cannot be before 'from' date.")

    except (ValueError, TypeError) as e:  # TypeError can occur if Decimal conversion fails
        gr.Error(f"Invalid input for price or date: {e}")
        return await list_model_pricing_data()

    async with get_db_session() as session:
        try:
            new_pricing = ModelPricing(
                model_id=model_id,
                input_price_per_million_tokens=input_price,
                output_price_per_million_tokens=output_price,
                currency=currency.upper(),
                effective_from_date=from_date_obj,  # Use the date object directly
                effective_to_date=to_date_obj  # Use the date object or None directly
            )
            session.add(new_pricing)
            await session.commit()
            gr.Info(f"Pricing added for Model ID {model_id} effective from {from_date_obj.isoformat()}.")
        except IntegrityError as e:  # Handles UniqueConstraint violation
            await session.rollback()
            gr.Error(
                f"Error: Pricing for this model and effective period might already exist or overlap. Details: {e.orig}")
        except Exception as e:
            await session.rollback()
            gr.Error(f"Failed to add pricing: {e}")
            logger.error(f"Gradio: Failed to add pricing for model ID {model_id}: {e}", exc_info=True)
    return await list_model_pricing_data()


async def handle_delete_model_pricing(pricing_id: int):
    if not pricing_id:
        gr.Warning("No pricing ID provided for deletion.")
        return await list_model_pricing_data()

    async with get_db_session() as session:
        try:
            pricing_record = await session.get(ModelPricing, pricing_id)
            if not pricing_record:
                gr.Error(f"Model Pricing record with ID {pricing_id} not found.")
                return await list_model_pricing_data()

            await session.delete(pricing_record)
            await session.commit()
            gr.Info(f"Model Pricing record ID {pricing_id} deleted successfully.")
        except Exception as e:
            await session.rollback()
            gr.Error(f"Failed to delete Model Pricing record ID {pricing_id}: {e}")
            logger.error(f"Gradio: Failed to delete pricing ID {pricing_id}: {e}", exc_info=True)
    return await list_model_pricing_data()


def _build_model_pricing_tab():
    with gr.TabItem("Model Pricing"):
        gr.Markdown("## Model Pricing Management")
        gr.Markdown("Define costs for LLM models. Prices are per million tokens.")

        pricing_data_df = gr.DataFrame(value=list_model_pricing_data, label="Model Pricing Records", interactive=False,
                                       key="model_pricing_df")
        selected_pricing_id_state = gr.State(None)

        def on_select_pricing(evt: gr.SelectData, df_data: pd.DataFrame):
            if evt.index is None or not isinstance(evt.index, tuple) or len(evt.index) == 0:
                return None, gr.Button(interactive=False), ""
            selected_row_index = evt.index[0]
            if selected_row_index < 0 or selected_row_index >= len(df_data):
                return None, gr.Button(interactive=False), ""

            selected_row = df_data.iloc[selected_row_index]
            pricing_id = selected_row["ID"]
            return pricing_id, gr.Button(interactive=True), str(pricing_id)

        with gr.Row():
            refresh_pricing_btn = gr.Button("🔄 Refresh Pricing Data")

        with gr.Accordion("Add New Model Pricing", open=False):
            # Use a dropdown for models that updates dynamically
            model_choices_dropdown_pricing = gr.Dropdown(
                label="Select LLM Model",
                choices=asyncio.run(get_llm_model_choices()),  # Initial load
                type="value"  # Ensure it returns the model ID
            )
            with gr.Row():
                new_pricing_input_price = gr.Textbox(label="Input Price (e.g., 0.50)")
                new_pricing_output_price = gr.Textbox(label="Output Price (e.g., 1.50)")
                new_pricing_currency = gr.Dropdown(CURRENCY_CHOICES, label="Currency", value="USD")
            with gr.Row():
                new_pricing_from_date = gr.Date(label="Effective From Date",
                                                type="date")  # type="date" helps ensure correct object
                new_pricing_to_date = gr.Date(label="Effective To Date (Optional)", type="date")  # type="date"
            create_pricing_btn = gr.Button("Add Pricing")

        with gr.Accordion("Delete Selected Pricing Record", open=False):
            delete_pricing_id_display = gr.Textbox(label="Selected Pricing Record ID", interactive=False)
            delete_pricing_btn = gr.Button("Delete Selected Pricing Record", variant="stop", interactive=False)

        # Event Handlers
        refresh_pricing_btn.click(list_model_pricing_data, outputs=[pricing_data_df])
        # Dynamic update for model dropdown when this tab is selected or models change (more complex, skip for now)
        # For now, user might need to refresh tab or page if models list changes significantly.

        create_pricing_btn.click(
            handle_create_model_pricing,
            inputs=[model_choices_dropdown_pricing, new_pricing_input_price, new_pricing_output_price,
                    new_pricing_currency, new_pricing_from_date, new_pricing_to_date],
            outputs=[pricing_data_df]
        ).then(lambda: (None, "", "", CURRENCY_CHOICES[0], None, None), outputs=[  # Reset form
            model_choices_dropdown_pricing, new_pricing_input_price, new_pricing_output_price, new_pricing_currency,
            new_pricing_from_date, new_pricing_to_date
        ])

        pricing_data_df.select(
            on_select_pricing,
            inputs=[pricing_data_df],
            outputs=[selected_pricing_id_state, delete_pricing_btn, delete_pricing_id_display]
        )

        delete_pricing_btn.click(
            handle_delete_model_pricing,
            inputs=[selected_pricing_id_state],
            outputs=[pricing_data_df]
        )


# --- Queue Management ---
async def list_queued_checks_data(status_filter: str) -> pd.DataFrame:
    async with get_db_session() as session:
        stmt = select(QueuedLLMCheck)
        if status_filter != "All":
            stmt = stmt.where(QueuedLLMCheck.status == status_filter)

        stmt = stmt.order_by(QueuedLLMCheck.status.desc(), QueuedLLMCheck.queued_at.asc()).limit(100)  # Limit for UI
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
                "Full Reason": item.reason_for_queueing,  # For detail view
                "Message (Preview)": ctx.get('message_text', '')[:100] + "..." if ctx.get('message_text') else '',
                "Full Message": ctx.get('message_text', ''),  # For detail view
            })
    return pd.DataFrame(items)


async def handle_reprocess_queued_item(item_id: int, current_status_filter: str):
    if not item_id:
        gr.Warning("No queued item ID provided for reprocessing.")
        return await list_queued_checks_data(current_status_filter)
    if not _llm_service_instance or not _action_service_instance:  # Check both services
        gr.Error("LLMService or ActionService not available for reprocessing.")
        logger.error("Gradio: LLMService or ActionService not initialized for queue reprocessing.")
        return await list_queued_checks_data(current_status_filter)

    async with get_db_session() as session:
        queued_item = await session.get(QueuedLLMCheck, item_id)
        if not queued_item:
            gr.Error(f"Queued item with ID {item_id} not found.")
            return await list_queued_checks_data(current_status_filter)

        if queued_item.status not in ["pending", "failed_reprocessing_attempt", "pending_admin_action"]:
            gr.Warning(f"Item {item_id} is currently '{queued_item.status}' and cannot be manually reprocessed now.")
            return await list_queued_checks_data(current_status_filter)

        gr.Info(f"Attempting to manually reprocess queued item ID: {item_id}...")
        # llm_service.reprocess_queued_item handles its own session flushing/committing related to the item
        # but the final state check should be done after it returns.
        success = await _llm_service_instance.reprocess_queued_item(session, item_id,
                                                                    _action_service_instance)  # Pass ActionService

        # Re-fetch to confirm state change or deletion, as reprocess_queued_item might commit or delete.
        # We need a new query within the same session or expect the session to reflect changes.
        # If reprocess_queued_item committed and closed its own session, this get might not see changes
        # unless the outer session here is also fresh.
        # Assuming reprocess_queued_item works within the passed session.
        await session.expire(queued_item)  # Expire the instance to force a refresh if it still exists
        final_item_state = await session.get(QueuedLLMCheck, item_id)

        if not final_item_state:  # Item was deleted (successfully processed)
            gr.Info(f"Item {item_id} successfully resolved (processed, sent for admin approval, or user approved).")
        else:  # Item still exists
            gr.Warning(
                f"Item {item_id} reprocessing did not lead to immediate resolution. Current status: '{final_item_state.status}'. Reason: {final_item_state.reason_for_queueing}")
    return await list_queued_checks_data(current_status_filter)


async def handle_discard_queued_item(item_id: int, current_status_filter: str):
    if not item_id:
        gr.Warning("No queued item ID provided for discarding.")
        return await list_queued_checks_data(current_status_filter)
    if not _action_service_instance:  # Check ActionService
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
            # ActionService.process_user_approval handles its own session flushing for NewUser
            await _action_service_instance.process_user_approval(session, user_id_to_approve, chat_id_to_approve)
            reply_message += f" User {user_id_to_approve} in chat {chat_id_to_approve} marked as approved."

        await session.delete(queued_item)
        await session.commit()  # Commit deletion of queued_item and user approval (if any)
        gr.Info(reply_message)
    return await list_queued_checks_data(current_status_filter)


def _build_queue_management_tab():
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
            # value=lambda sf: asyncio.run(list_queued_checks_data(sf)), # Initial load based on default filter
            label="Queued LLM Checks (Max 100 shown)",
            interactive=False,
            headers=["ID", "Status", "Chat ID", "User ID", "Msg ID", "Retries", "Queued", "Last Attempt",
                     "Reason (Preview)", "Full Reason", "Msg (Preview)", "Full Message"],
            # Adjust column widths: Full Reason and Full Message are hidden (width 0%) but data is there for selection
            column_widths=["3%", "10%", "8%", "8%", "8%", "5%", "10%", "10%", "20%", "0%", "18%", "0%"],
            key="queue_df"
        )

        selected_queue_item_id_state = gr.State(None)

        # Textboxes for displaying full details, initially hidden
        selected_queue_item_full_reason = gr.Textbox(label="Full Reason for Queuing", interactive=False, lines=3,
                                                     max_lines=10, visible=False)
        selected_queue_item_full_message = gr.Textbox(label="Full Message Text", interactive=False, lines=3,
                                                      max_lines=10, visible=False)

        def on_select_queue_item(evt: gr.SelectData, df_data: pd.DataFrame):
            if evt.index is None or not isinstance(evt.index, tuple) or len(evt.index) == 0:
                # Reset and hide detail textboxes, disable buttons
                return None, "", "", gr.Button(interactive=False), gr.Button(interactive=False), gr.Textbox(
                    visible=False), gr.Textbox(visible=False), ""

            selected_row_index = evt.index[0]
            if selected_row_index < 0 or selected_row_index >= len(df_data):
                return None, "", "", gr.Button(interactive=False), gr.Button(interactive=False), gr.Textbox(
                    visible=False), gr.Textbox(visible=False), ""

            selected_row = df_data.iloc[selected_row_index]
            item_id = selected_row["ID"]
            full_reason = selected_row.get("Full Reason", "")  # Use .get for safety if column somehow missing
            full_message = selected_row.get("Full Message", "")

            # Determine if reprocess button should be active
            can_reprocess = selected_row["Status"] in ["pending", "failed_reprocessing_attempt", "pending_admin_action"]

            # Return new values for states and component properties (visibility, interactivity)
            return item_id, full_reason, full_message, gr.Button(interactive=can_reprocess), gr.Button(
                interactive=True), gr.Textbox(visible=True, value=full_reason), gr.Textbox(visible=True,
                                                                                           value=full_message), str(
                item_id)

        with gr.Row():
            refresh_queue_btn = gr.Button("🔄 Refresh Queue Data")

        with gr.Accordion("Selected Item Details & Actions", open=False) as details_accordion:
            queue_item_id_display = gr.Textbox(label="Selected Item ID", interactive=False)
            # selected_queue_item_full_reason and selected_queue_item_full_message are updated by on_select_queue_item
            gr.Markdown("---")  # Separator
            gr.Label("Full Reason for Queuing:")
            selected_queue_item_full_reason_display = gr.Textbox(interactive=False, lines=3,
                                                                 max_lines=10)  # Display area
            gr.Label("Full Message Text:")
            selected_queue_item_full_message_display = gr.Textbox(interactive=False, lines=3,
                                                                  max_lines=10)  # Display area

            with gr.Row():
                reprocess_item_btn = gr.Button("♻️ Reprocess Selected Item", interactive=False)
                discard_item_btn = gr.Button("🗑️ Discard Selected Item (and Approve User)", variant="stop",
                                             interactive=False)

        # Event Handlers
        # Initial load of queue data when tab is rendered (or app starts)
        # We need to use a lambda for the initial load if it depends on the filter's default value

        def initial_queue_load(status_filter_val):
            return asyncio.run(list_queued_checks_data(status_filter_val))

        # queue_data_df.value = initial_queue_load(QUEUE_STATUS_FILTER_CHOICES[1]) # Load with default filter on startup
        # Instead of .value, use .load on the Blocks for initial data with dependencies

        refresh_queue_btn.click(
            lambda sf: asyncio.run(list_queued_checks_data(sf)),  # Lambda to correctly pass the filter value
            inputs=[queue_status_filter_dd],
            outputs=[queue_data_df]
        )
        queue_status_filter_dd.change(  # Refresh data when filter changes
            lambda sf: asyncio.run(list_queued_checks_data(sf)),
            inputs=[queue_status_filter_dd],
            outputs=[queue_data_df]
        )

        queue_data_df.select(
            on_select_queue_item,
            inputs=[queue_data_df],
            outputs=[
                selected_queue_item_id_state,
                selected_queue_item_full_reason,  # State for full reason
                selected_queue_item_full_message,  # State for full message
                reprocess_item_btn,
                discard_item_btn,
                selected_queue_item_full_reason_display,  # Textbox to display full reason
                selected_queue_item_full_message_display,  # Textbox to display full message
                queue_item_id_display,
            ]
        ).then(lambda: gr.Accordion(open=True), outputs=[details_accordion])  # Open accordion on select

        reprocess_item_btn.click(
            lambda item_id, sf: asyncio.run(handle_reprocess_queued_item(item_id, sf)),
            inputs=[selected_queue_item_id_state, queue_status_filter_dd],  # Pass filter for refresh
            outputs=[queue_data_df]
        )
        discard_item_btn.click(
            lambda item_id, sf: asyncio.run(handle_discard_queued_item(item_id, sf)),
            inputs=[selected_queue_item_id_state, queue_status_filter_dd],  # Pass filter for refresh
            outputs=[queue_data_df]
        )


# --- Dashboard ---
async def get_bot_status() -> str:
    if not _app_settings:
        return "Bot Status: Error - App settings not initialized for UI."
    db_status = "DB Status: Unknown"
    try:
        async with get_db_session() as session:
            await session.execute(text("SELECT 1"))  # Simple query to check connection
        db_status = "DB Connected"
    except Exception as e:
        db_status = f"DB Connection Error: {type(e).__name__}"
        logger.warning(f"Gradio UI: DB connection check failed: {e}")

    # Count pending_admin_action queue items
    queue_admin_count = 0
    try:
        async with get_db_session() as session:
            stmt = select(func.count(QueuedLLMCheck.id)).where(QueuedLLMCheck.status == "pending_admin_action")
            count_result = await session.execute(stmt)
            queue_admin_count = count_result.scalar_one_or_none() or 0
    except Exception as e:
        logger.warning(f"Gradio UI: Failed to count admin-action queue items: {e}")

    return f"Bot Status:\n- {db_status}\n- Items needing admin action in queue: {queue_admin_count}"


# --- Main UI Construction ---
def create_main_ui_layout():
    with gr.Blocks(title="Staring Misaka Admin", theme=gr.themes.Soft()) as ui:
        gr.Markdown("# Staring Misaka Admin UI")

        with gr.Tabs():
            with gr.TabItem("Dashboard", id="dashboard_tab"):
                gr.Markdown("## Dashboard")
                status_output = gr.Textbox(label="Bot Status", interactive=False,
                                           lines=4)  # Increased lines for queue count
                refresh_status_btn = gr.Button("Refresh Status")
                refresh_status_btn.click(get_bot_status, outputs=status_output)
                # ui.load(get_bot_status, outputs=status_output) # Load on app start

            _build_llm_models_tab()
            _build_prompts_tab()
            _build_model_pricing_tab()
            _build_queue_management_tab()  # Added Queue Management Tab

            with gr.TabItem("Monitored Groups", id="groups_tab"):
                gr.Markdown("## Monitored Group Management")
                gr.Markdown("_(Functionality to be implemented)_")

            with gr.TabItem("LLM Logs", id="logs_tab"):
                gr.Markdown("## LLM Logs Viewer")
                gr.Markdown("_(Functionality to be implemented)_")

        # Use the .load event on the Blocks itself for initial data loading
        # This is generally preferred for loading data when the app starts or a tab becomes visible.
        # For the dashboard status and initial queue load:
        default_queue_filter = QUEUE_STATUS_FILTER_CHOICES[1]  # e.g., "pending_admin_action"

        # Find the components by key/id if defined, or pass them directly if `ui` is the Blocks context
        # For components defined inside _build_... functions, direct reference might be tricky
        # Gradio's .load on Blocks usually targets components defined directly within its context.
        # A common pattern is to have the loading function return a dictionary of updates.

        # For simplicity, we will rely on the refresh buttons and default filter value for initial load,
        # or the DataFrame's `value` callable for its first render.
        # The `ui.load` for dashboard status is fine.
        # For the queue, the DataFrame `value` callable will run with the default filter.
        # Let's ensure queue_data_df's initial value callable uses the filter's default.
        # This is handled by how queue_data_df is defined:
        # value=lambda: asyncio.run(list_queued_checks_data(queue_status_filter_dd.value)) -> This won't work directly
        # The lambda for queue_data_df's value should take the filter's value.
        # A better approach is to load it via the filter's change event or a refresh button initially.
        # The current setup using queue_status_filter_dd.change and refresh_queue_btn.click will handle loading.
        # To ensure initial load for queue_data_df:
        ui.load(
            lambda sf_val: asyncio.run(list_queued_checks_data(sf_val)),
            inputs=[gr.State(value=default_queue_filter)],  # Use gr.State to pass initial filter value
            outputs=ui.key_to_elem_map["queue_df"]  # Target DataFrame by key
        )
        ui.load(get_bot_status, outputs=status_output)

    return ui


# --- Gradio Launch Logic ---
def _run_gradio_app(ui_instance: gr.Blocks, settings: "Settings"):
    auth_tuple = None
    if settings.gradio_username and settings.gradio_password and settings.gradio_password.get_secret_value():
        auth_tuple = (settings.gradio_username, settings.gradio_password.get_secret_value())
        logger.info(f"Gradio UI starting with authentication enabled on port {settings.gradio_port}.")
    else:
        logger.warning(
            f"Gradio UI starting WITHOUT authentication on port {settings.gradio_port}. "
            "Set GRADIO_USERNAME and GRADIO_PASSWORD environment variables to enable."
        )
    try:
        ui_instance.launch(
            server_name="0.0.0.0",
            server_port=settings.gradio_port,
            auth=auth_tuple,
        )
        logger.info("Gradio UI server has been stopped.")
    except Exception as e:
        logger.error(f"Gradio UI server failed to launch or crashed: {e}", exc_info=True)


def launch_gradio_ui(
        settings: "Settings",
        llm_service: "LLMService",  # Pass LLMService
        action_service: "ActionService",  # Pass ActionService
        main_event_loop: "asyncio.AbstractEventLoop"
):
    global _app_settings, _main_event_loop, _llm_service_instance, _action_service_instance
    _app_settings = settings
    _main_event_loop = main_event_loop
    _llm_service_instance = llm_service  # Store LLMService
    _action_service_instance = action_service  # Store ActionService

    if not (settings.gradio_username and settings.gradio_password and settings.gradio_password.get_secret_value()):
        logger.warning("Gradio username or password not set. UI will start without authentication.")

    logger.info("Initializing Gradio Web UI...")
    admin_ui_instance = create_main_ui_layout()

    gradio_thread = threading.Thread(
        target=_run_gradio_app,
        args=(admin_ui_instance, settings),
        name="GradioUIServerThread",
        daemon=True
    )
    gradio_thread.start()
    logger.info(f"Gradio UI thread started. Access at http://<your_ip>:{settings.gradio_port}")