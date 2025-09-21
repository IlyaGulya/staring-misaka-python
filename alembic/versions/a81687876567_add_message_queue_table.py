"""Add message queue table

Revision ID: a81687876567
Revises: 7278023a76b5
Create Date: 2025-08-12 20:53:18.790307

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a81687876567'
down_revision: Union[str, Sequence[str], None] = '7278023a76b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add message queue table."""
    # Check if table already exists before creating
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    
    if 'message_queue' not in inspector.get_table_names():
        op.create_table('message_queue',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('chat_id', sa.Integer(), nullable=False),
        sa.Column('message_id', sa.Integer(), nullable=False),
        sa.Column('message_text', sa.Text(), nullable=False),
        sa.Column('status', sa.Text(), nullable=False),
        sa.Column('retry_count', sa.Integer(), nullable=False),
        sa.Column('max_retries', sa.Integer(), nullable=False),
        sa.Column('next_retry_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('processed_at', sa.DateTime(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('spam_result', sa.Boolean(), nullable=True),
        sa.PrimaryKeyConstraint('id')
        )


def downgrade() -> None:
    """Remove message queue table."""
    # Only drop the message_queue table
    op.drop_table('message_queue')
