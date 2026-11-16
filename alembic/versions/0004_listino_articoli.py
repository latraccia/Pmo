"""listino di vendita: articoli con codice e descrizione

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-16 09:00:00+00:00

- tabella `articoli` (codice univoco, descrizione, prodotto, unità, prezzo di listino, IVA);
- `articolo_id` sulle righe di offerte e ordini (facoltativo: le righe libere restano possibili).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('articoli',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('codice', sa.String(length=60), nullable=False),
    sa.Column('descrizione', sa.Text(), nullable=False),
    sa.Column('prodotto_id', sa.Integer(), nullable=True),
    sa.Column('unita', sa.String(length=20), nullable=False),
    sa.Column('prezzo_listino', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('aliquota_iva', sa.Numeric(precision=5, scale=2), nullable=True),
    sa.Column('note', sa.Text(), nullable=False),
    sa.Column('attivo', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['prodotto_id'], ['prodotti.id'], name=op.f('fk_articoli_prodotto_id_prodotti'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_articoli'))
    )
    with op.batch_alter_table('articoli', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_articoli_codice'), ['codice'], unique=True)
        batch_op.create_index(batch_op.f('ix_articoli_prodotto_id'), ['prodotto_id'], unique=False)

    with op.batch_alter_table('righe_offerta', schema=None) as batch_op:
        batch_op.add_column(sa.Column('articolo_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(batch_op.f('fk_righe_offerta_articolo_id_articoli'), 'articoli', ['articolo_id'], ['id'], ondelete='SET NULL')

    with op.batch_alter_table('righe_ordine', schema=None) as batch_op:
        batch_op.add_column(sa.Column('articolo_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(batch_op.f('fk_righe_ordine_articolo_id_articoli'), 'articoli', ['articolo_id'], ['id'], ondelete='SET NULL')



def downgrade() -> None:
    with op.batch_alter_table('righe_ordine', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_righe_ordine_articolo_id_articoli'), type_='foreignkey')
        batch_op.drop_column('articolo_id')

    with op.batch_alter_table('righe_offerta', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_righe_offerta_articolo_id_articoli'), type_='foreignkey')
        batch_op.drop_column('articolo_id')

    with op.batch_alter_table('articoli', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_articoli_prodotto_id'))
        batch_op.drop_index(batch_op.f('ix_articoli_codice'))

    op.drop_table('articoli')
