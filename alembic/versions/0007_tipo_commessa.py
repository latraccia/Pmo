"""tipo della commessa

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-17 18:30:00+00:00

- `pratiche.tipo`: fornitura o manutenzione; le commesse esistenti diventano forniture.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('pratiche', schema=None) as batch_op:
        batch_op.add_column(sa.Column('tipo', sa.String(length=16), nullable=False, server_default='fornitura'))
        batch_op.create_index(batch_op.f('ix_pratiche_tipo'), ['tipo'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('pratiche', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_pratiche_tipo'))
        batch_op.drop_column('tipo')
