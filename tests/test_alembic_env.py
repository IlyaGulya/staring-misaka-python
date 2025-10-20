"""Tests for alembic/env.py module."""

import importlib
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# Add alembic directory to the path so we can import from env.py
sys.path.insert(0, str(Path(__file__).parent.parent / "alembic"))


def _import_env_module():
    """Import the env module with proper mocking of alembic context."""
    # Create a mock config that skips fileConfig by having config_file_name = None
    mock_config = MagicMock()
    mock_config.config_file_name = None

    # Mock engine and connection for migration execution
    mock_engine = MagicMock()
    mock_connection = MagicMock()
    mock_connection.__enter__ = MagicMock(return_value=mock_connection)
    mock_connection.__exit__ = MagicMock(return_value=False)
    mock_engine.connect.return_value = mock_connection

    # Mock the entire alembic context to prevent migration execution
    with (
        patch("alembic.context.config", mock_config, create=True),
        patch("alembic.context.is_offline_mode", return_value=False, create=True),
        patch("alembic.context.configure", create=True),
        patch("alembic.context.begin_transaction", create=True),
        patch("alembic.context.run_migrations", create=True),
        patch("sqlalchemy.engine_from_config", return_value=mock_engine),
    ):
        # Remove from sys.modules if already loaded to force reimport
        if "env" in sys.modules:
            del sys.modules["env"]
        import env
        return env


class TestGetDbPathForMigrations:
    """Tests for get_db_path_for_migrations function."""

    def test_returns_db_path_from_environment_variable(self):
        """Test that DB_PATH environment variable takes priority."""
        with patch.dict(os.environ, {"DB_PATH": "/production/database.db"}, clear=False):
            env = _import_env_module()
            result = env.get_db_path_for_migrations()
            assert result == "/production/database.db"

    def test_returns_db_path_from_env_file_when_db_path_not_set(self, tmp_path):
        """Test that function loads from .env file when DB_PATH env var is not set."""
        # Create a mock config object
        mock_config = MagicMock()
        mock_config.db_path = "/from/env/file/database.db"

        # Remove DB_PATH if it exists
        env_vars = dict(os.environ)
        env_vars.pop("DB_PATH", None)
        env_vars.pop("ENVIRONMENT", None)

        with (
            patch.dict(os.environ, env_vars, clear=True),
            patch("os.path.exists", return_value=True),
            patch("config.load_database_config", return_value=mock_config) as mock_load,
        ):
            env = _import_env_module()
            result = env.get_db_path_for_migrations()
            assert result == "/from/env/file/database.db"
            mock_load.assert_called_once()

    def test_skips_env_file_in_production_environment(self):
        """Test that .env file is not loaded when ENVIRONMENT is production."""
        env_vars = dict(os.environ)
        env_vars["ENVIRONMENT"] = "production"
        env_vars.pop("DB_PATH", None)

        with patch.dict(os.environ, env_vars, clear=True):
            env = _import_env_module()
            with pytest.raises(
                RuntimeError,
                match="No database path configured. Either set DB_PATH environment variable",
            ):
                env.get_db_path_for_migrations()

    def test_raises_error_when_env_file_missing_and_no_db_path(self):
        """Test that RuntimeError is raised when .env file doesn't exist and DB_PATH is not set."""
        # Remove DB_PATH and ENVIRONMENT
        env_vars = dict(os.environ)
        env_vars.pop("DB_PATH", None)
        env_vars.pop("ENVIRONMENT", None)

        with (
            patch.dict(os.environ, env_vars, clear=True),
            patch("os.path.exists", return_value=False),
        ):
            env = _import_env_module()
            with pytest.raises(
                RuntimeError,
                match="No database path configured. Either set DB_PATH environment variable",
            ):
                env.get_db_path_for_migrations()

    def test_raises_error_when_config_loading_fails(self):
        """Test that RuntimeError with descriptive message is raised when config loading fails."""
        # Remove DB_PATH and ENVIRONMENT
        env_vars = dict(os.environ)
        env_vars.pop("DB_PATH", None)
        env_vars.pop("ENVIRONMENT", None)

        with (
            patch.dict(os.environ, env_vars, clear=True),
            patch("os.path.exists", return_value=True),
            patch(
                "config.load_database_config",
                side_effect=ValueError("Invalid config format"),
            ),
        ):
            env = _import_env_module()
            with pytest.raises(
                RuntimeError, match="Failed to load database config from .env file"
            ):
                env.get_db_path_for_migrations()

    def test_db_path_priority_over_env_file(self):
        """Test that DB_PATH environment variable takes priority over .env file."""
        mock_config = MagicMock()
        mock_config.db_path = "/from/env/file/database.db"

        with (
            patch.dict(
                os.environ, {"DB_PATH": "/priority/database.db"}, clear=False
            ),
            patch("os.path.exists", return_value=True),
            patch("config.load_database_config", return_value=mock_config) as mock_load,
        ):
            env = _import_env_module()
            result = env.get_db_path_for_migrations()
            assert result == "/priority/database.db"
            # load_database_config should NOT be called when DB_PATH is set
            mock_load.assert_not_called()

    def test_empty_db_path_env_var_treated_as_not_set(self):
        """Test that empty DB_PATH environment variable is treated as not set."""
        mock_config = MagicMock()
        mock_config.db_path = "/from/env/file/database.db"

        env_vars = dict(os.environ)
        env_vars["DB_PATH"] = ""  # Empty string
        env_vars.pop("ENVIRONMENT", None)

        with (
            patch.dict(os.environ, env_vars, clear=True),
            patch("os.path.exists", return_value=True),
            patch("config.load_database_config", return_value=mock_config) as mock_load,
        ):
            env = _import_env_module()
            result = env.get_db_path_for_migrations()
            # Empty string is falsy, so it should fall back to loading from .env
            assert result == "/from/env/file/database.db"
            mock_load.assert_called_once()
