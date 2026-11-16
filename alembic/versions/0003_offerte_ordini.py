"""offerte, ordini e righe

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-16 08:23:30+00:00

- offerte con revisioni e righe; ordini con righe; allocazioni tra righe d'ordine e righe d'offerta;
- `ordini_messaggi`: una mail può riguardare più ordini;
- dati: ogni pratica con un importo d'ordine riceve un ordine confermato con una riga unica "Da dettagliare".
"""
from __future__ import annotations

from datetime import UTC, date, datetime

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('offerte',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('pratica_id', sa.Integer(), nullable=False),
    sa.Column('numero', sa.String(length=80), nullable=False),
    sa.Column('titolo', sa.String(length=300), nullable=False),
    sa.Column('note', sa.Text(), nullable=False),
    sa.Column('created_by_user_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], name=op.f('fk_offerte_created_by_user_id_users')),
    sa.ForeignKeyConstraint(['pratica_id'], ['pratiche.id'], name=op.f('fk_offerte_pratica_id_pratiche'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_offerte'))
    )
    with op.batch_alter_table('offerte', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_offerte_pratica_id'), ['pratica_id'], unique=False)

    op.create_table('revisioni_offerta',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('offerta_id', sa.Integer(), nullable=False),
    sa.Column('numero_revisione', sa.Integer(), nullable=False),
    sa.Column('data', sa.Date(), nullable=True),
    sa.Column('stato', sa.String(length=16), nullable=False),
    sa.Column('condizioni', sa.Text(), nullable=False),
    sa.Column('messaggio_id', sa.Integer(), nullable=True),
    sa.Column('documento_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['documento_id'], ['documenti.id'], name=op.f('fk_revisioni_offerta_documento_id_documenti'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['messaggio_id'], ['messaggi.id'], name=op.f('fk_revisioni_offerta_messaggio_id_messaggi'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['offerta_id'], ['offerte.id'], name=op.f('fk_revisioni_offerta_offerta_id_offerte'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_revisioni_offerta')),
    sa.UniqueConstraint('offerta_id', 'numero_revisione', name=op.f('uq_revisioni_offerta_offerta_id'))
    )
    with op.batch_alter_table('revisioni_offerta', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_revisioni_offerta_offerta_id'), ['offerta_id'], unique=False)

    op.create_table('ordini',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('pratica_id', sa.Integer(), nullable=False),
    sa.Column('numero', sa.String(length=80), nullable=False),
    sa.Column('data_ordine', sa.Date(), nullable=True),
    sa.Column('revisione_offerta_id', sa.Integer(), nullable=True),
    sa.Column('riferimento_offerta', sa.String(length=80), nullable=False),
    sa.Column('condizioni', sa.Text(), nullable=False),
    sa.Column('note', sa.Text(), nullable=False),
    sa.Column('stato_verifica', sa.String(length=16), nullable=False),
    sa.Column('conformita', sa.String(length=20), nullable=False),
    sa.Column('parziale', sa.Boolean(), nullable=False),
    sa.Column('esito_verifica', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('verificato_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('deroga_motivo', sa.Text(), nullable=False),
    sa.Column('deroga_user_id', sa.Integer(), nullable=True),
    sa.Column('deroga_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('avanzamento', sa.String(length=20), nullable=False),
    sa.Column('attesa', sa.String(length=10), nullable=False),
    sa.Column('attesa_motivo', sa.String(length=500), nullable=False),
    sa.Column('attesa_dal', sa.Date(), nullable=True),
    sa.Column('chiave', sa.String(length=300), nullable=True),
    sa.Column('messaggio_id', sa.Integer(), nullable=True),
    sa.Column('documento_id', sa.Integer(), nullable=True),
    sa.Column('evento_id', sa.Integer(), nullable=True),
    sa.Column('created_by_user_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], name=op.f('fk_ordini_created_by_user_id_users')),
    sa.ForeignKeyConstraint(['deroga_user_id'], ['users.id'], name=op.f('fk_ordini_deroga_user_id_users')),
    sa.ForeignKeyConstraint(['documento_id'], ['documenti.id'], name=op.f('fk_ordini_documento_id_documenti'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['evento_id'], ['eventi.id'], name=op.f('fk_ordini_evento_id_eventi'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['messaggio_id'], ['messaggi.id'], name=op.f('fk_ordini_messaggio_id_messaggi'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['pratica_id'], ['pratiche.id'], name=op.f('fk_ordini_pratica_id_pratiche'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['revisione_offerta_id'], ['revisioni_offerta.id'], name=op.f('fk_ordini_revisione_offerta_id_revisioni_offerta'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_ordini')),
    sa.UniqueConstraint('chiave', name=op.f('uq_ordini_chiave'))
    )
    with op.batch_alter_table('ordini', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_ordini_attesa'), ['attesa'], unique=False)
        batch_op.create_index(batch_op.f('ix_ordini_avanzamento'), ['avanzamento'], unique=False)
        batch_op.create_index(batch_op.f('ix_ordini_conformita'), ['conformita'], unique=False)
        batch_op.create_index(batch_op.f('ix_ordini_pratica_id'), ['pratica_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_ordini_stato_verifica'), ['stato_verifica'], unique=False)

    op.create_table('righe_offerta',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('revisione_id', sa.Integer(), nullable=False),
    sa.Column('posizione', sa.Integer(), nullable=False),
    sa.Column('prodotto_id', sa.Integer(), nullable=True),
    sa.Column('modulo', sa.String(length=120), nullable=False),
    sa.Column('descrizione', sa.Text(), nullable=False),
    sa.Column('quantita', sa.Numeric(precision=12, scale=3), nullable=True),
    sa.Column('prezzo_unitario', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('sconto_pct', sa.Numeric(precision=5, scale=2), nullable=True),
    sa.Column('aliquota_iva', sa.Numeric(precision=5, scale=2), nullable=True),
    sa.Column('stato_verifica', sa.String(length=16), nullable=False),
    sa.ForeignKeyConstraint(['prodotto_id'], ['prodotti.id'], name=op.f('fk_righe_offerta_prodotto_id_prodotti'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['revisione_id'], ['revisioni_offerta.id'], name=op.f('fk_righe_offerta_revisione_id_revisioni_offerta'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_righe_offerta'))
    )
    with op.batch_alter_table('righe_offerta', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_righe_offerta_revisione_id'), ['revisione_id'], unique=False)

    op.create_table('ordini_messaggi',
    sa.Column('ordine_id', sa.Integer(), nullable=False),
    sa.Column('messaggio_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['messaggio_id'], ['messaggi.id'], name=op.f('fk_ordini_messaggi_messaggio_id_messaggi'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['ordine_id'], ['ordini.id'], name=op.f('fk_ordini_messaggi_ordine_id_ordini'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('ordine_id', 'messaggio_id', name=op.f('pk_ordini_messaggi'))
    )
    op.create_table('righe_ordine',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('ordine_id', sa.Integer(), nullable=False),
    sa.Column('posizione', sa.Integer(), nullable=False),
    sa.Column('prodotto_id', sa.Integer(), nullable=True),
    sa.Column('modulo', sa.String(length=120), nullable=False),
    sa.Column('descrizione', sa.Text(), nullable=False),
    sa.Column('quantita', sa.Numeric(precision=12, scale=3), nullable=True),
    sa.Column('prezzo_unitario', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('sconto_pct', sa.Numeric(precision=5, scale=2), nullable=True),
    sa.Column('aliquota_iva', sa.Numeric(precision=5, scale=2), nullable=True),
    sa.Column('stato_verifica', sa.String(length=16), nullable=False),
    sa.ForeignKeyConstraint(['ordine_id'], ['ordini.id'], name=op.f('fk_righe_ordine_ordine_id_ordini'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['prodotto_id'], ['prodotti.id'], name=op.f('fk_righe_ordine_prodotto_id_prodotti'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_righe_ordine'))
    )
    with op.batch_alter_table('righe_ordine', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_righe_ordine_ordine_id'), ['ordine_id'], unique=False)

    op.create_table('allocazioni_righe',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('riga_offerta_id', sa.Integer(), nullable=False),
    sa.Column('riga_ordine_id', sa.Integer(), nullable=False),
    sa.Column('quantita', sa.Numeric(precision=12, scale=3), nullable=True),
    sa.Column('importo', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('origine', sa.String(length=12), nullable=False),
    sa.ForeignKeyConstraint(['riga_offerta_id'], ['righe_offerta.id'], name=op.f('fk_allocazioni_righe_riga_offerta_id_righe_offerta'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['riga_ordine_id'], ['righe_ordine.id'], name=op.f('fk_allocazioni_righe_riga_ordine_id_righe_ordine'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_allocazioni_righe')),
    sa.UniqueConstraint('riga_offerta_id', 'riga_ordine_id', name=op.f('uq_allocazioni_righe_riga_offerta_id'))
    )
    with op.batch_alter_table('allocazioni_righe', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_allocazioni_righe_riga_offerta_id'), ['riga_offerta_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_allocazioni_righe_riga_ordine_id'), ['riga_ordine_id'], unique=False)

    _ordini_dalle_pratiche()


def _ordini_dalle_pratiche() -> None:
    """Ogni pratica con un importo d'ordine riceve un ordine confermato con una sola riga "Da dettagliare"."""
    conn = op.get_bind()
    pratiche = sa.table(
        'pratiche', sa.column('id', sa.Integer), sa.column('importo_ordine', sa.Numeric), sa.column('riferimenti', sa.JSON),
        sa.column('data_apertura', sa.Date),
    )
    eventi = sa.table(
        'eventi', sa.column('pratica_id', sa.Integer), sa.column('tipo', sa.String), sa.column('stato_verifica', sa.String),
        sa.column('data_evento', sa.DateTime(timezone=True)),
    )
    ordini = sa.table('ordini', sa.column('esito_verifica', sa.JSON), *[sa.column(c) for c in (
        'id', 'pratica_id', 'numero', 'data_ordine', 'riferimento_offerta', 'condizioni', 'note', 'stato_verifica', 'conformita',
        'parziale', 'deroga_motivo', 'avanzamento', 'attesa', 'attesa_motivo', 'created_at', 'updated_at',
    )])
    righe = sa.table(
        'righe_ordine', sa.column('quantita', sa.Numeric(12, 3)), sa.column('prezzo_unitario', sa.Numeric(14, 2)),
        *[sa.column(c) for c in ('ordine_id', 'posizione', 'modulo', 'descrizione', 'stato_verifica')],
    )
    adesso = datetime.now(UTC)
    for pratica_id, importo, riferimenti, apertura in conn.execute(
        sa.select(pratiche.c.id, pratiche.c.importo_ordine, pratiche.c.riferimenti, pratiche.c.data_apertura).where(
            pratiche.c.importo_ordine.is_not(None)
        )
    ).all():
        primo = conn.execute(
            sa.select(sa.func.min(eventi.c.data_evento)).where(
                eventi.c.pratica_id == pratica_id, eventi.c.tipo == 'ordine_ricevuto', eventi.c.stato_verifica == 'confermato'
            )
        ).scalar()
        if isinstance(primo, str):  # SQLite restituisce il testo
            primo = datetime.fromisoformat(primo)
        if isinstance(apertura, str):
            apertura = date.fromisoformat(apertura)
        numero = str((riferimenti or {}).get('numero_ordine') or '')[:80] if isinstance(riferimenti, dict) else ''
        ordine_id = conn.execute(
            ordini.insert().values(
                pratica_id=pratica_id, numero=numero, data_ordine=primo.date() if primo else apertura, riferimento_offerta='',
                condizioni='', note='Creato dalla migrazione: righe da dettagliare.', stato_verifica='confermato',
                conformita='da_verificare', parziale=False, esito_verifica={}, deroga_motivo='', avanzamento='aperto',
                attesa='nessuna', attesa_motivo='', created_at=adesso, updated_at=adesso,
            ).returning(ordini.c.id)
        ).scalar_one()
        conn.execute(
            righe.insert().values(
                ordine_id=ordine_id, posizione=1, modulo='', descrizione='Da dettagliare', quantita=1, prezzo_unitario=importo,
                stato_verifica='confermato',
            )
        )


def downgrade() -> None:
    with op.batch_alter_table('allocazioni_righe', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_allocazioni_righe_riga_ordine_id'))
        batch_op.drop_index(batch_op.f('ix_allocazioni_righe_riga_offerta_id'))

    op.drop_table('allocazioni_righe')
    with op.batch_alter_table('righe_ordine', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_righe_ordine_ordine_id'))

    op.drop_table('righe_ordine')
    op.drop_table('ordini_messaggi')
    with op.batch_alter_table('righe_offerta', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_righe_offerta_revisione_id'))

    op.drop_table('righe_offerta')
    with op.batch_alter_table('ordini', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_ordini_stato_verifica'))
        batch_op.drop_index(batch_op.f('ix_ordini_pratica_id'))
        batch_op.drop_index(batch_op.f('ix_ordini_conformita'))
        batch_op.drop_index(batch_op.f('ix_ordini_avanzamento'))
        batch_op.drop_index(batch_op.f('ix_ordini_attesa'))

    op.drop_table('ordini')
    with op.batch_alter_table('revisioni_offerta', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_revisioni_offerta_offerta_id'))

    op.drop_table('revisioni_offerta')
    with op.batch_alter_table('offerte', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_offerte_pratica_id'))

    op.drop_table('offerte')
