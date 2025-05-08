import logging

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, SecretStr, computed_field

# Load .env file for local development - allows settings to be read from a file
load_dotenv()

logger = logging.getLogger(__name__)

class QueueSettings(BaseModel):
    """Configuration specific to the LLM check queue processing."""
    processing_interval_seconds: int = Field(
        5 * 60,  # Default to 5 minutes
        validation_alias="QUEUE_PROCESSING_INTERVAL_SECONDS",
        description="Interval in seconds for the background task to process the LLM check queue."
    )
    batch_size: int = Field(
        5, # Default batch size
        validation_alias="QUEUE_BATCH_SIZE",
        description="Number of queued items to process in one batch run."
    )
    max_automatic_retries: int = Field(
        2, # Default max automatic retries
        validation_alias="QUEUE_MAX_AUTOMATIC_RETRIES",
        description="Maximum number of automatic retries for a queued item before requiring admin action (status becomes 'pending_admin_action')."
    )

class Settings(BaseModel):
    """Main application settings, loaded from environment variables."""
    # Telegram API Credentials (required)
    api_id: int = Field(..., validation_alias="API_ID")
    api_hash: str = Field(..., validation_alias="API_HASH")
    bot_token: SecretStr = Field(..., validation_alias="BOT_TOKEN")

    # Bot Administration
    admin_id: int = Field(..., validation_alias="ADMIN_ID") # Super admin User ID

    # Database Configuration
    db_url: str = Field("sqlite+aiosqlite:///./staring_misaka.db", validation_alias="DB_URL") # Database connection URL

    # LLM Provider API Keys (at least one recommended)
    anthropic_api_key: SecretStr | None = Field(None, validation_alias="ANTHROPIC_API_KEY")
    openai_api_key: SecretStr | None = Field(None, validation_alias="OPENAI_API_KEY")
    # TODO: Add fields for other potential LLM provider API keys (e.g., Google Gemini, Cohere)

    # Session and Operational Settings
    bot_session_path: str = Field("staring_misaka_bot.session", validation_alias="BOT_SESSION_PATH") # Path for Telethon session file
    prometheus_port: int = Field(8000, validation_alias="PROMETHEUS_PORT") # Port for Prometheus metrics endpoint
    log_level: str = Field("INFO", validation_alias="LOG_LEVEL") # Logging level (DEBUG, INFO, WARNING, ERROR)
    default_ban_message_deletion_limit: int = Field(
        10,
        validation_alias="DEFAULT_BAN_MESSAGE_DELETION_LIMIT",
        description="Default number of recent messages to delete when banning a user (can be overridden per group)."
    )

    # Nested Queue Settings
    queue: QueueSettings = Field(default_factory=QueueSettings)

    model_config = ConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True # Enables use of validation_alias
    )

    @computed_field
    @property
    def db_path_for_sync_engine(self) -> str:
        """Helper property for tools needing a sync SQLite path (like Alembic)."""
        if self.db_url.startswith("sqlite+aiosqlite:///"):
            return self.db_url.replace("sqlite+aiosqlite:///", "sqlite:///")
        elif self.db_url.startswith("sqlite:///"):
            return self.db_url
        logger.warning("DB_URL is not SQLite, sync path helper might not be accurate.")
        return self.db_url # For other DBs, path might be the same or different

def load_config() -> Settings:
    """Loads configuration from environment variables using Pydantic."""
    try:
        return Settings.model_validate({}) # Use model_validate with empty dict to trigger env var loading
    except Exception as e:
        logger.error(f"Configuration loading error: {e}")
        raise
