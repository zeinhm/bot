"""add_funding_fee_to_trades

Revision ID: c3e8a1f2b4d6
Revises: a2f1c3d5e7b9
Create Date: 2026-06-10 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3e8a1f2b4d6'
down_revision: Union[str, Sequence[str], None] = 'a2f1c3d5e7b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Funding fee over the position's life (signed USDT). Part of the
    # income-based PnL breakdown; commission becomes the USDT trading fee and
    # pnl_usdt becomes the net realized PnL that matches Binance Position History.
    op.add_column('trades', sa.Column('funding_fee', sa.Float(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('trades', 'funding_fee')
