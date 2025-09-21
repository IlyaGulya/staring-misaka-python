from logging.config import fileConfig
import os
import sys

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# Add project root to path so we can import our models
sys.path.append(os.path.dirname(os.path.dirname(__file__)))

# Import our database models for autogenerate support
from db import Base

# For migrations, we'll create a minimal config to get the db_path
def get_db_path_for_migrations():
    """Get database path for migrations."""
    # First check for explicit DB_PATH environment variable (production)
    db_path = os.environ.get('DB_PATH')
    if db_path:
        return db_path
    
    # If not in production environment, try to load from .env file
    if os.environ.get("ENVIRONMENT") != "production":
        env_file_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env')
        if os.path.exists(env_file_path):
            try:
                from config import load_database_config
                return load_database_config().db_path
            except Exception as e:
                raise RuntimeError(f"Failed to load database config from .env file: {e}")
    
    # No valid database path found
    raise RuntimeError(
        "No database path configured. Either set DB_PATH environment variable "
        "or ensure .env file exists with valid configuration."
    )

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Set target_metadata to our Base metadata for autogenerate support
target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    # Check if URL is already set in config (e.g., by production scripts)
    url = config.get_main_option("sqlalchemy.url")
    
    if not url or url == "driver://user:pass@localhost/dbname":
        # Load config to get database URL
        db_path = get_db_path_for_migrations()
        url = f'sqlite:///{db_path}'
    
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    # Check if URL is already set in config (e.g., by production scripts)
    url = config.get_main_option("sqlalchemy.url")
    
    if not url or url == "driver://user:pass@localhost/dbname":
        # Load config to get database URL
        db_path = get_db_path_for_migrations()
        url = f'sqlite:///{db_path}'
    
    # Override the sqlalchemy.url in config
    configuration = config.get_section(config.config_ini_section, {})
    configuration['sqlalchemy.url'] = url
    
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
