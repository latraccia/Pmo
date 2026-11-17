"""estrazione delle righe dai documenti con Claude

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-17 09:00:00+00:00

- tabella `estrazioni`: coda e registro delle letture di ordini e offerte da mail, Drive e (in futuro) protocollo;
- `estrazione_id` sulle righe di offerte e ordini (da quale lettura viene la riga);
- `evento_id` sulle offerte (l'evento che le ha create, se non sono scritte a mano).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('estrazioni',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('pratica_id', sa.Integer(), nullable=False),
    sa.Column('fonte', sa.String(length=16), nullable=False),
    sa.Column('messaggio_id', sa.Integer(), nullable=True),
    sa.Column('documento_id', sa.Integer(), nullable=True),
    sa.Column('riferimento_esterno', sa.String(length=300), nullable=False),
    sa.Column('ordine_id', sa.Integer(), nullable=True),
    sa.Column('revisione_offerta_id', sa.Integer(), nullable=True),
    sa.Column('stato', sa.String(length=16), nullable=False),
    sa.Column('tentativi', sa.Integer(), nullable=False),
    sa.Column('errore', sa.Text(), nullable=False),
    sa.Column('file', JSON, nullable=False),
    sa.Column('risultato', JSON, nullable=False),
    sa.Column('modello', sa.String(length=80), nullable=False),
    sa.Column('token', JSON, nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('elaborata_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['pratica_id'], ['pratiche.id'], name=op.f('fk_estrazioni_pratica_id_pratiche'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['messaggio_id'], ['messaggi.id'], name=op.f('fk_estrazioni_messaggio_id_messaggi'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['documento_id'], ['documenti.id'], name=op.f('fk_estrazioni_documento_id_documenti'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['ordine_id'], ['ordini.id'], name=op.f('fk_estrazioni_ordine_id_ordini'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['revisione_offerta_id'], ['revisioni_offerta.id'], name=op.f('fk_estrazioni_revisione_offerta_id_revisioni_offerta'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_estrazioni'))
    )
    with op.batch_alter_table('estrazioni', schema=None) as batch_op:
        for colonna in ('pratica_id', 'messaggio_id', 'documento_id', 'ordine_id', 'revisione_offerta_id', 'stato'):
            batch_op.create_index(batch_op.f(f'ix_estrazioni_{colonna}'), [colonna], unique=False)

    for tabella in ('righe_offerta', 'righe_ordine'):
        with op.batch_alter_table(tabella, schema=None) as batch_op:
            batch_op.add_column(sa.Column('estrazione_id', sa.Integer(), nullable=True))
            batch_op.create_foreign_key(batch_op.f(f'fk_{tabella}_estrazione_id_estrazioni'), 'estrazioni', ['estrazione_id'], ['id'], ondelete='SET NULL')

    with op.batch_alter_table('offerte', schema=None) as batch_op:
        batch_op.add_column(sa.Column('evento_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(batch_op.f('fk_offerte_evento_id_eventi'), 'eventi', ['evento_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    with op.batch_alter_table('offerte', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_offerte_evento_id_eventi'), type_='foreignkey')
        batch_op.drop_column('evento_id')

    for tabella in ('righe_ordine', 'righe_offerta'):
        with op.batch_alter_table(tabella, schema=None) as batch_op:
            batch_op.drop_constraint(batch_op.f(f'fk_{tabella}_estrazione_id_estrazioni'), type_='foreignkey')
            batch_op.drop_column('estrazione_id')

    with op.batch_alter_table('estrazioni', schema=None) as batch_op:
        for colonna in ('stato', 'revisione_offerta_id', 'ordine_id', 'documento_id', 'messaggio_id', 'pratica_id'):
            batch_op.drop_index(batch_op.f(f'ix_estrazioni_{colonna}'))

    op.drop_table('estrazioni')
