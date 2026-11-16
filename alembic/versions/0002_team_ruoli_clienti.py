"""team, ruoli e anagrafica clienti

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-05 09:00:00+00:00

- tabelle `teams` e `team_membri` (un utente può stare in più team);
- `pratiche.team_id` e `pratiche.sostituto_user_id`, `accounts.team_id` (casella di team);
- anagrafica completa dei clienti (P.IVA, codici Oracle e VTE, ...) e tabella `contatti_cliente`;
- ruoli: `admin` diventa `amministratore`, `membro` diventa `operativo`.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None

CAMPI_CLIENTE = [
    ('ragione_sociale', 255),
    ('partita_iva', 20),
    ('codice_fiscale', 20),
    ('indirizzo', 255),
    ('citta', 120),
    ('telefono', 50),
    ('email', 255),
    ('pec', 255),
    ('codice_oracle', 50),
    ('codice_vte', 50),
]


def upgrade() -> None:
    op.create_table('teams',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('nome', sa.String(length=120), nullable=False),
    sa.Column('descrizione', sa.Text(), nullable=False, server_default=''),
    sa.Column('attivo', sa.Boolean(), nullable=False, server_default=sa.true()),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_teams')),
    sa.UniqueConstraint('nome', name=op.f('uq_teams_nome'))
    )
    op.create_table('team_membri',
    sa.Column('team_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], name=op.f('fk_team_membri_team_id_teams'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_team_membri_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('team_id', 'user_id', name=op.f('pk_team_membri'))
    )
    op.create_table('contatti_cliente',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('cliente_id', sa.Integer(), nullable=False),
    sa.Column('nome', sa.String(length=255), nullable=False),
    sa.Column('ruolo', sa.String(length=120), nullable=False, server_default=''),
    sa.Column('email', sa.String(length=255), nullable=False, server_default=''),
    sa.Column('telefono', sa.String(length=50), nullable=False, server_default=''),
    sa.Column('note', sa.Text(), nullable=False, server_default=''),
    sa.Column('attivo', sa.Boolean(), nullable=False, server_default=sa.true()),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['cliente_id'], ['clienti.id'], name=op.f('fk_contatti_cliente_cliente_id_clienti'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_contatti_cliente'))
    )
    with op.batch_alter_table('contatti_cliente', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_contatti_cliente_cliente_id'), ['cliente_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_contatti_cliente_email'), ['email'], unique=False)

    with op.batch_alter_table('pratiche', schema=None) as batch_op:
        batch_op.add_column(sa.Column('sostituto_user_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('team_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_pratiche_team_id'), ['team_id'], unique=False)
        batch_op.create_foreign_key(batch_op.f('fk_pratiche_sostituto_user_id_users'), 'users', ['sostituto_user_id'], ['id'])
        batch_op.create_foreign_key(batch_op.f('fk_pratiche_team_id_teams'), 'teams', ['team_id'], ['id'], ondelete='SET NULL')

    with op.batch_alter_table('accounts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('team_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_accounts_team_id'), ['team_id'], unique=False)
        batch_op.create_foreign_key(batch_op.f('fk_accounts_team_id_teams'), 'teams', ['team_id'], ['id'], ondelete='SET NULL')

    with op.batch_alter_table('clienti', schema=None) as batch_op:
        for nome, lunghezza in CAMPI_CLIENTE:
            batch_op.add_column(sa.Column(nome, sa.String(length=lunghezza), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('team_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(batch_op.f('fk_clienti_team_id_teams'), 'teams', ['team_id'], ['id'], ondelete='SET NULL')

    op.execute("UPDATE users SET ruolo = 'amministratore' WHERE ruolo = 'admin'")
    op.execute("UPDATE users SET ruolo = 'operativo' WHERE ruolo = 'membro'")


def downgrade() -> None:
    op.execute("UPDATE users SET ruolo = 'admin' WHERE ruolo = 'amministratore'")
    op.execute("UPDATE users SET ruolo = 'membro' WHERE ruolo <> 'admin'")

    with op.batch_alter_table('clienti', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_clienti_team_id_teams'), type_='foreignkey')
        batch_op.drop_column('team_id')
        for nome, _ in reversed(CAMPI_CLIENTE):
            batch_op.drop_column(nome)

    with op.batch_alter_table('accounts', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_accounts_team_id_teams'), type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_accounts_team_id'))
        batch_op.drop_column('team_id')

    with op.batch_alter_table('pratiche', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_pratiche_team_id_teams'), type_='foreignkey')
        batch_op.drop_constraint(batch_op.f('fk_pratiche_sostituto_user_id_users'), type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_pratiche_team_id'))
        batch_op.drop_column('team_id')
        batch_op.drop_column('sostituto_user_id')

    with op.batch_alter_table('contatti_cliente', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_contatti_cliente_email'))
        batch_op.drop_index(batch_op.f('ix_contatti_cliente_cliente_id'))

    op.drop_table('contatti_cliente')
    op.drop_table('team_membri')
    op.drop_table('teams')
