"""banned_user_uuid_pk

Migrate banned_users.id from autoincrement integer to UUID string.

Revision ID: d1e2f3a4b5c6
Revises: c9f1a2b3d456
Create Date: 2026-03-30 17:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
import uuid


# revision identifiers, used by Alembic.
revision = 'd1e2f3a4b5c6'
down_revision = 'c9f1a2b3d456'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite doesn't support ALTER COLUMN, so we recreate the table.
    # 1. Create new table with UUID primary key
    op.create_table(
        'banned_users_new',
        sa.Column('id', sa.Text(), nullable=False, primary_key=True),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('user_name', sa.Text(), nullable=True),
        sa.Column('chat_id', sa.Integer(), nullable=False),
        sa.Column('message_text', sa.Text(), nullable=False),
        sa.Column('banned_at', sa.DateTime(), server_default=sa.func.now()),
    )

    # 2. Copy data, generating UUIDs for existing rows
    conn = op.get_bind()
    rows = conn.execute(sa.text(
        "SELECT id, user_id, user_name, chat_id, message_text, banned_at FROM banned_users"
    )).fetchall()

    for row in rows:
        conn.execute(
            sa.text(
                "INSERT INTO banned_users_new (id, user_id, user_name, chat_id, message_text, banned_at) "
                "VALUES (:id, :user_id, :user_name, :chat_id, :message_text, :banned_at)"
            ),
            {
                "id": str(uuid.uuid4()),
                "user_id": row[1],
                "user_name": row[2],
                "chat_id": row[3],
                "message_text": row[4],
                "banned_at": row[5],
            },
        )

    # 3. Drop old table and rename
    op.drop_table('banned_users')
    op.rename_table('banned_users_new', 'banned_users')

    # 4. Recreate indexes
    op.create_index('ix_banned_users_user_id', 'banned_users', ['user_id'])
    op.create_index('ix_banned_users_user_chat', 'banned_users', ['user_id', 'chat_id'])


def downgrade() -> None:
    # Recreate with integer PK
    op.create_table(
        'banned_users_old',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('user_name', sa.Text(), nullable=True),
        sa.Column('chat_id', sa.Integer(), nullable=False),
        sa.Column('message_text', sa.Text(), nullable=False),
        sa.Column('banned_at', sa.DateTime(), server_default=sa.func.now()),
    )

    op.execute(
        "INSERT INTO banned_users_old (user_id, user_name, chat_id, message_text, banned_at) "
        "SELECT user_id, user_name, chat_id, message_text, banned_at FROM banned_users"
    )

    op.drop_table('banned_users')
    op.rename_table('banned_users_old', 'banned_users')

    op.create_index('ix_banned_users_user_id', 'banned_users', ['user_id'])
    op.create_index('ix_banned_users_user_chat', 'banned_users', ['user_id', 'chat_id'])
