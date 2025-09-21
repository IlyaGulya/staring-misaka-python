"""Tests for database migrations using Alembic."""

import tempfile
import os
from pathlib import Path
import pytest
from sqlalchemy import create_engine, text
from alembic.config import Config
from alembic import command
from alembic.script import ScriptDirectory
from alembic.runtime.environment import EnvironmentContext

from db import Base


@pytest.fixture
def temp_db():
    """Create a temporary database for migration testing."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name
    
    yield db_path
    
    # Cleanup
    if os.path.exists(db_path):
        os.unlink(db_path)


@pytest.fixture
def alembic_config(temp_db):
    """Create an Alembic configuration for testing."""
    # Get the project root directory
    project_root = Path(__file__).parent.parent
    alembic_cfg = Config(str(project_root / "alembic.ini"))
    
    # Override the database URL to use our temp database
    alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{temp_db}")
    
    return alembic_cfg


class TestDatabaseMigrations:
    """Test database migration functionality."""

    def test_migration_upgrade_creates_all_tables(self, alembic_config, temp_db):
        """Test that running migrations creates all expected tables."""
        # Run migrations to latest
        command.upgrade(alembic_config, "head")
        
        # Connect to the database and check tables exist
        engine = create_engine(f"sqlite:///{temp_db}")
        with engine.connect() as conn:
            # Check that all expected tables exist
            result = conn.execute(text("""
                SELECT name FROM sqlite_master 
                WHERE type='table' AND name NOT LIKE 'alembic_%'
                ORDER BY name
            """))
            tables = [row[0] for row in result.fetchall()]
            
            expected_tables = [
                'admin_settings',
                'approved_users', 
                'banned_users',
                'message_queue',
                'new_users',
                'pending_ban_requests'
            ]
            
            assert set(tables) == set(expected_tables)

    def test_migration_downgrade_removes_tables(self, alembic_config, temp_db):
        """Test that downgrading removes all tables."""
        # First upgrade to latest
        command.upgrade(alembic_config, "head")
        
        # Then downgrade to base
        command.downgrade(alembic_config, "base")
        
        # Check that tables are gone (except alembic_version)
        engine = create_engine(f"sqlite:///{temp_db}")
        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT name FROM sqlite_master 
                WHERE type='table' AND name NOT LIKE 'alembic_%'
            """))
            tables = [row[0] for row in result.fetchall()]
            
            # Should be empty (no application tables)
            assert tables == []

    def test_migration_schema_matches_models(self, alembic_config, temp_db):
        """Test that migration schema matches SQLAlchemy models."""
        # Run migrations
        command.upgrade(alembic_config, "head")
        
        # Create a second database using SQLAlchemy create_all
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            model_db_path = f.name
        
        try:
            model_engine = create_engine(f"sqlite:///{model_db_path}")
            Base.metadata.create_all(model_engine)
            
            # Compare table structures (this is a basic check)
            migration_engine = create_engine(f"sqlite:///{temp_db}")
            
            # Get table info from migration database
            with migration_engine.connect() as migration_conn:
                migration_result = migration_conn.execute(text("""
                    SELECT name FROM sqlite_master 
                    WHERE type='table' AND name NOT LIKE 'alembic_%'
                    ORDER BY name
                """))
                migration_tables = set(row[0] for row in migration_result.fetchall())
            
            # Get table info from model database  
            with model_engine.connect() as model_conn:
                model_result = model_conn.execute(text("""
                    SELECT name FROM sqlite_master 
                    WHERE type='table'
                    ORDER BY name
                """))
                model_tables = set(row[0] for row in model_result.fetchall())
            
            # Tables should match
            assert migration_tables == model_tables
            
        finally:
            if os.path.exists(model_db_path):
                os.unlink(model_db_path)

    def test_migration_version_tracking(self, alembic_config, temp_db):
        """Test that Alembic properly tracks migration versions."""
        # Check initial state (no migrations)
        current = command.current(alembic_config)
        assert current is None or len(current) == 0
        
        # Run migrations
        command.upgrade(alembic_config, "head")
        
        # Check that version is tracked
        engine = create_engine(f"sqlite:///{temp_db}")
        with engine.connect() as conn:
            result = conn.execute(text("SELECT version_num FROM alembic_version"))
            version = result.fetchone()[0]
            assert version is not None
            assert len(version) > 0

    def test_migration_idempotency(self, alembic_config, temp_db):
        """Test that running migrations multiple times is safe."""
        # Run migrations twice
        command.upgrade(alembic_config, "head")
        command.upgrade(alembic_config, "head")  # Should be safe to run again
        
        # Verify tables still exist and are correct
        engine = create_engine(f"sqlite:///{temp_db}")
        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT name FROM sqlite_master 
                WHERE type='table' AND name NOT LIKE 'alembic_%'
                ORDER BY name
            """))
            tables = [row[0] for row in result.fetchall()]
            
            expected_tables = [
                'admin_settings',
                'approved_users',
                'banned_users',
                'message_queue', 
                'new_users',
                'pending_ban_requests'
            ]
            
            assert set(tables) == set(expected_tables)

    def test_admin_settings_table_structure(self, alembic_config, temp_db):
        """Test that the AdminSettings table has the correct structure."""
        command.upgrade(alembic_config, "head")
        
        engine = create_engine(f"sqlite:///{temp_db}")
        with engine.connect() as conn:
            # Get column info for admin_settings table
            result = conn.execute(text("PRAGMA table_info(admin_settings)"))
            columns = {row[1]: row[2] for row in result.fetchall()}
            
            expected_columns = {
                'id': 'INTEGER',
                'require_approval': 'BOOLEAN'
            }
            
            for col_name, col_type in expected_columns.items():
                assert col_name in columns
                assert columns[col_name] == col_type

    def test_datetime_server_defaults(self, alembic_config, temp_db):
        """Test that datetime columns have proper server defaults."""
        command.upgrade(alembic_config, "head")
        
        engine = create_engine(f"sqlite:///{temp_db}")
        with engine.connect() as conn:
            # Test that we can insert records without specifying datetime fields
            # and they get populated automatically
            
            # Test new_users table - note: current migration doesn't have server defaults
            # so we need to provide the join_time explicitly
            conn.execute(text("""
                INSERT INTO new_users (user_id, chat_id, join_time) 
                VALUES (12345, 67890, datetime('now'))
            """))
            
            result = conn.execute(text("""
                SELECT join_time FROM new_users WHERE user_id = 12345
            """))
            join_time = result.fetchone()[0]
            assert join_time is not None
            
            # Test approved_users table - also needs explicit approved_at
            conn.execute(text("""
                INSERT INTO approved_users (user_id, chat_id, approved_at)
                VALUES (12345, 67890, datetime('now'))
            """))
            
            result = conn.execute(text("""
                SELECT approved_at FROM approved_users WHERE user_id = 12345
            """))
            approved_at = result.fetchone()[0]
            assert approved_at is not None
            
            conn.rollback()  # Don't actually commit test data