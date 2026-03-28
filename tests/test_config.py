import pytest
from pydantic import ValidationError
import tempfile
import os
from pathlib import Path

from config import Config, SpamConfig, load_spam_config


class TestConfigValidation:
    """Test config.py validators and for_testing() method"""
    
    @pytest.mark.parametrize("api_id,expected_error", [
        (-1, "API_ID must be positive"),
        (0, "API_ID must be positive"),
        (-12345, "API_ID must be positive"),
    ])
    def test_invalid_api_id(self, api_id, expected_error):
        """Test API_ID validation with invalid values"""
        with pytest.raises(ValidationError) as exc_info:
            Config.for_testing(api_id=api_id)
        
        assert expected_error in str(exc_info.value)
    
    @pytest.mark.parametrize("api_hash,expected_error", [
        ("", "API_HASH appears to be too short"),
        ("short", "API_HASH appears to be too short"),
        ("123456789", "API_HASH appears to be too short"),  # 9 characters
    ])
    def test_invalid_api_hash(self, api_hash, expected_error):
        """Test API_HASH validation with invalid values"""
        with pytest.raises(ValidationError) as exc_info:
            Config.for_testing(api_hash=api_hash)
        
        assert expected_error in str(exc_info.value)
    
    @pytest.mark.parametrize("bot_token,expected_error", [
        ("", "BOT_TOKEN appears to be invalid format"),
        ("invalid_token", "BOT_TOKEN appears to be invalid format"),
        ("123456789", "BOT_TOKEN appears to be invalid format"),  # No colon
        ("123:short", "BOT_TOKEN appears to be invalid format"),  # Too short
    ])
    def test_invalid_bot_token(self, bot_token, expected_error):
        """Test BOT_TOKEN validation with invalid values"""
        with pytest.raises(ValidationError) as exc_info:
            Config.for_testing(bot_token=bot_token)
        
        assert expected_error in str(exc_info.value)
    
    @pytest.mark.parametrize("admin_id,expected_error", [
        (-1, "ADMIN_ID must be positive"),
        (0, "ADMIN_ID must be positive"),
        (-99999, "ADMIN_ID must be positive"),
    ])
    def test_invalid_admin_id(self, admin_id, expected_error):
        """Test ADMIN_ID validation with invalid values"""
        with pytest.raises(ValidationError) as exc_info:
            Config.for_testing(admin_id=admin_id)
        
        assert expected_error in str(exc_info.value)
    
    @pytest.mark.parametrize("tracking_chat_ids,expected_error", [
        ("", "TRACKING_CHAT_IDS must contain at least one valid chat ID"),
        ("   ", "TRACKING_CHAT_IDS must contain at least one valid chat ID"),
        (",,,", "TRACKING_CHAT_IDS must contain at least one valid chat ID"),
        ([], "TRACKING_CHAT_IDS must contain at least one chat ID"),
        ([0], "TRACKING_CHAT_IDS cannot contain zero"),
        ([123, 0, 456], "TRACKING_CHAT_IDS cannot contain zero"),
    ])
    def test_invalid_tracking_chat_ids(self, tracking_chat_ids, expected_error):
        """Test TRACKING_CHAT_IDS validation with invalid values"""
        with pytest.raises(ValidationError) as exc_info:
            Config.for_testing(TRACKING_CHAT_IDS=tracking_chat_ids)
        
        assert expected_error in str(exc_info.value)
    
    @pytest.mark.parametrize("anthropic_api_key,expected_error", [
        ("", "ANTHROPIC_API_KEY appears to be too short"),
        ("short", "ANTHROPIC_API_KEY appears to be too short"),
        ("123456789", "ANTHROPIC_API_KEY appears to be too short"),  # 9 characters
    ])
    def test_invalid_anthropic_api_key(self, anthropic_api_key, expected_error):
        """Test ANTHROPIC_API_KEY validation with invalid values"""
        with pytest.raises(ValidationError) as exc_info:
            Config.for_testing(anthropic_api_key=anthropic_api_key)
        
        assert expected_error in str(exc_info.value)
    
    def test_directory_creation_failure(self):
        """Test directory creation failure in model_post_init"""
        # Use a path that cannot be created (parent is a file)
        with tempfile.NamedTemporaryFile() as temp_file:
            invalid_path = f"{temp_file.name}/subdir/file.db"
            
            with pytest.raises(ValidationError) as exc_info:
                Config.for_testing(db_path=invalid_path)
            
            assert "Cannot create directory for DB_PATH" in str(exc_info.value)
    
    def test_for_testing_valid_defaults(self):
        """Test that for_testing() creates valid config with defaults"""
        config = Config.for_testing()
        
        assert config.api_id == 12345
        assert config.api_hash == 'test_hash_1234567890'
        assert config.bot_token == '123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890'
        assert config.admin_id == 99999
        assert config.tracking_chat_ids == [67890, 12345]
        assert config.bot_session_path == '/tmp/test_bot.session'
        assert config.userbot_session_path == '/tmp/test_userbot.session'
        assert config.db_path == '/tmp/test.db'
        assert config.anthropic_api_key == 'test_key_1234567890'
    
    def test_for_testing_with_overrides(self):
        """Test that for_testing() applies overrides correctly"""
        overrides = {
            'api_id': 54321,
            'admin_id': 11111,
            'TRACKING_CHAT_IDS': [-123456, -789012],
        }
        
        config = Config.for_testing(**overrides)
        
        assert config.api_id == 54321
        assert config.admin_id == 11111
        assert config.tracking_chat_ids == [-123456, -789012]
        # Other values should remain defaults
        assert config.api_hash == 'test_hash_1234567890'
        assert config.bot_token == '123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890'
    
    def test_tracking_chat_ids_string_parsing(self):
        """Test comma-separated string parsing for TRACKING_CHAT_IDS"""
        test_cases = [
            ("123,456,789", [123, 456, 789]),
            (" 123 , 456 , 789 ", [123, 456, 789]),  # With spaces
            ("123", [123]),  # Single value
            ("-123,-456", [-123, -456]),  # Negative values
        ]
        
        for input_str, expected_list in test_cases:
            config = Config.for_testing(TRACKING_CHAT_IDS=input_str)
            assert config.tracking_chat_ids == expected_list
    
    def test_valid_minimum_lengths(self):
        """Test that minimum valid lengths are accepted"""
        config = Config.for_testing(
            api_hash='1234567890',  # Exactly 10 characters
            anthropic_api_key='1234567890',  # Exactly 10 characters
            bot_token='123456789:1234567890123456789012345678901234567890'  # 51 chars with colon
        )
        
        assert len(config.api_hash) == 10
        assert len(config.anthropic_api_key) == 10
        assert ':' in config.bot_token
        assert len(config.bot_token) > 40


class TestSpamConfig:
    """Test YAML spam config loading and per-chat overrides."""

    def test_load_spam_config_defaults(self, tmp_path):
        """Test loading a minimal config with only system_prompt."""
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text(
            "system_prompt: You are a spam classifier.\n"
        )

        config = load_spam_config(str(yaml_file))

        assert config.model == "claude-haiku-4-5-20251001"
        assert config.include_reason_in_ban is False
        assert config.system_prompt == "You are a spam classifier."
        assert config.chats == {}

    def test_load_spam_config_with_per_chat(self, tmp_path):
        """Test loading config with per-chat overrides."""
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text(
            "model: claude-sonnet-4-6\n"
            "include_reason_in_ban: true\n"
            "system_prompt: Default spam classifier prompt.\n"
            "chats:\n"
            "  -1001234567890:\n"
            "    extra_instructions: This group is about Python.\n"
        )

        config = load_spam_config(str(yaml_file))

        assert config.model == "claude-sonnet-4-6"
        assert config.include_reason_in_ban is True
        assert -1001234567890 in config.chats
        assert config.chats[-1001234567890].extra_instructions == "This group is about Python."

    def test_load_spam_config_per_chat_full_override(self, tmp_path):
        """Test per-chat config with full system_prompt override."""
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text(
            "system_prompt: Default prompt.\n"
            "chats:\n"
            "  111:\n"
            "    system_prompt: Completely custom prompt for chat 111.\n"
        )

        config = load_spam_config(str(yaml_file))

        assert config.get_system_prompt(111) == "Completely custom prompt for chat 111."
        assert config.get_system_prompt(999) == "Default prompt."

    def test_get_system_prompt_inheritance(self):
        """Test get_system_prompt with append, override, and default."""
        from config import ChatSpamConfig
        config = SpamConfig(
            system_prompt="Base prompt.",
            chats={
                1: ChatSpamConfig(extra_instructions="Extra for chat 1."),
                2: ChatSpamConfig(system_prompt="Full override for chat 2."),
                3: ChatSpamConfig(),  # inherits default
            }
        )

        # Appends extra_instructions
        assert config.get_system_prompt(1) == "Base prompt.\n\nExtra for chat 1."
        # Full override
        assert config.get_system_prompt(2) == "Full override for chat 2."
        # Inherits default
        assert config.get_system_prompt(3) == "Base prompt."
        # Unknown chat — default
        assert config.get_system_prompt(999) == "Base prompt."
        # None chat_id — default
        assert config.get_system_prompt(None) == "Base prompt."