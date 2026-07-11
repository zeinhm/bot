"""add_setup_json_to_trades

Revision ID: b8d3f6a2c4e1
Revises: a7c9e1b3d5f2
Create Date: 2026-07-09 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b8d3f6a2c4e1'
down_revision: Union[str, Sequence[str], None] = 'a7c9e1b3d5f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # AMD setup geometry (accumulation + manipulation box coordinates, entry/sl/tp)
    # stored as JSON at signal time, so the live Position chart can draw the same
    # setup the Backtester shows. Nullable; manual/legacy trades stay NULL.
    op.add_column('trades', sa.Column('setup_json', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('trades', 'setup_json')
