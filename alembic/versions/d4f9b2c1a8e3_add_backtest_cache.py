"""add_backtest_cache

Revision ID: d4f9b2c1a8e3
Revises: c3e8a1f2b4d6
Create Date: 2026-06-11 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4f9b2c1a8e3'
down_revision: Union[str, Sequence[str], None] = 'c3e8a1f2b4d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Cached amd_engine results so the backtester doesn't re-simulate ~210k bars
    # on every call. Keyed by (strategy, params, candle-data) signature.
    op.create_table(
        'backtest_runs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('signature', sa.String(length=64), nullable=False),
        sa.Column('strategy', sa.String(length=50), nullable=False),
        sa.Column('symbol', sa.String(length=20), nullable=False),
        sa.Column('interval', sa.String(length=5), nullable=False),
        sa.Column('params_hash', sa.String(length=64), nullable=False),
        sa.Column('data_first_ts', sa.Integer(), nullable=True),
        sa.Column('data_last_ts', sa.Integer(), nullable=True),
        sa.Column('candle_count', sa.Integer(), nullable=True),
        sa.Column('total_setups', sa.Integer(), nullable=False),
        sa.Column('stats', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('signature', name='uq_backtest_run_sig'),
    )
    op.create_index('ix_backtest_runs_signature', 'backtest_runs', ['signature'])

    op.create_table(
        'backtest_setups',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('run_id', sa.Integer(), nullable=False),
        sa.Column('ordinal', sa.Integer(), nullable=False),
        sa.Column('entry_time', sa.Integer(), nullable=False),
        sa.Column('data', sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(['run_id'], ['backtest_runs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_bt_setup_run_ordinal', 'backtest_setups', ['run_id', 'ordinal'])
    op.create_index('ix_bt_setup_run_entry', 'backtest_setups', ['run_id', 'entry_time'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_bt_setup_run_entry', table_name='backtest_setups')
    op.drop_index('ix_bt_setup_run_ordinal', table_name='backtest_setups')
    op.drop_table('backtest_setups')
    op.drop_index('ix_backtest_runs_signature', table_name='backtest_runs')
    op.drop_table('backtest_runs')
