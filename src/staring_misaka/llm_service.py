import datetime
import logging
from abc import ABC, abstractmethod
from datetime import timezone  # For timezone-aware datetime objects
from decimal import Decimal
from typing import Any

import instructor
from anthropic import APIError as AnthropicAPIError
from anthropic import AsyncAnthropic
from openai import APIError as OpenAIAPIError
from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .action_service import ActionService
from .config import Settings
from .db_models import (
    GlobalBotSettings,
    LLMLog,
    LLMModel,
    ModelPricing,
    MonitoredGroup,
    NewUser,
    Prompt,
    QueuedLLMCheck,
)
from .dto import LLMSpamAnalysisResult, MessageContext
from .metrics_service import (
    LLM_API_ERRORS,
    LLM_API_REQUESTS,
    LLM_RESPONSE_LATENCY_SECONDS,
    record_llm_cost,
    record_llm_tokens,
)
from .telegram_utils import send_message_to_chat

# TODO: Consider adding a common base exception for provider-specific API errors for easier catching.

logger = logging.getLogger(__name__)


# --- LLM Provider Abstraction ---

class LLMProviderStrategy(ABC):
    """Abstract base class for LLM provider strategies."""

    def __init__(self, api_key: str | None, settings: Settings):
        self.api_key = api_key
        self.settings = settings  # Store settings for potential use by strategies
        self.client: Any = None  # Will be the initialized provider-specific client (e.g., instructor client)
        self._initialize_client()

    @abstractmethod
    def _initialize_client(self):
        """Initializes the specific LLM provider's client.
        Should set self.client or log an error if initialization fails.
        Sets self.client to None on failure.
        """
        pass

    @abstractmethod
    async def analyze(self, model_api_identifier: str, formatted_prompt: str,
                      response_pydantic_model: type[LLMSpamAnalysisResult]) -> LLMSpamAnalysisResult:
        """
        Sends request to the LLM and returns a structured Pydantic model.
        The implementation must populate `input_tokens`, `output_tokens`, and `status` in the result.
        Should raise provider-specific API errors or ConnectionError if client is not initialized.
        """
        pass


class AnthropicProviderStrategy(LLMProviderStrategy):
    """Strategy for interacting with Anthropic models via instructor."""

    def _initialize_client(self):
        if not self.api_key:
            logger.warning("Anthropic API key not provided. Anthropic models will not be available.")
            self.client = None
            return
        try:
            anthropic_sdk_client = AsyncAnthropic(api_key=self.api_key)
            # Using ANTHROPIC_TOOLS mode for Claude 3 structured output with Pydantic models
            self.client = instructor.from_anthropic(anthropic_sdk_client, mode=instructor.Mode.ANTHROPIC_TOOLS)
            logger.info("Anthropic client initialized successfully with instructor.")
        except Exception as e:
            logger.error(f"Failed to initialize Anthropic client: {e}", exc_info=True)
            self.client = None  # Ensure client is None on failure

    async def analyze(self, model_api_identifier: str, formatted_prompt: str,
                      response_pydantic_model: type[LLMSpamAnalysisResult]) -> LLMSpamAnalysisResult:
        if not self.client:
            # This indicates an issue during _initialize_client
            raise ConnectionError("Anthropic client is not initialized (check API key or setup).")

        try:
            # Using create_with_completion to get both parsed model and raw completion for token usage
            parsed_response, raw_completion = await self.client.messages.create_with_completion(
                model=model_api_identifier,
                max_tokens=256, # TODO: Consider making max_tokens configurable
                messages=[{"role": "user", "content": formatted_prompt}],
                response_model=response_pydantic_model,
            )
            input_tokens = raw_completion.usage.input_tokens if hasattr(raw_completion,
                                                                        'usage') and raw_completion.usage else 0
            output_tokens = raw_completion.usage.output_tokens if hasattr(raw_completion,
                                                                          'usage') and raw_completion.usage else 0
            if not (input_tokens or output_tokens) and not (
                    parsed_response.is_spam is not None and parsed_response.reason):  # Check if tokens are zero and no valid response data
                logger.warning(
                    f"Token usage reported as 0/0 by Anthropic for model {model_api_identifier} and response seems incomplete. This might be an issue or expected for some error cases.")

            parsed_response.input_tokens = input_tokens
            parsed_response.output_tokens = output_tokens
            parsed_response.model_name_used = model_api_identifier
            parsed_response.status = "success"
            return parsed_response
        except AnthropicAPIError as e:
            logger.error(
                f"Anthropic API error during analysis (model: {model_api_identifier}): {e.status_code} - {e.message}",
                exc_info=True)
            raise
        except Exception as e:
            logger.error(f"Unexpected error during Anthropic analysis (model: {model_api_identifier}): {e}",
                         exc_info=True)
            raise


class OpenAIProviderStrategy(LLMProviderStrategy):
    """Strategy for interacting with OpenAI models via instructor."""

    def _initialize_client(self):
        if not self.api_key:
            logger.warning("OpenAI API key not provided. OpenAI models will not be available.")
            self.client = None
            return
        try:
            openai_sdk_client = AsyncOpenAI(api_key=self.api_key)
            # Using JSON mode for structured JSON output based on Pydantic model
            self.client = instructor.from_openai(openai_sdk_client, mode=instructor.Mode.JSON)
            logger.info("OpenAI client initialized successfully with instructor.")
        except Exception as e:
            logger.error(f"Failed to initialize OpenAI client: {e}", exc_info=True)
            self.client = None  # Ensure client is None on failure

    async def analyze(self, model_api_identifier: str, formatted_prompt: str,
                      response_pydantic_model: type[LLMSpamAnalysisResult]) -> LLMSpamAnalysisResult:
        if not self.client:
            raise ConnectionError("OpenAI client is not initialized (check API key or setup).")

        try:
            # Using create_with_completion for OpenAI as well to consistently get raw_completion for tokens
            parsed_response, raw_completion = await self.client.chat.completions.create_with_completion(
                model=model_api_identifier,
                response_model=response_pydantic_model,
                messages=[
                    {"role": "system",
                     "content": "You are a helpful assistant designed to output JSON according to the provided schema for spam detection."},
                    {"role": "user", "content": formatted_prompt}
                ],
                max_tokens=256, # TODO: Consider making max_tokens configurable
            )
            input_tokens = raw_completion.usage.prompt_tokens if hasattr(raw_completion,
                                                                         'usage') and raw_completion.usage else 0
            output_tokens = raw_completion.usage.completion_tokens if hasattr(raw_completion,
                                                                              'usage') and raw_completion.usage else 0
            if not (input_tokens or output_tokens) and not (
                    parsed_response.is_spam is not None and parsed_response.reason):
                logger.warning(
                    f"Token usage reported as 0/0 by OpenAI for model {model_api_identifier} and response seems incomplete. This might be an issue or expected for some error cases.")

            parsed_response.input_tokens = input_tokens
            parsed_response.output_tokens = output_tokens
            parsed_response.model_name_used = model_api_identifier
            parsed_response.status = "success"
            return parsed_response
        except OpenAIAPIError as e:
            error_body_message = e.body.get('message') if isinstance(e.body, dict) else str(e.body or e.message)
            logger.error(
                f"OpenAI API error during analysis (model: {model_api_identifier}): {e.status_code} - {error_body_message}",
                exc_info=True)
            raise
        except Exception as e:
            logger.error(f"Unexpected error during OpenAI analysis (model: {model_api_identifier}): {e}", exc_info=True)
            raise


# --- Main LLM Service ---

class LLMService:
    """Service responsible for interacting with LLMs, handling different providers,
       calculating costs, logging interactions, and managing queueing on failure."""

    def __init__(self, settings: Settings, telegram_client: Any | None = None):
        self.settings = settings
        self.telegram_client = telegram_client
        self.provider_strategies: dict[str, LLMProviderStrategy] = {}
        self._initialize_strategies()
        logger.info("LLMService initialized with available provider strategies.")

    def _initialize_strategies(self):
        """Initializes strategies for all LLM providers configured with API keys."""
        if self.settings.anthropic_api_key and self.settings.anthropic_api_key.get_secret_value():
            self.provider_strategies["Anthropic"] = AnthropicProviderStrategy(
                self.settings.anthropic_api_key.get_secret_value(), self.settings
            )
        if self.settings.openai_api_key and self.settings.openai_api_key.get_secret_value():
            self.provider_strategies["OpenAI"] = OpenAIProviderStrategy(
                self.settings.openai_api_key.get_secret_value(), self.settings
            )

    async def _get_active_prompt_and_model(self, session: AsyncSession, chat_id: int | None) -> tuple[
        Prompt | None, LLMModel | None]:
        """Determines the active prompt and LLM model based on group-specific overrides or global defaults."""
        prompt_to_use = None
        model_to_use = None
        global_settings = await session.get(GlobalBotSettings, 1)  # Fetch global settings (singleton)
        if not global_settings:  # Critical configuration issue
            logger.error("GlobalBotSettings record not found in the database!")
            return None, None

        # Check for group-specific settings if a chat_id is provided
        if chat_id:
            group_settings = await session.get(MonitoredGroup, chat_id)
            if group_settings:
                if group_settings.custom_prompt_id:
                    prompt_to_use = await session.get(Prompt, group_settings.custom_prompt_id)
                    if not prompt_to_use: logger.warning(
                        f"Group {chat_id} custom_prompt_id {group_settings.custom_prompt_id} is invalid.")
                if group_settings.custom_model_id:
                    model_to_use = await session.get(LLMModel, group_settings.custom_model_id)
                    if not model_to_use: logger.warning(
                        f"Group {chat_id} custom_model_id {group_settings.custom_model_id} is invalid.")

        # Fallback to global defaults if not set at the group level or if group config is invalid
        if not prompt_to_use and global_settings.default_prompt_id:
            prompt_to_use = await session.get(Prompt, global_settings.default_prompt_id)
            if not prompt_to_use: logger.warning(
                f"Global default_prompt_id {global_settings.default_prompt_id} is invalid.")
        if not model_to_use and global_settings.default_model_id:
            model_to_use = await session.get(LLMModel, global_settings.default_model_id)
            if not model_to_use: logger.warning(
                f"Global default_model_id {global_settings.default_model_id} is invalid.")

        # Log final decision or lack thereof
        if not prompt_to_use: logger.warning(f"Could not determine active prompt for chat {chat_id} (or globally).")
        if not model_to_use: logger.warning(f"Could not determine active LLM model for chat {chat_id} (or globally).")
        return prompt_to_use, model_to_use

    async def _get_model_pricing(self, session: AsyncSession, model_id: int,
                                 timestamp: datetime.datetime) -> ModelPricing | None:
        """Fetches the applicable pricing record for a given model ID and timestamp."""
        # Selects the pricing record where the timestamp falls within the effective date range.
        # Orders by effective_from_date descending to get the most recent applicable rate.
        stmt = (
            select(ModelPricing)
            .where(ModelPricing.model_id == model_id)
            .where(ModelPricing.effective_from_date <= timestamp.date())
            .where((ModelPricing.effective_to_date.is_(None)) | (ModelPricing.effective_to_date >= timestamp.date()))
            .order_by(ModelPricing.effective_from_date.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalars().first()  # Returns the single most relevant pricing record or None

    async def _calculate_cost(self, pricing: ModelPricing, input_tokens: int, output_tokens: int) -> Decimal:
        """Calculates the estimated cost based on token counts and pricing information."""
        cost = Decimal("0.0")
        if pricing:  # Ensure pricing object exists
            # Calculate cost based on price per million tokens
            cost += (Decimal(input_tokens or 0) / Decimal("1000000")) * pricing.input_price_per_million_tokens
            cost += (Decimal(output_tokens or 0) / Decimal("1000000")) * pricing.output_price_per_million_tokens
        else:
            logger.warning("Attempted to calculate cost but no valid pricing information was provided.")
        return cost

    async def _notify_admin_and_queue_check(
            self, session: AsyncSession, context: MessageContext, reason_for_failure: str,
            active_prompt: Prompt | None, active_model: LLMModel | None
    ) -> LLMSpamAnalysisResult:
        """Handles critical LLM failures by queuing the check and notifying the admin."""

        logger.error(
            f"Critical LLM failure: {reason_for_failure}. Queuing message check for user {context.user_id}, msg_id {context.message_id} in chat {context.chat_id}.")

        # Create a record in the queue table
        queued_item = QueuedLLMCheck(
            message_context_json=context.model_dump(mode='json'),
            reason_for_queueing=reason_for_failure,
            original_model_id_attempted=active_model.id if active_model else None,
            original_prompt_id_attempted=active_prompt.id if active_prompt else None,
            last_attempted_at=datetime.datetime.now(timezone.utc),
            status="pending"
        )
        session.add(queued_item)
        await session.flush()  # Ensure queued_item is in DB before admin notification or return

        # Notify the super admin via Telegram if the client is available
        if self.telegram_client and self.settings.admin_id:
            admin_message = (
                f"⚠️ LLM Check Failed & Queued ⚠️\n"
                f"Reason: {reason_for_failure}\n"
                f"Chat: {context.chat_id}, User: {context.user_id}, Msg: {context.message_id}\n"
                f"Model Attempted: {active_model.name if active_model else 'N/A'}\n"
                f"Message (preview): {context.message_text[:100]}...\n"
                f"This check has been queued for automatic retry. Please investigate the underlying issue."
            )
            try:
                # Use the utility function to send the message
                await send_message_to_chat(self.telegram_client, self.settings.admin_id, admin_message)
            except Exception as e_notify:
                # Log failure to notify admin, but don't let it stop the queuing process
                logger.error(f"Failed to send admin notification about queued LLM check: {e_notify}", exc_info=True)

        # Return a DTO indicating the check was deferred, including the reason
        return LLMSpamAnalysisResult(
            status="deferred_admin_notified",
            error_message=reason_for_failure,
            model_name_used=active_model.api_identifier if active_model else "ConfigurationIssue"
            # Indicate what was attempted
        )

    async def analyze_message_for_spam(
            self, session: AsyncSession, context: MessageContext, is_reprocessing: bool = False
    ) -> LLMSpamAnalysisResult:
        """
        Analyzes a message for spam using the configured LLM provider and prompt.
        Handles errors by queuing the check and notifying the admin if necessary.

        Args:
            session: The active database session.
            context: The context of the message to analyze.
            is_reprocessing: Flag indicating if this call is for a queued item reprocessing.

        Returns:
            An LLMSpamAnalysisResult DTO indicating the outcome.
        """
        active_prompt, active_model = await self._get_active_prompt_and_model(session, context.chat_id)

        # --- Pre-analysis checks for critical configuration issues ---
        if not active_prompt:
            return await self._notify_admin_and_queue_check(session, context,
                                                            "No active prompt configured for this chat/globally.", None,
                                                            active_model)
        if not active_model:
            return await self._notify_admin_and_queue_check(session, context,
                                                            "No active LLM model configured for this chat/globally.",
                                                            active_prompt, None)

        provider_name = active_model.provider
        strategy = self.provider_strategies.get(provider_name)

        if not strategy:
            return await self._notify_admin_and_queue_check(
                session, context,
                f"Unsupported LLM provider: '{provider_name}' for model '{active_model.name}'. Check model configuration or LLMService setup.",
                active_prompt, active_model
            )
        # Check if the provider's client was successfully initialized (e.g., API key might be invalid/missing)
        if not strategy.client:
            return await self._notify_admin_and_queue_check(
                session, context,
                f"LLM client for provider '{provider_name}' is not initialized (API key or setup issue).",
                active_prompt, active_model
            )

        # --- Prepare and execute LLM analysis ---
        formatted_prompt_text = active_prompt.text.format(message_text=context.message_text)
        # Initialize LLMLog entry - will be populated fully after the analysis attempt
        log_entry = LLMLog(
            chat_id=context.chat_id, user_id=context.user_id, message_id=context.message_id,
            prompt_id=active_prompt.id, model_id=active_model.id,
            full_prompt_text=formatted_prompt_text, timestamp=datetime.datetime.now(timezone.utc),
            llm_is_spam=False  # Assume not spam initially, will be updated based on LLM result or error
        )
        session.add(log_entry)  # Add to session early; its attributes will be updated

        # Increment API request counter only for initial processing, not for retries from the queue
        if not is_reprocessing:
            chat_id_label = str(context.chat_id) if context.chat_id else "unknown_chat"
            LLM_API_REQUESTS.labels(model_name=active_model.api_identifier, chat_id_label=chat_id_label).inc()

        analysis_result_dto: LLMSpamAnalysisResult | None = None
        try:
            # Time the LLM API call duration
            with LLM_RESPONSE_LATENCY_SECONDS.labels(model_name=active_model.api_identifier).time():
                # Call the appropriate provider strategy
                analysis_result_dto = await strategy.analyze(active_model.api_identifier, formatted_prompt_text,
                                                             LLMSpamAnalysisResult)

            # Basic validation that the strategy returned a DTO
            if not analysis_result_dto:
                # This case indicates a programming error in the strategy if it can return None without exception.
                raise ValueError(
                    f"LLM analysis strategy for {provider_name} (model {active_model.api_identifier}) returned None unexpectedly.")

            # --- Process successful analysis ---
            log_entry.llm_is_spam = analysis_result_dto.is_spam if analysis_result_dto.is_spam is not None else False  # Handle potential None from LLM/parsing
            log_entry.llm_reason = analysis_result_dto.reason
            log_entry.input_tokens = analysis_result_dto.input_tokens or 0
            log_entry.output_tokens = analysis_result_dto.output_tokens or 0
            log_entry.raw_response_payload = analysis_result_dto.model_dump_json(exclude_none=True)
            record_llm_tokens(active_model.api_identifier, log_entry.input_tokens, log_entry.output_tokens)

            # Calculate and record estimated cost
            pricing = await self._get_model_pricing(session, active_model.id, log_entry.timestamp)
            if pricing:
                cost = await self._calculate_cost(pricing, log_entry.input_tokens, log_entry.output_tokens)
                log_entry.calculated_cost = cost
                log_entry.cost_currency = pricing.currency
                record_llm_cost(active_model.api_identifier, cost, pricing.currency)
            else:
                logger.warning(
                    f"No pricing found for model {active_model.name} (ID: {active_model.id}) at timestamp {log_entry.timestamp}. Cost not calculated.")

            logger.info(
                f"LLM ({active_model.name} via {provider_name}) analysis successful: user {context.user_id}, chat {context.chat_id}, "
                f"spam={analysis_result_dto.is_spam}, reason='{analysis_result_dto.reason}'. Tokens I/O: {log_entry.input_tokens}/{log_entry.output_tokens}"
            )
            analysis_result_dto.status = "success"  # Ensure status is set correctly
            await session.flush()  # Ensure log_entry (updated) is flushed to the DB transaction
            return analysis_result_dto

        except ConnectionError as e:  # Catches uninitialized client error raised from strategy.analyze()
            error_msg = f"LLM Client Connection Error for provider {provider_name}: {e}"
            LLM_API_ERRORS.labels(model_name=active_model.api_identifier,
                                  error_type=f"connection_error_{provider_name.lower()}").inc()
            log_entry.llm_reason = error_msg  # Update existing log_entry
            await session.flush()  # Flush the updated log_entry
            # Queue this check as it likely requires configuration fix (API key etc.)
            return await self._notify_admin_and_queue_check(session, context, error_msg, active_prompt, active_model)

        except (AnthropicAPIError, OpenAIAPIError) as e:  # Catch specific provider API errors
            error_msg = f"API Error with provider {provider_name} for model {active_model.api_identifier}: {e!s}"
            LLM_API_ERRORS.labels(model_name=active_model.api_identifier,
                                  error_type=f"api_error_{provider_name.lower()}").inc()
            log_entry.llm_reason = error_msg  # Update existing log_entry
            await session.flush()  # Flush the updated log_entry

            # If this is an initial processing attempt, queue it. Admins need to check API status/keys.
            if not is_reprocessing:
                return await self._notify_admin_and_queue_check(session, context, error_msg, active_prompt,
                                                                active_model)
            else:
                # If it's a reprocessing attempt that hit an API error again, don't re-queue automatically.
                # Return an error status for the reprocessor task to handle (e.g., mark as failed_reprocessing).
                logger.warning(
                    f"API error occurred during reprocessing attempt for msg {context.message_id}. Error: {error_msg}")
                return LLMSpamAnalysisResult(status="critical_error_no_check", error_message=error_msg,
                                             model_name_used=active_model.api_identifier)

        except Exception as e:  # Catch other unexpected errors during the analysis process
            error_msg = f"Unexpected error during {provider_name} spam check for model {active_model.api_identifier}: {e}"
            LLM_API_ERRORS.labels(model_name=active_model.api_identifier, error_type="unknown_exception").inc()
            log_entry.llm_reason = error_msg  # Update existing log_entry
            await session.flush()  # Flush the updated log_entry

            # Queue unexpected errors for initial processing attempts.
            if not is_reprocessing:
                return await self._notify_admin_and_queue_check(session, context, error_msg, active_prompt,
                                                                active_model)
            else:
                # If error happens during reprocessing, return critical error status.
                logger.error(
                    f"Unexpected error during reprocessing attempt for msg {context.message_id}. Error: {error_msg}",
                    exc_info=True)
                return LLMSpamAnalysisResult(status="critical_error_no_check", error_message=error_msg,
                                             model_name_used=active_model.api_identifier)

    async def reprocess_queued_item(self, session: AsyncSession, queued_item_id: int,
                                    action_service: ActionService) -> bool:  # Pass ActionService
        """
        Attempts to reprocess a single queued LLM check.
        Handles success (creating PendingAdminAction if spam), failure (updating status), and max retries.

        Args:
            session: The database session.
            queued_item_id: The ID of the QueuedLLMCheck item to process.
            action_service: The ActionService instance to request admin approval.

        Returns:
            True if successfully processed and removed/actioned, False otherwise.
        """
        queued_item = await session.get(QueuedLLMCheck, queued_item_id)
        if not queued_item:
            logger.warning(f"Attempted to reprocess non-existent queued item ID: {queued_item_id}")
            return False

        # Allow manual reprocessing of pending_admin_action if admin triggers it
        if queued_item.status not in ["pending", "failed_reprocessing_attempt", "pending_admin_action"]:
            logger.info(
                f"Queued item {queued_item_id} status ('{queued_item.status}') not eligible for this reprocessing path. Skipping.")
            return False

        logger.info(
            f"Reprocessing queued LLM check ID: {queued_item.id} (Retry: {queued_item.retry_count + 1})")

        # Update status and retry count before processing
        queued_item.status = "processing"
        queued_item.last_attempted_at = datetime.datetime.now(timezone.utc)
        queued_item.retry_count += 1
        await session.flush()  # Make status update visible within this transaction immediately

        try:
            message_context_data = queued_item.message_context_json
            if not isinstance(message_context_data, dict):
                raise ValueError("Stored message_context_json is not a valid dictionary.")
            message_context = MessageContext(**message_context_data)

            # Call analyze_message_for_spam with is_reprocessing=True
            # analyze_message_for_spam now flushes its own LLMLog
            analysis_result = await self.analyze_message_for_spam(session, message_context, is_reprocessing=True)

            if analysis_result.status == "success":
                logger.info(
                    f"Reprocessing successful for item {queued_item.id}. Spam: {analysis_result.is_spam}, Reason: {analysis_result.reason}")

                # --- Post-Reprocessing Action ---
                if analysis_result.is_spam:
                    # If spam detected on reprocessing, create a PendingAdminAction for review.
                    # This avoids potentially very delayed automatic bans.
                    logger.warning(
                        f"Queued item {queued_item.id} (User: {message_context.user_id}) was found to be SPAM upon reprocessing. Creating PendingAdminAction for admin review.")
                    # Use ActionService to request approval
                    # This will also flush its PendingAdminAction
                    await action_service.request_admin_approval_for_ban(
                        session, message_context,
                        f"(From Reprocessed Queue) {analysis_result.reason or 'LLM detected spam.'}"
                    )
                elif analysis_result.is_spam is False:  # Explicitly check for False
                    # User is not spam, remove from NewUser table if they were there
                    new_user_entry = await session.get(NewUser, {"user_id": message_context.user_id,
                                                                 "chat_id": message_context.chat_id})
                    if new_user_entry:
                        await session.delete(new_user_entry)
                        await session.flush()  # Flush deletion
                        logger.info(
                            f"User {message_context.user_id} approved in chat {message_context.chat_id} after successful non-spam reprocessing of queued item {queued_item.id}.")

                # On successful outcome (spam or not), remove the item from the queue
                await session.delete(queued_item)
                await session.flush()  # Flush deletion of queued_item
                return True  # Indicates successful resolution of this item

            elif analysis_result.status == "deferred_admin_notified":
                # Analysis during reprocessing *again* resulted in deferral.
                # The analyze function already created and flushed a *new* QueuedLLMCheck item.
                # We should delete the *current* one we were processing.
                logger.warning(
                    f"Reprocessing item {queued_item.id} resulted in another deferral. Original item deleted, new queued item created by analyze_message_for_spam.")
                await session.delete(queued_item)  # Delete the old item, as a new one was created
                await session.flush()
                return False  # Not "success" in terms of final verdict for this item, but it's been handled by re-queuing

            else:  # status == "critical_error_no_check" during reprocessing
                # The reprocessing attempt failed critically (e.g., persistent API error).
                queued_item.status = "failed_reprocessing_attempt"  # Mark as failed this attempt
                queued_item.reason_for_queueing = f"Reprocess critical error: {analysis_result.error_message or 'Unknown critical error'}"
                await session.flush()  # Persist the updated status and reason
                logger.error(
                    f"Critical error during reprocessing of item {queued_item.id}: {analysis_result.error_message}")

        except Exception as e:
            # Catch any unexpected errors during the reprocessing logic itself
            logger.error(f"Unhandled exception during reprocessing logic for item {queued_item.id}: {e}", exc_info=True)
            queued_item.status = "failed_reprocessing_attempt"  # Mark as failed
            queued_item.reason_for_queueing = f"Reprocessing logic exception: {str(e)[:250]}"  # Store truncated error
            await session.flush()  # Persist the updated status and reason

        # --- Check Max Retries (only if reprocessing didn't succeed and delete the item) ---
        # This check applies if the status wasn't 'success' leading to deletion.
        if queued_item.retry_count >= self.settings.queue.max_automatic_retries and queued_item.status != "pending_admin_action":
            queued_item.status = "pending_admin_action"  # Changed from "failed_max_retries"
            await session.flush()  # Persist status change
            logger.error(
                f"Queued item {queued_item.id} reached max auto retries ({self.settings.queue.max_automatic_retries}). Status set to 'pending_admin_action'. Final Reason: {queued_item.reason_for_queueing}")
            # Notify admin about the persistent failure
            if self.telegram_client and self.settings.admin_id:
                ctx_data = queued_item.message_context_json or {}
                admin_msg = (
                    f"🚫 Max Auto-Retries Reached 🚫\nItem ID: {queued_item.id}\nUser: {ctx_data.get('user_id', 'N/A')}\nChat: {ctx_data.get('chat_id', 'N/A')}\n"
                    f"Reason: {queued_item.reason_for_queueing}\nStatus: pending_admin_action.\nUse /list_queued_checks, /reprocess_check {queued_item.id}, or /discard_check {queued_item.id}.")
                try:
                    await send_message_to_chat(self.telegram_client, self.settings.admin_id, admin_msg)
                except Exception as e_notify:
                    logger.error(
                        f"Failed to send admin notification about max retries for item {queued_item.id}: {e_notify}",
                        exc_info=True)
        return False  # Indicates item was not successfully resolved and removed

    async def process_llm_queue_batch(self, session: AsyncSession, action_service: ActionService):  # Pass ActionService
        """Processes a batch of pending or failed_reprocessing LLM checks."""
        # TODO: Consider adding a delay before retrying "failed_reprocessing_attempt" items using last_attempted_at (Original TODO)
        stmt = (
            select(QueuedLLMCheck.id)
            .where(QueuedLLMCheck.status.in_(
                ["pending", "failed_reprocessing_attempt"]))  # Include items that failed last try
            .order_by(QueuedLLMCheck.retry_count.asc(),
                      QueuedLLMCheck.queued_at.asc())  # Prioritize items with fewer retries
            .limit(self.settings.queue.batch_size)  # Use configured batch size
        )
        item_ids = (await session.execute(stmt)).scalars().all()

        if not item_ids:
            logger.debug("LLM check queue (pending/failed_reprocessing_attempt) is empty for automatic processing.")
            return 0

        logger.info(f"Processing {len(item_ids)} items from LLM check queue.")
        successful_reprocessing_count = 0
        for item_id in item_ids:
            # Use a nested session or careful transaction management if reprocess_queued_item could fail partially
            # For now, assume reprocess_queued_item handles its transactionality or relies on the outer session.
            if await self.reprocess_queued_item(session, item_id, action_service):  # Pass ActionService
                successful_reprocessing_count += 1
            # Optional: Add a small delay between processing items in a batch
            # await asyncio.sleep(0.5)

        # Commits are handled by the get_db_session context manager in __main__.py loop
        logger.info(
            f"LLM queue batch processing finished. Successfully resolved: {successful_reprocessing_count}/{len(item_ids)} items.")
        return successful_reprocessing_count  # Return count of successfully resolved items
