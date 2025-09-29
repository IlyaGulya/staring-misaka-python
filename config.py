import os
from typing import List, Dict
from pathlib import Path
from pydantic import Field, field_validator, ConfigDict
from pydantic_settings import BaseSettings


class BaseConfig(BaseSettings):
    """Base configuration class with common settings"""
    
    model_config = ConfigDict(
        env_file=".env" if os.environ.get("ENVIRONMENT") != "production" and Path(".env").exists() else None,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


class DatabaseConfig(BaseConfig):
    """Database-only configuration for migrations and database operations"""
    db_path: str


class TelegramConfig(BaseConfig):
    """Telegram API and bot configuration"""
    api_id: int
    api_hash: str
    bot_token: str
    admin_id: int
    tracking_chat_ids: List[int] = Field(alias="TRACKING_CHAT_IDS")
    log_channel_map: Dict[int, int] = Field(default_factory=dict, alias="LOG_CHANNEL_MAP")
    default_purge_count: int = Field(default=25, alias="DEFAULT_PURGE_COUNT")
    
    @field_validator('api_id')
    @classmethod
    def validate_api_id(cls, v):
        if v <= 0:
            raise ValueError(f"API_ID must be positive, got: {v}")
        return v
    
    @field_validator('api_hash')
    @classmethod
    def validate_api_hash(cls, v):
        if len(v) < 10:
            raise ValueError(f"API_HASH appears to be too short: {len(v)} characters")
        return v
    
    @field_validator('bot_token')
    @classmethod
    def validate_bot_token(cls, v):
        if ':' not in v or len(v) < 40:
            raise ValueError("BOT_TOKEN appears to be invalid format")
        return v
    
    @field_validator('admin_id')
    @classmethod
    def validate_admin_id(cls, v):
        if v <= 0:
            raise ValueError(f"ADMIN_ID must be positive, got: {v}")
        return v
    
    @field_validator('tracking_chat_ids', mode='before')
    @classmethod
    def parse_chat_ids(cls, v):
        """Parse comma-separated chat IDs from string or return as-is if already a list"""
        if isinstance(v, str):
            values = [int(x.strip()) for x in v.split(',') if x.strip()]
            if not values:
                raise ValueError("TRACKING_CHAT_IDS must contain at least one valid chat ID")
            return values
        return v
    
    @field_validator('tracking_chat_ids')
    @classmethod
    def validate_tracking_chat_ids(cls, v):
        if not v:
            raise ValueError("TRACKING_CHAT_IDS must contain at least one chat ID")
        for chat_id in v:
            if chat_id == 0:
                raise ValueError("TRACKING_CHAT_IDS cannot contain zero")
        return v

    @field_validator('log_channel_map', mode='before')
    @classmethod
    def parse_log_channel_map(cls, v):
        """
        Accept formats:
          - dict: {group_id: log_chat_id}
          - string: "-100111:-100222,-100333:-100444"
        """
        if v is None or v == "":
            return {}
        if isinstance(v, dict):
            # ensure ints
            return {int(k): int(vv) for k, vv in v.items()}
        if isinstance(v, str):
            pairs = [p.strip() for p in v.split(",") if p.strip()]
            out = {}
            for p in pairs:
                if ":" not in p:
                    raise ValueError(f"Invalid LOG_CHANNEL_MAP pair: {p}")
                k, vv = p.split(":", 1)
                out[int(k.strip())] = int(vv.strip())
            return out
        raise ValueError("LOG_CHANNEL_MAP must be a dict or 'group:log,group2:log2' string")

    @field_validator('default_purge_count')
    @classmethod
    def validate_default_purge_count(cls, v):
        if v <= 0:
            raise ValueError("DEFAULT_PURGE_COUNT must be positive")
        return v


class SessionConfig(BaseConfig):
    """Session file paths configuration"""
    bot_session_path: str
    userbot_session_path: str


class LLMConfig(BaseConfig):
    """LLM API configuration"""
    anthropic_api_key: str
    
    @field_validator('anthropic_api_key')
    @classmethod
    def validate_anthropic_api_key(cls, v):
        if len(v) < 10:
            raise ValueError(f"ANTHROPIC_API_KEY appears to be too short: {len(v)} characters")
        return v


class Config(BaseConfig):
    """Complete application configuration that combines all sub-configs"""
    
    # Telegram API configuration
    api_id: int
    api_hash: str
    
    # Bot configuration
    bot_token: str
    admin_id: int
    tracking_chat_ids: List[int] = Field(alias="TRACKING_CHAT_IDS")
    log_channel_map: Dict[int, int] = Field(default_factory=dict, alias="LOG_CHANNEL_MAP")
    default_purge_count: int = Field(default=25, alias="DEFAULT_PURGE_COUNT")
    
    # Session file paths
    bot_session_path: str
    userbot_session_path: str
    
    # Database
    db_path: str
    
    # LLM API
    anthropic_api_key: str
    
    @field_validator('tracking_chat_ids', mode='before')
    @classmethod
    def parse_chat_ids(cls, v):
        """Parse comma-separated chat IDs from string or return as-is if already a list"""
        if isinstance(v, str):
            values = [int(x.strip()) for x in v.split(',') if x.strip()]
            if not values:
                raise ValueError("TRACKING_CHAT_IDS must contain at least one valid chat ID")
            return values
        return v
    
    @field_validator('api_id')
    @classmethod
    def validate_api_id(cls, v):
        if v <= 0:
            raise ValueError(f"API_ID must be positive, got: {v}")
        return v
    
    @field_validator('api_hash')
    @classmethod
    def validate_api_hash(cls, v):
        if len(v) < 10:
            raise ValueError(f"API_HASH appears to be too short: {len(v)} characters")
        return v
    
    @field_validator('bot_token')
    @classmethod
    def validate_bot_token(cls, v):
        if ':' not in v or len(v) < 40:
            raise ValueError("BOT_TOKEN appears to be invalid format")
        return v
    
    @field_validator('admin_id')
    @classmethod
    def validate_admin_id(cls, v):
        if v <= 0:
            raise ValueError(f"ADMIN_ID must be positive, got: {v}")
        return v
    
    @field_validator('tracking_chat_ids')
    @classmethod
    def validate_tracking_chat_ids(cls, v):
        if not v:
            raise ValueError("TRACKING_CHAT_IDS must contain at least one chat ID")
        for chat_id in v:
            if chat_id == 0:
                raise ValueError("TRACKING_CHAT_IDS cannot contain zero")
        return v

    @field_validator('log_channel_map', mode='before')
    @classmethod
    def parse_log_channel_map(cls, v):
        if v is None or v == "":
            return {}
        if isinstance(v, dict):
            return {int(k): int(vv) for k, vv in v.items()}
        if isinstance(v, str):
            pairs = [p.strip() for p in v.split(",") if p.strip()]
            out = {}
            for p in pairs:
                if ":" not in p:
                    raise ValueError(f"Invalid LOG_CHANNEL_MAP pair: {p}")
                k, vv = p.split(":", 1)
                out[int(k.strip())] = int(vv.strip())
            return out
        raise ValueError("LOG_CHANNEL_MAP must be a dict or 'group:log,group2:log2' string")

    @field_validator('default_purge_count')
    @classmethod
    def validate_default_purge_count(cls, v):
        if v <= 0:
            raise ValueError("DEFAULT_PURGE_COUNT must be positive")
        return v

    @field_validator('anthropic_api_key')
    @classmethod
    def validate_anthropic_api_key(cls, v):
        if len(v) < 10:
            raise ValueError(f"ANTHROPIC_API_KEY appears to be too short: {len(v)} characters")
        return v
    
    def model_post_init(self, __context) -> None:
        """Create necessary directories after model initialization"""
        for path_attr in ['db_path', 'bot_session_path', 'userbot_session_path']:
            path_value = getattr(self, path_attr)
            try:
                Path(path_value).parent.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                raise ValueError(f"Cannot create directory for {path_attr.upper()} {path_value}: {e}")
    
    @classmethod
    def for_testing(cls, **overrides) -> 'Config':
        """Create a test configuration with sensible defaults"""
        defaults = {
            'api_id': 12345,
            'api_hash': 'test_hash_1234567890',
            'bot_token': '123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890',
            'admin_id': 99999,
            'tracking_chat_ids': [67890, 12345],
            'bot_session_path': '/tmp/test_bot.session',
            'userbot_session_path': '/tmp/test_userbot.session',
            'db_path': '/tmp/test.db',
            'anthropic_api_key': 'test_key_1234567890',
            'log_channel_map': {},
            'default_purge_count': 25,
        }
        
        # Apply overrides
        for key, value in overrides.items():
            # Handle alias mapping
            if key == 'TRACKING_CHAT_IDS':
                key = 'tracking_chat_ids'
            defaults[key] = value
        
        # Create a simple BaseModel class to avoid environment loading
        from pydantic import BaseModel, ValidationError
        
        class TestConfig(BaseModel):
            api_id: int
            api_hash: str
            bot_token: str
            admin_id: int
            tracking_chat_ids: List[int]
            bot_session_path: str
            userbot_session_path: str
            db_path: str
            anthropic_api_key: str
            log_channel_map: Dict[int, int] = {}
            default_purge_count: int = 25
            
            # Copy all validators
            @field_validator('api_id')
            @classmethod
            def validate_api_id(cls, v):
                if v <= 0:
                    raise ValueError(f"API_ID must be positive")
                return v
            
            @field_validator('api_hash')
            @classmethod
            def validate_api_hash(cls, v):
                if len(v) < 10:
                    raise ValueError(f"API_HASH appears to be too short")
                return v
            
            @field_validator('bot_token')
            @classmethod
            def validate_bot_token(cls, v):
                if ':' not in v or len(v) < 40:
                    raise ValueError("BOT_TOKEN appears to be invalid format")
                return v
            
            @field_validator('admin_id')
            @classmethod
            def validate_admin_id(cls, v):
                if v <= 0:
                    raise ValueError(f"ADMIN_ID must be positive")
                return v
            
            @field_validator('tracking_chat_ids', mode='before')
            @classmethod
            def parse_chat_ids(cls, v):
                """Parse comma-separated chat IDs from string or return as-is if already a list"""
                if isinstance(v, str):
                    values = [int(x.strip()) for x in v.split(',') if x.strip()]
                    if not values:
                        raise ValueError("TRACKING_CHAT_IDS must contain at least one valid chat ID")
                    return values
                return v
            
            @field_validator('tracking_chat_ids')
            @classmethod
            def validate_tracking_chat_ids(cls, v):
                if not v:
                    raise ValueError("TRACKING_CHAT_IDS must contain at least one chat ID")
                for chat_id in v:
                    if chat_id == 0:
                        raise ValueError("TRACKING_CHAT_IDS cannot contain zero")
                return v
            
            @field_validator('anthropic_api_key')
            @classmethod
            def validate_anthropic_api_key(cls, v):
                if len(v) < 10:
                    raise ValueError(f"ANTHROPIC_API_KEY appears to be too short")
                return v
            
            def model_post_init(self, __context) -> None:
                """Create necessary directories after model initialization"""
                for path_attr in ['db_path', 'bot_session_path', 'userbot_session_path']:
                    path_value = getattr(self, path_attr)
                    try:
                        Path(path_value).parent.mkdir(parents=True, exist_ok=True)
                    except Exception as e:
                        raise ValueError(f"Cannot create directory for {path_attr.upper()} {path_value}: {e}")
        
        return TestConfig(**defaults)


def load_config() -> Config:
    """Load and validate complete application configuration from environment variables"""
    return Config()

def eprint(*args, **kwargs):
    import sys
    print(*args, file=sys.stderr, **kwargs)

def load_database_config() -> DatabaseConfig:
    """Load only database configuration - useful for migrations"""

    eprint(f"[CONFIG DEBUG] Loading database config...")
    eprint(f"[CONFIG DEBUG] ENVIRONMENT env var: {os.environ.get('ENVIRONMENT', 'not set')}")
    env_file_path = ".env"
    env_exists = Path(env_file_path).exists()
    eprint(f"[CONFIG DEBUG] .env file exists: {env_exists}")

    if os.environ.get("ENVIRONMENT") != "production" and env_exists:
        eprint(f"[CONFIG DEBUG] Will load from .env file")
    else:
        eprint(f"[CONFIG DEBUG] Will load from environment variables only")
    
    db_config = DatabaseConfig()
    eprint(f"[CONFIG DEBUG] Resolved DB_PATH: {db_config.db_path}")
    return db_config


def load_telegram_config() -> TelegramConfig:
    """Load only Telegram configuration"""
    return TelegramConfig()


def load_session_config() -> SessionConfig:
    """Load only session file configuration"""
    return SessionConfig()


def load_llm_config() -> LLMConfig:
    """Load only LLM API configuration"""
    return LLMConfig()