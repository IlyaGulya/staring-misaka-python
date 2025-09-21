"""add_performance_indexes_and_constraints

Revision ID: 00de03bd4136
Revises: a81687876567
Create Date: 2025-09-21 20:52:40.308467

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '00de03bd4136'
down_revision: Union[str, Sequence[str], None] = 'a81687876567'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add performance indexes and constraints."""

    # Helper function to check if index exists
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    def index_exists(table_name, index_name):
        existing_indexes = [idx['name'] for idx in inspector.get_indexes(table_name)]
        return index_name in existing_indexes

    # MESSAGE QUEUE PERFORMANCE INDEXES
    # Critical composite index for queue selection (status, next_retry_at, created_at)
    if not index_exists("message_queue", "ix_message_queue_status_nextretry_created"):
        op.create_index(
            "ix_message_queue_status_nextretry_created",
            "message_queue",
            ["status", "next_retry_at", "created_at"]
        )

    # Index for cleanup operations (status, processed_at)
    if not index_exists("message_queue", "ix_message_queue_status_processed_at"):
        op.create_index(
            "ix_message_queue_status_processed_at",
            "message_queue",
            ["status", "processed_at"]
        )

    # Index for user-specific lookups (user_id, chat_id)
    if not index_exists("message_queue", "ix_message_queue_user_chat"):
        op.create_index(
            "ix_message_queue_user_chat",
            "message_queue",
            ["user_id", "chat_id"]
        )

    # For SQLite, we need to use batch mode to add unique constraints
    with op.batch_alter_table("message_queue") as batch_op:
        batch_op.create_unique_constraint(
            "uq_message_queue_triplet",
            ["user_id", "chat_id", "message_id"]
        )

    # NEW_USERS TABLE FIXES - recreate table without single user_id unique constraint
    # For SQLite, we need to recreate the table to remove the inline UNIQUE constraint
    with op.batch_alter_table("new_users", recreate="always") as batch_op:
        # The batch operation will recreate the table without the old UNIQUE(user_id)
        # We just need to add our new composite unique constraint
        batch_op.create_unique_constraint(
            "uq_new_users_user_chat",
            ["user_id", "chat_id"]
        )

    # Add composite index for user lookups (user_id, chat_id)
    op.create_index(
        "ix_new_users_user_chat",
        "new_users",
        ["user_id", "chat_id"]
    )

    # APPROVED_USERS TABLE OPTIMIZATIONS
    # Add composite index for user lookups (user_id, chat_id)
    op.create_index(
        "ix_approved_users_user_chat",
        "approved_users",
        ["user_id", "chat_id"]
    )

    # Add composite unique constraint to prevent duplicates using batch mode
    with op.batch_alter_table("approved_users") as batch_op:
        batch_op.create_unique_constraint(
            "uq_approved_users_user_chat",
            ["user_id", "chat_id"]
        )

    # OPTIONAL HELPER INDEXES
    # Index on banned_users for user lookups
    op.create_index(
        "ix_banned_users_user_id",
        "banned_users",
        ["user_id"]
    )

    # Composite index on banned_users for chat-specific lookups
    op.create_index(
        "ix_banned_users_user_chat",
        "banned_users",
        ["user_id", "chat_id"]
    )

    # Index on pending_ban_requests for sender lookups
    op.create_index(
        "ix_pending_ban_requests_sender_id",
        "pending_ban_requests",
        ["sender_id"]
    )


def downgrade() -> None:
    """Remove performance indexes and constraints."""

    # Remove optional helper indexes
    op.drop_index("ix_pending_ban_requests_sender_id", table_name="pending_ban_requests")
    op.drop_index("ix_banned_users_user_chat", table_name="banned_users")
    op.drop_index("ix_banned_users_user_id", table_name="banned_users")

    # Remove approved_users optimizations using batch mode
    with op.batch_alter_table("approved_users") as batch_op:
        batch_op.drop_constraint("uq_approved_users_user_chat", type_="unique")
    op.drop_index("ix_approved_users_user_chat", table_name="approved_users")

    # Remove new_users optimizations using batch mode
    op.drop_index("ix_new_users_user_chat", table_name="new_users")

    # Recreate the table with the original UNIQUE(user_id) constraint
    with op.batch_alter_table("new_users", recreate="always") as batch_op:
        batch_op.drop_constraint("uq_new_users_user_chat", type_="unique")
        # The original table had UNIQUE(user_id) - this will be restored automatically
        # when we recreate without our composite constraint

    # Remove message queue optimizations using batch mode
    with op.batch_alter_table("message_queue") as batch_op:
        batch_op.drop_constraint("uq_message_queue_triplet", type_="unique")
    op.drop_index("ix_message_queue_user_chat", table_name="message_queue")
    op.drop_index("ix_message_queue_status_processed_at", table_name="message_queue")
    op.drop_index("ix_message_queue_status_nextretry_created", table_name="message_queue")
