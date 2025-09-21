#!/usr/bin/env python3
"""Docker entrypoint script for Staring Misaka bot"""
import os
import sys
import sqlite3
import subprocess
from pathlib import Path


def error_exit(message: str) -> None:
    """Print error and exit"""
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def backup_database(db_path: str) -> str:
    """Create a backup of the database before migrations"""
    import time

    # Determine backup location - prefer mounted backup dir if available
    backup_dir = "/backups" if Path("/backups").exists() else str(Path(db_path).parent)
    db_name = Path(db_path).stem
    backup_filename = f"{db_name}.backup_{int(time.time())}.db"
    backup_path = str(Path(backup_dir) / backup_filename)

    print(f"Creating database backup: {backup_path}")

    try:
        # Use SQLite's backup API for safe backup
        source = sqlite3.connect(db_path)
        backup = sqlite3.connect(backup_path)
        source.backup(backup)
        source.close()
        backup.close()

        print("Database backup completed successfully")
        return backup_path
    except Exception as e:
        error_exit(f"Failed to create database backup: {e}")


def run_cmd(cmd: list[str]) -> bool:
    """Run command and return success status"""
    try:
        result = subprocess.run(cmd)
        return result.returncode == 0
    except Exception:
        return False


def main() -> None:
    print("Starting Staring Misaka bot initialization...")

    # Set production environment
    os.environ["ENVIRONMENT"] = "production"

    # Check required environment variables
    required_vars = ["DB_PATH", "ANTHROPIC_API_KEY", "API_ID", "API_HASH", "BOT_TOKEN"]
    for var in required_vars:
        if not os.getenv(var):
            error_exit(f"{var} environment variable is required")

    # Check database file exists
    db_path = os.getenv("DB_PATH")
    if not Path(db_path).exists():
        error_exit(f"Database file not found at {db_path}")

    # Validate database schema
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        existing_tables = {row[0] for row in cursor.fetchall()}
        conn.close()

        expected_tables = {'admin_settings', 'new_users', 'pending_ban_requests', 'banned_users'}
        if not expected_tables.issubset(existing_tables):
            missing = expected_tables - existing_tables
            error_exit(f"Missing database tables: {missing}")
    except Exception as e:
        error_exit(f"Database validation failed: {e}")

    # Create database backup before any migration operations
    if os.getenv("SKIP_BACKUP") != "true":
        backup_path = backup_database(db_path)
        print(f"Backup available at: {backup_path}")

    # Handle Alembic migration
    print("Checking migration status...")

    # Check if database needs stamping by trying to get current revision
    check_result = subprocess.run(
        ["alembic", "current"],
        capture_output=True, text=True
    )

    if check_result.returncode != 0 or not check_result.stdout.strip():
        print("Stamping database with baseline migration...")
        if not run_cmd(["alembic", "stamp", "7278023a76b5"]):
            error_exit("Failed to stamp database")

    # Run migrations
    print("Running migrations...")
    if not run_cmd(["alembic", "upgrade", "head"]):
        error_exit("Migration failed")

    # Check if testing mode
    if os.getenv("SKIP_APP_START") == "true":
        print("Migration setup completed successfully!")
        sys.exit(0)

    # Start application
    print("Starting Staring Misaka bot...")
    os.execvp("pixi", ["pixi", "run", "start"])


if __name__ == "__main__":
    main()