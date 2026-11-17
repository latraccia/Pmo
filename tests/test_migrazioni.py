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
    attese = {"users", "accounts", "watched_folders", "clienti", "prodotti", "pratiche", "eventi", "messaggi", "documenti", "regole", "solleciti", "sequenze_codici", "sync_runs", "impostazioni", "teams", "team_membri", "contatti_cliente", "articoli", "offerte", "revisioni_offerta", "righe_offerta", "ordini", "righe_ordine", "allocazioni_righe", "ordini_messaggi", "estrazioni"}
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


def test_migrazione_0003_crea_gli_ordini_dalle_pratiche(tmp_path):
    url = f"sqlite:///{tmp_path / 'ordini.sqlite'}"
    cfg = _config(url)
    try:
        command.upgrade(cfg, "0002")
        motore = create_engine(url)
        with motore.begin() as conn:
            for id_, importo, riferimenti in ((1, "18000", '{"numero_ordine": "ORD-7"}'), (2, None, "{}")):
                conn.execute(
                    text(
                        "INSERT INTO pratiche (id, codice, titolo, descrizione, stato, stato_dal, stato_verifica, origine, priorita, "
                        "importo_ordine, data_apertura, riferimenti, note, created_at, updated_at) VALUES (:id, :codice, 'P', '', "
                        "'ordine', '2026-01-01', 'confermato', 'manuale', 'media', :importo, '2026-01-05', :rif, '', '2026-01-01', '2026-01-01')"
                    ),
                    {"id": id_, "codice": f"PMO-2026-000{id_}", "importo": importo, "rif": riferimenti},
                )
        command.upgrade(cfg, "head")
        with motore.connect() as conn:
            ordini = conn.execute(text("SELECT pratica_id, numero, data_ordine, conformita FROM ordini")).all()
            righe = conn.execute(text("SELECT descrizione, quantita, prezzo_unitario FROM righe_ordine")).all()
        assert [tuple(o) for o in ordini] == [(1, "ORD-7", "2026-01-05", "da_verificare")]
        assert [(r[0], float(r[1]), float(r[2])) for r in righe] == [("Da dettagliare", 1.0, 18000.0)]
        command.downgrade(cfg, "0002")
        assert "ordini" not in inspect(motore).get_table_names()
    finally:
        os.environ.pop("ALEMBIC_URL", None)
