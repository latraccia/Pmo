"""Notifica email (digest) personale: ogni destinatario riceve solo i solleciti e le verifiche delle pratiche che può vedere."""

from __future__ import annotations

import pytest

from pmo.db.models import Sollecito, StatoSollecito, Team, User
from pmo.services import digest as modulo_digest
from pmo.services import pratiche as svc
from pmo.services.digest import componi_digest, destinatari_digest, invia_digest
from pmo.services.impostazioni import set_impostazione


@pytest.fixture
def scenario(db):
    alfa, beta = Team(nome="Alfa"), Team(nome="Beta")
    db.add_all([alfa, beta])
    db.flush()
    op = User(email="op@latraccia.it", nome="Op", ruolo="operativo", teams=[alfa])
    ceo = User(email="ceo@latraccia.it", nome="Ceo", ruolo="direzione")
    spento = User(email="spento@latraccia.it", nome="Spento", ruolo="direzione", attivo=False)
    db.add_all([op, ceo, spento])
    db.flush()
    pa = svc.crea_pratica(db, "Ordine del team Alfa", team_id=alfa.id)
    pb = svc.crea_pratica(db, "Ordine riservato Beta", team_id=beta.id, stato_verifica="proposto")
    for p in (pa, pb):
        db.add(Sollecito(pratica_id=p.id, regola="r", chiave=f"{p.id}:r", gravita="critico", titolo=f"Sollecito {p.titolo}", stato=StatoSollecito.APERTO))
    db.commit()
    return {"op": op, "ceo": ceo, "pa": pa, "pb": pb}


def test_ognuno_vede_solo_le_sue_pratiche(db, settings, scenario):
    del_operativo = componi_digest(db, settings, utente=scenario["op"])
    assert "Ordine del team Alfa" in del_operativo.testo and "Ordine riservato Beta" not in del_operativo.testo
    assert del_operativo.contatori["solleciti"] == 1 and del_operativo.contatori["pratiche_proposte"] == 0
    della_direzione = componi_digest(db, settings, utente=scenario["ceo"])
    assert della_direzione.contatori["solleciti"] == 2 and della_direzione.contatori["pratiche_proposte"] == 1


def test_anteprima_per_destinatario_ed_esclusi(db, settings, scenario):
    esito = invia_digest(
        db, settings, destinatari=["OP@latraccia.it", "ceo@latraccia.it", "lista@esterno.it", "spento@latraccia.it"], solo_anteprima=True
    )
    assert esito["destinatari"] == ["op@latraccia.it", "ceo@latraccia.it"]
    assert esito["esclusi"] == ["lista@esterno.it", "spento@latraccia.it"]
    assert "Ordine riservato Beta" not in esito["anteprime"]["op@latraccia.it"]
    assert "Ordine riservato Beta" in esito["anteprime"]["ceo@latraccia.it"]


def test_una_mail_per_destinatario(db, settings, scenario, monkeypatch):
    spedite: list[tuple[list[str], str]] = []
    monkeypatch.setattr(modulo_digest, "_invia_smtp", lambda _s, destinatari, contenuto: spedite.append((destinatari, contenuto.testo)))
    con_smtp = settings.model_copy(update={"smtp_host": "smtp.prova"})
    esito = invia_digest(db, con_smtp, destinatari=["op@latraccia.it", "ceo@latraccia.it"])
    assert esito["inviato"] and esito["canale"] == "smtp" and esito["inviati"] == ["op@latraccia.it", "ceo@latraccia.it"]
    per_destinatario = dict((d[0], testo) for d, testo in spedite)
    assert "Ordine riservato Beta" not in per_destinatario["op@latraccia.it"]
    assert "Ordine riservato Beta" in per_destinatario["ceo@latraccia.it"]
    assert all(s.notificato_il is not None for s in db.query(Sollecito).all())


def test_senza_canale_di_invio_nessun_sollecito_risulta_notificato(db, settings, scenario):
    esito = invia_digest(db, settings, destinatari=["ceo@latraccia.it"])
    assert not esito["inviato"] and "Nessun canale" in esito["errore"]
    assert all(s.notificato_il is None for s in db.query(Sollecito).all())


def test_la_notifica_va_a_chi_la_ha_attiva_e_a_nessun_altro(db, settings, scenario):
    """I destinatari sono solo gli utenti attivi con la notifica email attiva, al loro indirizzo.

    Una vecchia lista di destinatari salvata nelle impostazioni non conta più.
    """
    scenario["ceo"].riceve_digest = False
    set_impostazione(db, "digest", {"attivo": True, "destinatari": ["ceo@latraccia.it", "lista@esterno.it"]})
    db.commit()
    assert destinatari_digest(db, settings) == ["op@latraccia.it"]
    esito = invia_digest(db, settings, solo_anteprima=True)
    assert esito["destinatari"] == ["op@latraccia.it"] and esito["esclusi"] == []
