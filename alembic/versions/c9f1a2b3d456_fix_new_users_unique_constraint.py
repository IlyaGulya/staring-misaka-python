"""fix_new_users_unique_constraint

Drop legacy UNIQUE(user_id) constraint that prevents tracking users across
multiple chats. Keep only UNIQUE(user_id, chat_id).

Revision ID: c9f1a2b3d456
Revises: b8d9e32f1a47
Create Date: 2026-03-30 14:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c9f1a2b3d456'
down_revision: Union[str, Sequence[str], None] = 'b8d9e32f1a47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Recreate new_users table without the legacy UNIQUE(user_id) constraint."""
    bind = op.get_bind()

    # Check if the legacy constraint exists by inspecting the CREATE TABLE SQL
    result = bind.execute(sa.text(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='new_users'"
    ))
    row = result.fetchone()
    if row is None:
        return  # Table doesn't exist, nothing to do

    create_sql = row[0]
    # Only run if the old UNIQUE(user_id) exists (separate from the composite one)
    if 'UNIQUE (user_id)' not in create_sql:
        print("Legacy UNIQUE(user_id) constraint not found, skipping migration")
        return

    print("Dropping legacy UNIQUE(user_id) constraint from new_users table...")

    # SQLite doesn't support ALTER TABLE DROP CONSTRAINT — must recreate
    op.execute("ALTER TABLE new_users RENAME TO _new_users_old")
    op.execute("""
        CREATE TABLE new_users (
            id INTEGER NOT NULL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            join_time DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL,
            CONSTRAINT uq_new_users_user_chat UNIQUE (user_id, chat_id)
        )
    """)
    op.execute("""
        INSERT INTO new_users (id, user_id, chat_id, join_time)
        SELECT id, user_id, chat_id, join_time FROM _new_users_old
    """)
    op.execute("DROP TABLE _new_users_old")

    # Recreate index
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_new_users_user_chat
        ON new_users (user_id, chat_id)
    """)

    print("Legacy UNIQUE(user_id) constraint removed successfully!")


def downgrade() -> None:
    """Add back the legacy UNIQUE(user_id) constraint."""
    op.execute("ALTER TABLE new_users RENAME TO _new_users_old")
    op.execute("""
        CREATE TABLE new_users (
            id INTEGER NOT NULL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            join_time DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL,
            CONSTRAINT uq_new_users_user_chat UNIQUE (user_id, chat_id),
            UNIQUE (user_id)
        )
    """)
    op.execute("""
        INSERT INTO new_users (id, user_id, chat_id, join_time)
        SELECT id, user_id, chat_id, join_time FROM _new_users_old
    """)
    op.execute("DROP TABLE _new_users_old")
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_new_users_user_chat
        ON new_users (user_id, chat_id)
    """)
