import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def test_migrazione_crea_tutte_le_tabelle(tmp_path):
    url = f"sqlite:///{tmp_path / 'mig.sqlite'}"
    radice = Path(__file__).resolve().parents[1]
    cfg = Config(str(radice / "alembic.ini"))
    cfg.set_main_option("script_location", str(radice / "alembic"))
    os.environ["ALEMBIC_URL"] = url
    try:
        command.upgrade(cfg, "head")
    finally:
        os.environ.pop("ALEMBIC_URL", None)
    tabelle = set(inspect(create_engine(url)).get_table_names())
    attese = {"users", "accounts", "watched_folders", "clienti", "prodotti", "pratiche", "eventi", "messaggi", "documenti", "regole", "solleciti", "sequenze_codici", "sync_runs", "impostazioni", "teams", "team_membri", "contatti_cliente"}
    assert attese <= tabelle


def _config(url: str) -> Config:
    radice = Path(__file__).resolve().parents[1]
    cfg = Config(str(radice / "alembic.ini"))
    cfg.set_main_option("script_location", str(radice / "alembic"))
    os.environ["ALEMBIC_URL"] = url
    return cfg


def test_migrazione_0002_converte_i_ruoli_e_aggiunge_le_colonne(tmp_path):
    url = f"sqlite:///{tmp_path / 'ruoli.sqlite'}"
    cfg = _config(url)
    try:
        command.upgrade(cfg, "0001")
        motore = create_engine(url)
        with motore.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO users (email, nome, ruolo, attivo, riceve_digest, created_at) VALUES "
                    "('a@latraccia.it', 'A', 'admin', 1, 1, '2026-01-01'), ('m@latraccia.it', 'M', 'membro', 1, 1, '2026-01-01')"
                )
            )
        command.upgrade(cfg, "head")
        with motore.connect() as conn:
            ruoli = dict(conn.execute(text("SELECT email, ruolo FROM users")).all())
        assert ruoli == {"a@latraccia.it": "amministratore", "m@latraccia.it": "operativo"}
        colonne = {c["name"] for c in inspect(motore).get_columns("pratiche")}
        assert {"team_id", "sostituto_user_id"} <= colonne
        assert "codice_oracle" in {c["name"] for c in inspect(motore).get_columns("clienti")}

        # e si torna indietro senza perdere gli utenti
        command.downgrade(cfg, "0001")
        with motore.connect() as conn:
            ruoli = dict(conn.execute(text("SELECT email, ruolo FROM users")).all())
        assert ruoli == {"a@latraccia.it": "admin", "m@latraccia.it": "membro"}
        assert "teams" not in inspect(motore).get_table_names()
    finally:
        os.environ.pop("ALEMBIC_URL", None)
