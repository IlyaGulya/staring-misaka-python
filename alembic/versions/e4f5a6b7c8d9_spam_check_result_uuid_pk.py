"""spam_check_result_uuid_pk

Migrate spam_check_results.id from autoincrement integer to UUID string.
Also update FK columns (banned_users.spam_check_id, message_queue.spam_check_id) to Text.

Revision ID: e4f5a6b7c8d9
Revises: c3a1f5e8d902
Create Date: 2026-04-01 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
import uuid


revision = 'e4f5a6b7c8d9'
down_revision = 'c3a1f5e8d902'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Recreate spam_check_results with Text PK
    op.create_table(
        'spam_check_results_new',
        sa.Column('id', sa.Text(), nullable=False, primary_key=True),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('chat_id', sa.Integer(), nullable=False),
        sa.Column('message_text', sa.Text(), nullable=False),
        sa.Column('is_spam', sa.Boolean(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('raw_response', sa.Text(), nullable=True),
        sa.Column('model', sa.Text(), nullable=True),
        sa.Column('checked_at', sa.DateTime(), server_default=sa.func.now()),
    )

    conn = op.get_bind()

    # Build mapping of old integer IDs to new UUIDs
    rows = conn.execute(sa.text(
        "SELECT id, user_id, chat_id, message_text, is_spam, reason, raw_response, model, checked_at "
        "FROM spam_check_results"
    )).fetchall()

    id_map = {}  # old_int_id -> new_uuid
    for row in rows:
        new_id = str(uuid.uuid4())
        id_map[row[0]] = new_id
        conn.execute(
            sa.text(
                "INSERT INTO spam_check_results_new (id, user_id, chat_id, message_text, is_spam, reason, raw_response, model, checked_at) "
                "VALUES (:id, :user_id, :chat_id, :message_text, :is_spam, :reason, :raw_response, :model, :checked_at)"
            ),
            {
                "id": new_id,
                "user_id": row[1],
                "chat_id": row[2],
                "message_text": row[3],
                "is_spam": row[4],
                "reason": row[5],
                "raw_response": row[6],
                "model": row[7],
                "checked_at": row[8],
            },
        )

    # 2. Update FK references in banned_users and message_queue
    for old_id, new_id in id_map.items():
        conn.execute(sa.text(
            "UPDATE banned_users SET spam_check_id = :new_id WHERE spam_check_id = :old_id"
        ), {"new_id": new_id, "old_id": str(old_id)})
        conn.execute(sa.text(
            "UPDATE message_queue SET spam_check_id = :new_id WHERE spam_check_id = :old_id"
        ), {"new_id": new_id, "old_id": str(old_id)})

    # 3. Drop old table and rename
    op.drop_table('spam_check_results')
    op.rename_table('spam_check_results_new', 'spam_check_results')

    # 4. Recreate index
    op.create_index('ix_spam_check_results_user_chat', 'spam_check_results', ['user_id', 'chat_id'])


def downgrade() -> None:
    op.create_table(
        'spam_check_results_old',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('chat_id', sa.Integer(), nullable=False),
        sa.Column('message_text', sa.Text(), nullable=False),
        sa.Column('is_spam', sa.Boolean(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('raw_response', sa.Text(), nullable=True),
        sa.Column('model', sa.Text(), nullable=True),
        sa.Column('checked_at', sa.DateTime(), server_default=sa.func.now()),
    )

    op.execute(
        "INSERT INTO spam_check_results_old (user_id, chat_id, message_text, is_spam, reason, raw_response, model, checked_at) "
        "SELECT user_id, chat_id, message_text, is_spam, reason, raw_response, model, checked_at FROM spam_check_results"
    )

    op.drop_table('spam_check_results')
    op.rename_table('spam_check_results_old', 'spam_check_results')
    op.create_index('ix_spam_check_results_user_chat', 'spam_check_results', ['user_id', 'chat_id'])
