"""add_paper_bot_started_to_users

Revision ID: a2f1c3d5e7b9
Revises: 673da50d8570
Create Date: 2026-06-04 07:50:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a2f1c3d5e7b9'
down_revision: Union[str, Sequence[str], None] = '673da50d8570'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('users', sa.Column('paper_bot_started', sa.Boolean(), nullable=False, server_default=sa.text('false')))
    op.execute("UPDATE users SET paper_bot_started = true WHERE is_approved = true")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'paper_bot_started')
