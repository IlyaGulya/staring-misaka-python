# src/staring_misaka/config.py
import datetime  # For date parsing in pricing config
import logging
from decimal import Decimal  # For pricing config
from typing import List, Optional  # For pricing config

import yaml  # For loading pricing_config.yaml
from pydantic import BaseModel, Field, SecretStr, computed_field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


# --- Pydantic models for pricing_config.yaml ---
class PricingPeriod(BaseModel):
    effective_from_date: datetime.date
    effective_to_date: Optional[datetime.date] = None
    input_price_per_million_tokens: Decimal
    output_price_per_million_tokens: Decimal
    currency: Optional[str] = None

    @model_validator(mode='after')
    def check_dates(cls, values):
        # Accessing fields directly from `values` (which is the model instance here)
        # For Pydantic v2, it's recommended to access fields via self or values.model_fields_set
        # However, direct access to `values.effective_from_date` etc. works in this context.
        from_date = values.effective_from_date
        to_date = values.effective_to_date
        if to_date and from_date > to_date:
            raise ValueError("effective_from_date cannot be after effective_to_date")
        return values


class ModelPricingConfig(BaseModel):
    model_api_identifier: str
    pricing_periods: List[PricingPeriod] = []


class PricingFile(BaseModel):
    default_currency: str = "USD"
    models: List[ModelPricingConfig] = []


class QueueSettings(BaseSettings):  # Changed from pydantic.BaseModel to pydantic_settings.BaseSettings
    """Configuration specific to the LLM check queue processing."""
    processing_interval_seconds: float = Field(
        5 * 60,  # Default to 5 minutes
        validation_alias="QUEUE_PROCESSING_INTERVAL_SECONDS",
        description="Interval in seconds for the background task to process the LLM check queue."
    )
    batch_size: int = Field(
        5,  # Default batch size
        validation_alias="QUEUE_BATCH_SIZE",
        description="Number of queued items to process in one batch run."
    )
    max_automatic_retries: int = Field(
        2,  # Default max automatic retries
        validation_alias="QUEUE_MAX_AUTOMATIC_RETRIES",
        description="Maximum number of automatic retries for a queued item before requiring admin action (status becomes 'pending_admin_action')."
    )
    model_config = SettingsConfigDict(
        extra="ignore",
    )


class Settings(BaseSettings):  # Changed from pydantic.BaseModel to pydantic_settings.BaseSettings
    """Main application settings, loaded from environment variables."""
    # Telegram API Credentials (required)
    api_id: int = Field()
    api_hash: str = Field()
    bot_token: SecretStr = Field()

    # Bot Administration
    admin_id: int = Field()  # Super admin User ID

    # Database Configuration
    db_url: str = Field("sqlite+aiosqlite:///./staring_misaka.db")  # Database connection URL

    # LLM Provider API Keys (at least one recommended)
    anthropic_api_key: SecretStr | None = Field(None)
    openai_api_key: SecretStr | None = Field(None)

    # Session and Operational Settings
    bot_session_path: str = Field("staring_misaka_bot.session",
                                  validation_alias="BOT_SESSION_PATH")  # Path for Telethon session file
    prometheus_port: int = Field(8000)  # Port for Prometheus metrics endpoint
    log_level: str = Field("INFO")
    default_ban_message_deletion_limit: int = Field(
        10,
        validation_alias="DEFAULT_BAN_MESSAGE_DELETION_LIMIT",
        description="Default number of recent messages to delete when banning a user (can be overridden per group)."
    )

    # Gradio Web UI (Optional)
    gradio_username: str | None = Field(None)
    gradio_password: SecretStr | None = Field(None)
    gradio_port: int = Field(7860)

    # Nested Queue Settings
    queue: QueueSettings = Field(default_factory=QueueSettings)

    pricing_config_file_path: str = Field("pricing_config.yaml")
    loaded_pricing_config: Optional[PricingFile] = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode='after')
    def load_pricing_config_from_file(cls,
                                      values_obj):  # In Pydantic v2, the first arg to model_validator `self` is the model instance
        # For Pydantic v2, we access fields via the model instance `values_obj`
        file_path = values_obj.pricing_config_file_path
        try:
            with open(file_path, 'r') as f:
                pricing_data = yaml.safe_load(f)
                if pricing_data:
                    values_obj.loaded_pricing_config = PricingFile(**pricing_data)
                    logger.info(f"Successfully loaded pricing configuration from {file_path}")
                else:
                    logger.warning(f"Pricing configuration file {file_path} is empty. No pricing will be applied.")
                    values_obj.loaded_pricing_config = PricingFile()  # Empty default
        except FileNotFoundError:
            logger.warning(f"Pricing configuration file not found at {file_path}. No pricing will be applied.")
            values_obj.loaded_pricing_config = PricingFile()  # Empty default
        except yaml.YAMLError as e:
            logger.error(f"Error parsing pricing configuration file {file_path}: {e}", exc_info=True)
            values_obj.loaded_pricing_config = PricingFile()  # Empty default
        except Exception as e:  # Catch Pydantic validation errors too
            logger.error(f"Error validating pricing configuration from {file_path}: {e}", exc_info=True)
            values_obj.loaded_pricing_config = PricingFile()
        return values_obj

    @computed_field
    @property
    def db_path_for_sync_engine(self) -> str:
        """Helper property for tools needing a sync SQLite path (like Alembic)."""
        if self.db_url.startswith("sqlite+aiosqlite:///"):
            return self.db_url.replace("sqlite+aiosqlite:///", "sqlite:///")
        elif self.db_url.startswith("sqlite:///"):
            return self.db_url
        logger.warning("DB_URL is not SQLite, sync path helper might not be accurate.")
        return self.db_url


def load_config() -> Settings:
    """Loads configuration from environment variables using Pydantic-Settings."""
    try:
        return Settings()
    except Exception as e:
        logger.error(f"Configuration loading error: {e}", exc_info=True)
        raise
