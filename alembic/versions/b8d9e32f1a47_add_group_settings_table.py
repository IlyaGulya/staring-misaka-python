"""Add group settings table

Revision ID: b8d9e32f1a47
Revises: 4b33ae41d934
Create Date: 2025-10-13 14:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b8d9e32f1a47'
down_revision: Union[str, Sequence[str], None] = '4b33ae41d934'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add group_settings table for per-group bot enable/disable functionality."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if 'group_settings' not in inspector.get_table_names():
        op.create_table('group_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('chat_id', sa.Integer(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('chat_id', name='group_settings_chat_id_key')
        )

        # Add performance index for chat_id lookups
        op.create_index('ix_group_settings_chat_id', 'group_settings', ['chat_id'])

        print("group_settings table created successfully!")


def downgrade() -> None:
    """Remove group_settings table."""
    # Drop the index first
    try:
        op.drop_index('ix_group_settings_chat_id', table_name='group_settings')
    except Exception:
        pass

    # Drop the table
    op.drop_table('group_settings')
    print("group_settings table removed successfully!")
