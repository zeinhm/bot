"""add_strategy_to_trades_and_backtest_results

Revision ID: a7c9e1b3d5f2
Revises: f6b2d4e8a1c9
Create Date: 2026-06-19 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7c9e1b3d5f2'
down_revision: Union[str, Sequence[str], None] = 'f6b2d4e8a1c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Which strategy opened the trade / produced the backtest setup: 'amd_15m'
    # (15m production) or 'trend_5m' (5m trend-sniper overlay). Nullable;
    # historical rows predating the multi-strategy platform stay NULL and are
    # treated as 'amd_15m' (the only strategy that existed then).
    op.add_column('trades', sa.Column('strategy', sa.String(length=20), nullable=True))
    op.add_column('backtest_results', sa.Column('strategy', sa.String(length=20), nullable=True))
    op.create_index('ix_trades_strategy', 'trades', ['strategy'])
    op.create_index('ix_backtest_results_strategy', 'backtest_results', ['strategy'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_backtest_results_strategy', table_name='backtest_results')
    op.drop_index('ix_trades_strategy', table_name='trades')
    op.drop_column('backtest_results', 'strategy')
    op.drop_column('trades', 'strategy')
