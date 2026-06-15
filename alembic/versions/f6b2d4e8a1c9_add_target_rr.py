"""add_target_rr_to_trades_and_backtest_results

Revision ID: f6b2d4e8a1c9
Revises: e5a1c2d3f4b7
Create Date: 2026-06-16 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f6b2d4e8a1c9'
down_revision: Union[str, Sequence[str], None] = 'e5a1c2d3f4b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Planned reward:risk per trade for the adaptive-RR regime (2:1 in range,
    # 3:1 when the previous completed 6h candle's ADX >= threshold). Nullable;
    # historical rows predating adaptive RR stay NULL and render as "—".
    op.add_column('trades', sa.Column('target_rr', sa.Float(), nullable=True))
    op.add_column('backtest_results', sa.Column('target_rr', sa.Float(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('backtest_results', 'target_rr')
    op.drop_column('trades', 'target_rr')
