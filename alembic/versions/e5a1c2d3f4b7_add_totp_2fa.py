"""add_totp_2fa

Revision ID: e5a1c2d3f4b7
Revises: d4f9b2c1a8e3
Create Date: 2026-06-14 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5a1c2d3f4b7'
down_revision: Union[str, Sequence[str], None] = 'd4f9b2c1a8e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Opt-in TOTP two-factor auth. totp_secret_enc holds the Fernet-encrypted
    # base32 secret; totp_enabled gates whether step-up verification applies.
    op.add_column('users', sa.Column('totp_secret_enc', sa.Text(), nullable=True))
    op.add_column('users', sa.Column('totp_enabled', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('users', sa.Column('totp_backup_codes', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'totp_backup_codes')
    op.drop_column('users', 'totp_enabled')
    op.drop_column('users', 'totp_secret_enc')
