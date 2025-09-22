import pytest
import tempfile
import os
from pathlib import Path
from sqlalchemy import create_engine, inspect

from config import Config
from db import create_session, AdminSettings, Base


class TestDatabaseBootstrap:
    """Test create_session() bootstrap functionality"""
    
    def test_create_session_creates_default_admin_settings(self):
        """Test that create_session() creates default AdminSettings if none exist"""
        # Create a temporary database file
        with tempfile.NamedTemporaryFile(delete=False, suffix='.db') as tmp_file:
            db_path = tmp_file.name
        
        try:
            # Create config with temporary database
            config = Config.for_testing(db_path=db_path)
            
            # Create session - this should create default AdminSettings
            session = create_session(config)
            
            # Verify AdminSettings was created
            admin_settings = session.query(AdminSettings).first()
            assert admin_settings is not None
            assert admin_settings.require_approval is False  # Default value
            assert admin_settings.id == 1  # First entry
            
            session.close()
        finally:
            # Clean up temporary file
            if os.path.exists(db_path):
                os.unlink(db_path)
    
    def test_create_session_is_idempotent(self):
        """Test that create_session() is idempotent - doesn't create duplicate AdminSettings"""
        # Create a temporary database file
        with tempfile.NamedTemporaryFile(delete=False, suffix='.db') as tmp_file:
            db_path = tmp_file.name
        
        try:
            config = Config.for_testing(db_path=db_path)
            
            # Create first session
            session1 = create_session(config)
            admin_settings1 = session1.query(AdminSettings).first()
            assert admin_settings1 is not None
            session1.close()
            
            # Create second session with same database
            session2 = create_session(config)
            admin_settings_list = session2.query(AdminSettings).all()
            
            # Should still have only one AdminSettings entry
            assert len(admin_settings_list) == 1
            assert admin_settings_list[0].id == admin_settings1.id
            assert admin_settings_list[0].require_approval == admin_settings1.require_approval
            
            session2.close()
        finally:
            # Clean up temporary file
            if os.path.exists(db_path):
                os.unlink(db_path)
    
    def test_create_session_preserves_existing_admin_settings(self):
        """Test that create_session() preserves existing AdminSettings values"""
        # Create a temporary database file
        with tempfile.NamedTemporaryFile(delete=False, suffix='.db') as tmp_file:
            db_path = tmp_file.name
        
        try:
            config = Config.for_testing(db_path=db_path)
            
            # Create first session and modify AdminSettings
            session1 = create_session(config)
            admin_settings = session1.query(AdminSettings).first()
            admin_settings.require_approval = True  # Change from default False
            session1.commit()
            session1.close()
            
            # Create second session
            session2 = create_session(config)
            admin_settings_check = session2.query(AdminSettings).first()
            
            # Should preserve the modified value
            assert admin_settings_check.require_approval is True
            # Can't compare object IDs across sessions, just verify it's the expected value
            assert admin_settings_check.id == 1  # Should be the first and only entry
            
            session2.close()
        finally:
            # Clean up temporary file
            if os.path.exists(db_path):
                os.unlink(db_path)
    
    def test_create_session_creates_all_tables(self):
        """Test that create_session() creates all expected database tables"""
        # Create a temporary database file
        with tempfile.NamedTemporaryFile(delete=False, suffix='.db') as tmp_file:
            db_path = tmp_file.name
        
        try:
            config = Config.for_testing(db_path=db_path)
            session = create_session(config)
            
            # Check that all tables were created
            engine = session.bind
            inspector = inspect(engine)
            table_names = set(inspector.get_table_names())
            
            expected_tables = {
                'new_users',
                'pending_ban_requests', 
                'banned_users',
                'approved_users',
                'admin_settings',
                'message_queue'
            }
            
            assert expected_tables.issubset(table_names), f"Missing tables: {expected_tables - table_names}"
            
            session.close()
        finally:
            # Clean up temporary file
            if os.path.exists(db_path):
                os.unlink(db_path)
    
    def test_create_session_with_nonexistent_directory(self):
        """Test that create_session() works when database directory doesn't exist"""
        # Create a path in a nonexistent directory
        with tempfile.TemporaryDirectory() as temp_dir:
            nested_dir = os.path.join(temp_dir, 'nested', 'subdir')
            db_path = os.path.join(nested_dir, 'test.db')
            
            config = Config.for_testing(db_path=db_path)
            
            # Directory should be created during config initialization
            assert os.path.exists(nested_dir)
            
            # create_session should work with the new directory
            session = create_session(config)
            admin_settings = session.query(AdminSettings).first()
            
            assert admin_settings is not None
            assert os.path.exists(db_path)
            
            session.close()
    
    def test_create_session_default_admin_settings_values(self):
        """Test specific default values in AdminSettings"""
        with tempfile.NamedTemporaryFile(delete=False, suffix='.db') as tmp_file:
            db_path = tmp_file.name
        
        try:
            config = Config.for_testing(db_path=db_path)
            session = create_session(config)
            
            admin_settings = session.query(AdminSettings).first()
            
            # Verify specific default value as per db.py:129
            assert admin_settings.require_approval is False
            
            session.close()
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_load_database_config_from_env(self, monkeypatch, tmp_path):
        """Sanity-check that load_database_config reads DB_PATH from env in production."""
        from config import load_database_config
        db_file = tmp_path / "env_db.sqlite"
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("DB_PATH", str(db_file))
        cfg = load_database_config()
        assert cfg.db_path == str(db_file)