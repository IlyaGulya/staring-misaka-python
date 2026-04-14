"""add_raw_text_and_metadata_json

Add raw_message_text and message_metadata (JSON) columns to
spam_check_results and message_queue for cleaner trace attributes
without regex extraction.

Revision ID: f1a2b3c4d5e6
Revises: e4f5a6b7c8d9
Create Date: 2026-04-14 09:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'f1a2b3c4d5e6'
down_revision = 'e4f5a6b7c8d9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('spam_check_results') as batch_op:
        batch_op.add_column(sa.Column('raw_message_text', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('message_metadata', sa.JSON(), nullable=True))

    with op.batch_alter_table('message_queue') as batch_op:
        batch_op.add_column(sa.Column('raw_message_text', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('message_metadata', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('spam_check_results') as batch_op:
        batch_op.drop_column('message_metadata')
        batch_op.drop_column('raw_message_text')

    with op.batch_alter_table('message_queue') as batch_op:
        batch_op.drop_column('message_metadata')
        batch_op.drop_column('raw_message_text')
