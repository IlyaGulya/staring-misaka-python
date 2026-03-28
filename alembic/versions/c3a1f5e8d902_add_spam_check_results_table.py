"""add_spam_check_results_table

Revision ID: c3a1f5e8d902
Revises: b8d9e32f1a47
Create Date: 2026-01-23 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3a1f5e8d902'
down_revision: Union[str, Sequence[str], None] = 'd1e2f3a4b5c6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create spam_check_results table and add new columns to message_queue and banned_users."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    # Create spam_check_results table
    if 'spam_check_results' not in existing_tables:
        op.create_table(
            'spam_check_results',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('user_id', sa.Integer(), nullable=False),
            sa.Column('chat_id', sa.Integer(), nullable=False),
            sa.Column('message_text', sa.Text(), nullable=False),
            sa.Column('is_spam', sa.Boolean(), nullable=False),
            sa.Column('reason', sa.Text(), nullable=False),
            sa.Column('raw_response', sa.Text(), nullable=True),
            sa.Column('model', sa.Text(), nullable=True),
            sa.Column('checked_at', sa.DateTime(), server_default=sa.func.now()),
        )
        op.create_index('ix_spam_check_results_user_chat', 'spam_check_results', ['user_id', 'chat_id'])

    # Add spam_reason, raw_llm_response, and spam_check_id to message_queue
    existing_columns = [col['name'] for col in inspector.get_columns('message_queue')]
    with op.batch_alter_table('message_queue') as batch_op:
        if 'spam_reason' not in existing_columns:
            batch_op.add_column(sa.Column('spam_reason', sa.Text(), nullable=True))
        if 'raw_llm_response' not in existing_columns:
            batch_op.add_column(sa.Column('raw_llm_response', sa.Text(), nullable=True))
        if 'spam_check_id' not in existing_columns:
            batch_op.add_column(sa.Column('spam_check_id', sa.Integer(), nullable=True))
            batch_op.create_foreign_key('fk_message_queue_spam_check_id', 'spam_check_results', ['spam_check_id'], ['id'])

    # Add spam_check_id to banned_users
    existing_columns = [col['name'] for col in inspector.get_columns('banned_users')]
    if 'spam_check_id' not in existing_columns:
        with op.batch_alter_table('banned_users') as batch_op:
            batch_op.add_column(sa.Column('spam_check_id', sa.Integer(), nullable=True))
            batch_op.create_foreign_key('fk_banned_users_spam_check_id', 'spam_check_results', ['spam_check_id'], ['id'])


def downgrade() -> None:
    """Remove new columns and drop spam_check_results table."""
    with op.batch_alter_table('banned_users') as batch_op:
        batch_op.drop_constraint('fk_banned_users_spam_check_id', type_='foreignkey')
        batch_op.drop_column('spam_check_id')

    with op.batch_alter_table('message_queue') as batch_op:
        batch_op.drop_constraint('fk_message_queue_spam_check_id', type_='foreignkey')
        batch_op.drop_column('spam_check_id')
        batch_op.drop_column('raw_llm_response')
        batch_op.drop_column('spam_reason')

    op.drop_index('ix_spam_check_results_user_chat', table_name='spam_check_results')
    op.drop_table('spam_check_results')
