#!/usr/bin/env python3
"""
Production migration setup script for Staring Misaka.

This script safely introduces Alembic to an existing production database
that was created with SQLAlchemy create_all().

IMPORTANT: Run this BEFORE deploying any new code that uses Alembic migrations.
"""

import logging
import sqlite3
import os
from pathlib import Path
from alembic import command
from alembic.config import Config

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def backup_database(db_path: str) -> str:
    """Create a backup of the production database."""
    backup_path = f"{db_path}.backup_{int(os.path.getmtime(db_path))}"
    
    logger.info(f"Creating backup: {backup_path}")
    
    # Use SQLite's backup API for safe backup
    source = sqlite3.connect(db_path)
    backup = sqlite3.connect(backup_path)
    
    source.backup(backup)
    source.close()
    backup.close()
    
    logger.info("Backup completed successfully")
    return backup_path


def validate_existing_schema(db_path: str) -> dict:
    """Validate the existing database schema."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Get all tables
    cursor.execute("""
        SELECT name FROM sqlite_master 
        WHERE type='table' AND name NOT LIKE 'sqlite_%'
        ORDER BY name
    """)
    tables = [row[0] for row in cursor.fetchall()]
    
    schema_info = {"tables": tables, "table_schemas": {}}
    
    # Get schema for each table
    for table in tables:
        cursor.execute(f"PRAGMA table_info({table})")
        columns = cursor.fetchall()
        schema_info["table_schemas"][table] = columns
    
    conn.close()
    
    logger.info(f"Found {len(tables)} tables: {', '.join(tables)}")
    return schema_info


def check_migration_compatibility(db_path: str, expected_revision: str) -> bool:
    """Check if the database schema matches the expected migration."""
    schema_info = validate_existing_schema(db_path)
    
    # Expected tables for the baseline migration (7278023a76b5)
    expected_tables = {
        'admin_settings',
        'approved_users', 
        'banned_users',
        'new_users',
        'pending_ban_requests'
    }
    
    actual_tables = set(schema_info["tables"])
    
    if actual_tables == expected_tables:
        logger.info("✅ Database schema matches baseline migration")
        return True
    elif expected_tables.issubset(actual_tables):
        logger.warning("⚠️  Database has additional tables not in baseline migration")
        logger.warning(f"Extra tables: {actual_tables - expected_tables}")
        return True
    else:
        logger.error("❌ Database schema does not match expected baseline")
        logger.error(f"Missing tables: {expected_tables - actual_tables}")
        return False


def stamp_database(db_path: str, revision: str = "head"):
    """Stamp the database with a specific Alembic revision."""
    project_root = Path(__file__).parent.parent
    alembic_cfg = Config(str(project_root / "alembic.ini"))
    
    # Override database URL with absolute path
    abs_db_path = os.path.abspath(db_path)
    alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{abs_db_path}")
    
    logger.info(f"Stamping database with revision: {revision}")
    logger.info(f"Database path: {abs_db_path}")
    
    try:
        command.stamp(alembic_cfg, revision)
        logger.info("✅ Database stamped successfully")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to stamp database: {e}")
        logger.error(f"Database path used: {abs_db_path}")
        return False


def verify_stamp(db_path: str) -> str:
    """Verify that the database was stamped correctly."""
    abs_db_path = os.path.abspath(db_path)
    conn = sqlite3.connect(abs_db_path)
    cursor = conn.cursor()
    
    try:
        cursor.execute("SELECT version_num FROM alembic_version")
        version = cursor.fetchone()
        if version:
            logger.info(f"✅ Database is stamped with version: {version[0]}")
            return version[0]
        else:
            logger.error("❌ No version found in alembic_version table")
            return None
    except sqlite3.OperationalError:
        logger.error("❌ alembic_version table does not exist")
        return None
    finally:
        conn.close()


def main():
    """Main function to safely set up Alembic on production database."""
    
    # You need to set this to your actual production database path
    db_path = input("Enter path to production database: ").strip()
    
    if not os.path.exists(db_path):
        logger.error(f"Database file not found: {db_path}")
        return 1
    
    logger.info("=== PRODUCTION MIGRATION SETUP ===")
    logger.info(f"Database: {db_path}")
    
    # Step 1: Create backup
    try:
        backup_path = backup_database(db_path)
        logger.info(f"Backup created: {backup_path}")
    except Exception as e:
        logger.error(f"Failed to create backup: {e}")
        return 1
    
    # Step 2: Validate schema
    if not check_migration_compatibility(db_path, "7278023a76b5"):
        logger.error("Schema validation failed. Manual migration required.")
        return 1
    
    # Step 3: Stamp database
    if not stamp_database(db_path, "7278023a76b5"):  # Use baseline migration
        logger.error("Failed to stamp database")
        return 1
    
    # Step 4: Verify stamp
    version = verify_stamp(db_path)
    if not version:
        logger.error("Stamp verification failed")
        return 1
    
    logger.info("=== SUCCESS ===")
    logger.info("Production database is now ready for Alembic migrations!")
    logger.info(f"Current version: {version}")
    logger.info(f"Backup available at: {backup_path}")
    
    return 0


if __name__ == "__main__":
    exit(main())