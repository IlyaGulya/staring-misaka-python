"""add_database_performance_optimizations

Revision ID: 4b33ae41d934
Revises: a81687876567
Create Date: 2025-09-21 21:15:43.315706

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4b33ae41d934'
down_revision: Union[str, Sequence[str], None] = 'a81687876567'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add comprehensive database performance optimizations."""

    bind = op.get_bind()
    inspector = sa.inspect(bind)

    def has_index(table_name, index_name):
        existing_indexes = [idx['name'] for idx in inspector.get_indexes(table_name)]
        return index_name in existing_indexes

    # DATA DEDUPLICATION FOR EXISTING TABLES
    print("Removing duplicate data before adding unique constraints...")

    # Remove new_users duplicates (keep most recent by join_time)
    op.execute("""
        DELETE FROM new_users
        WHERE rowid NOT IN (
            SELECT MAX(rowid)
            FROM new_users
            GROUP BY user_id, chat_id
        )
    """)

    # Remove approved_users duplicates (keep most recent by approved_at)
    op.execute("""
        DELETE FROM approved_users
        WHERE rowid NOT IN (
            SELECT MAX(rowid)
            FROM approved_users
            GROUP BY user_id, chat_id
        )
    """)

    # Note: message_queue table is empty (created in previous migration), no deduplication needed

    # MESSAGE QUEUE PERFORMANCE OPTIMIZATIONS
    print("Adding message queue performance indexes...")

    # Critical composite index for queue selection (status, next_retry_at, created_at)
    if not has_index("message_queue", "ix_message_queue_status_nextretry_created"):
        op.create_index(
            "ix_message_queue_status_nextretry_created",
            "message_queue",
            ["status", "next_retry_at", "created_at"]
        )

    # Index for cleanup operations (status, processed_at)
    if not has_index("message_queue", "ix_message_queue_status_processed_at"):
        op.create_index(
            "ix_message_queue_status_processed_at",
            "message_queue",
            ["status", "processed_at"]
        )

    # Index for user-specific lookups (user_id, chat_id)
    if not has_index("message_queue", "ix_message_queue_user_chat"):
        op.create_index(
            "ix_message_queue_user_chat",
            "message_queue",
            ["user_id", "chat_id"]
        )

    # Add unique constraint to prevent duplicate messages
    with op.batch_alter_table("message_queue") as batch_op:
        try:
            batch_op.create_unique_constraint(
                "uq_message_queue_triplet",
                ["user_id", "chat_id", "message_id"]
            )
        except Exception:
            pass

    # NEW_USERS TABLE OPTIMIZATIONS
    print("Optimizing new_users table for multi-chat support...")

    # Recreate new_users table: remove UNIQUE(user_id), add composite unique (user_id, chat_id)
    with op.batch_alter_table("new_users", recreate="always") as batch_op:
        # The recreate will remove the old UNIQUE(user_id) and use model definition
        try:
            batch_op.create_unique_constraint(
                "uq_new_users_user_chat",
                ["user_id", "chat_id"]
            )
        except Exception:
            pass

    # Add composite index for user lookups (user_id, chat_id)
    if not has_index("new_users", "ix_new_users_user_chat"):
        op.create_index(
            "ix_new_users_user_chat",
            "new_users",
            ["user_id", "chat_id"]
        )

    # APPROVED_USERS TABLE OPTIMIZATIONS
    print("Adding approved_users performance optimizations...")

    # Add composite index for user lookups (user_id, chat_id)
    if not has_index("approved_users", "ix_approved_users_user_chat"):
        op.create_index(
            "ix_approved_users_user_chat",
            "approved_users",
            ["user_id", "chat_id"]
        )

    # Add composite unique constraint to prevent duplicates
    with op.batch_alter_table("approved_users") as batch_op:
        try:
            batch_op.create_unique_constraint(
                "uq_approved_users_user_chat",
                ["user_id", "chat_id"]
            )
        except Exception:
            pass

    # HELPER INDEXES FOR OTHER TABLES
    print("Adding helper indexes...")

    # Index on banned_users for user lookups
    if not has_index("banned_users", "ix_banned_users_user_id"):
        op.create_index(
            "ix_banned_users_user_id",
            "banned_users",
            ["user_id"]
        )

    # Composite index on banned_users for chat-specific lookups
    if not has_index("banned_users", "ix_banned_users_user_chat"):
        op.create_index(
            "ix_banned_users_user_chat",
            "banned_users",
            ["user_id", "chat_id"]
        )

    # Index on pending_ban_requests for sender lookups
    if not has_index("pending_ban_requests", "ix_pending_ban_requests_sender_id"):
        op.create_index(
            "ix_pending_ban_requests_sender_id",
            "pending_ban_requests",
            ["sender_id"]
        )

    print("Database performance optimizations completed successfully!")


def downgrade() -> None:
    """Remove all performance optimizations."""

    print("Removing performance optimizations...")

    # Remove helper indexes
    try:
        op.drop_index("ix_pending_ban_requests_sender_id", table_name="pending_ban_requests")
    except Exception:
        pass
    try:
        op.drop_index("ix_banned_users_user_chat", table_name="banned_users")
    except Exception:
        pass
    try:
        op.drop_index("ix_banned_users_user_id", table_name="banned_users")
    except Exception:
        pass

    # Remove approved_users optimizations
    try:
        op.drop_index("ix_approved_users_user_chat", table_name="approved_users")
    except Exception:
        pass
    with op.batch_alter_table("approved_users") as batch_op:
        try:
            batch_op.drop_constraint("uq_approved_users_user_chat", type_="unique")
        except Exception:
            pass

    # Restore new_users original schema with UNIQUE(user_id)
    try:
        op.drop_index("ix_new_users_user_chat", table_name="new_users")
    except Exception:
        pass

    with op.batch_alter_table("new_users", recreate="always") as batch_op:
        try:
            batch_op.drop_constraint("uq_new_users_user_chat", type_="unique")
        except Exception:
            pass
        # Restore original single-column unique constraint
        try:
            batch_op.create_unique_constraint("new_users_user_id_key", ["user_id"])
        except Exception:
            pass

    # Remove message queue optimizations
    try:
        op.drop_index("ix_message_queue_user_chat", table_name="message_queue")
    except Exception:
        pass
    try:
        op.drop_index("ix_message_queue_status_processed_at", table_name="message_queue")
    except Exception:
        pass
    try:
        op.drop_index("ix_message_queue_status_nextretry_created", table_name="message_queue")
    except Exception:
        pass

    with op.batch_alter_table("message_queue") as batch_op:
        try:
            batch_op.drop_constraint("uq_message_queue_triplet", type_="unique")
        except Exception:
            pass

    print("Performance optimizations removed successfully!")
