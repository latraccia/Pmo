"""cliente dell'offerta

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-16 09:35:00+00:00

- `offerte.cliente_id`: il cliente a cui si offre (obbligatorio per le offerte nuove, lo impone l'applicazione);
- dati: le offerte esistenti prendono il cliente della loro commessa.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('offerte', schema=None) as batch_op:
        batch_op.add_column(sa.Column('cliente_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_offerte_cliente_id'), ['cliente_id'], unique=False)
        batch_op.create_foreign_key(batch_op.f('fk_offerte_cliente_id_clienti'), 'clienti', ['cliente_id'], ['id'])

    offerte = sa.table('offerte', sa.column('pratica_id', sa.Integer), sa.column('cliente_id', sa.Integer))
    pratiche = sa.table('pratiche', sa.column('id', sa.Integer), sa.column('cliente_id', sa.Integer))
    op.get_bind().execute(
        offerte.update().values(
            cliente_id=sa.select(pratiche.c.cliente_id).where(pratiche.c.id == offerte.c.pratica_id).scalar_subquery()
        )
    )


def downgrade() -> None:
    with op.batch_alter_table('offerte', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_offerte_cliente_id_clienti'), type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_offerte_cliente_id'))
        batch_op.drop_column('cliente_id')
