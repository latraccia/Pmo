"""Interfaccia web con ruoli e team: chi vede quali pratiche, chi può confermare, chiudere, configurare."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from pmo.db.models import Account, Messaggio, StatoMessaggio, Team, User
from pmo.services import pratiche as svc
from pmo.services.seed import carica_seed


@pytest.fixture
def client(db):
    from pmo.web.app import create_app

    carica_seed(db)
    with TestClient(create_app(), follow_redirects=False) as c:
        yield c


@pytest.fixture
def scenario(db):
    """Due team, un operativo e un commerciale di Alfa, una direzione; una pratica per team; una mail per casella."""
    alfa, beta = Team(nome="Alfa"), Team(nome="Beta")
    db.add_all([alfa, beta])
    db.flush()
    utenti = {
        "op": User(email="op@latraccia.it", nome="Op", ruolo="operativo", teams=[alfa]),
        "comm": User(email="comm@latraccia.it", nome="Comm", ruolo="commerciale", teams=[alfa]),
        "ceo": User(email="ceo@latraccia.it", nome="Ceo", ruolo="direzione"),
    }
    db.add_all(utenti.values())
    casella_alfa = Account(email="alfa@latraccia.it", team_id=alfa.id, token_cifrato="x")
    casella_beta = Account(email="beta@latraccia.it", team_id=beta.id, token_cifrato="x")
    db.add_all([casella_alfa, casella_beta])
    db.flush()
    pa = svc.crea_pratica(db, "Pratica del team Alfa", team_id=alfa.id)
    pb = svc.crea_pratica(db, "Pratica del team Beta", team_id=beta.id, stato_verifica="proposto")
    for n, casella in enumerate((casella_alfa, casella_beta)):
        db.add(
            Messaggio(
                account_id=casella.id, gmail_message_id=f"g{n}", gmail_thread_id=f"t{n}", subject=f"Mail della casella {casella.email}",
                from_email="x@cliente.it", data=datetime.now(UTC), stato=StatoMessaggio.DA_RIVEDERE, motivo="da rivedere",
            )
        )
    db.commit()
    return {"alfa": alfa, "beta": beta, "pa": pa, "pb": pb, **utenti}


def _login(client: TestClient, email: str) -> str:
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/login").text).group(1)
    r = client.post("/auth/dev", data={"email": email, "csrf_token": csrf, "next": "/"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    return csrf


def test_operativo_vede_solo_il_suo_team(client, scenario):
    _login(client, "op@latraccia.it")
    assert client.get(f"/pratiche/{scenario['pa'].id}").status_code == 200
    assert client.get(f"/pratiche/{scenario['pb'].id}").status_code == 404
    elenco = client.get("/pratiche").text
    assert "Pratica del team Alfa" in elenco and "Pratica del team Beta" not in elenco
    # coda di revisione: solo la mail della casella di team visibile; nessuna pratica proposta di Beta
    revisione = client.get("/revisione?scheda=messaggi").text
    assert "alfa@latraccia.it" in revisione and "beta@latraccia.it" not in revisione
    assert "Pratica del team Beta" not in client.get("/revisione?scheda=pratiche").text
    # la casella di team si vede, quella dell'altro team no
    caselle = client.get("/caselle").text
    assert "alfa@latraccia.it" in caselle and "beta@latraccia.it" not in caselle


def test_direzione_vede_tutto_ma_non_configura(client, scenario):
    _login(client, "ceo@latraccia.it")
    assert client.get(f"/pratiche/{scenario['pb'].id}").status_code == 200
    assert "Pratica del team Beta" in client.get("/pratiche").text
    assert client.get("/impostazioni").status_code == 403


def test_operativo_non_conferma_ne_chiude_ma_annota(client, scenario):
    csrf = _login(client, "op@latraccia.it")
    pa = scenario["pa"]
    assert client.post(f"/pratiche/{pa.id}/conferma", data={"csrf_token": csrf}).status_code == 403
    assert client.post(f"/pratiche/{pa.id}/stato", data={"csrf_token": csrf, "stato": "chiusa"}).status_code == 403
    assert client.post(f"/pratiche/{pa.id}/stato", data={"csrf_token": csrf, "stato": "sviluppo"}).status_code == 403
    assert client.get("/pratiche/nuova").status_code == 403
    r = client.post(f"/pratiche/{pa.id}/eventi", data={"csrf_token": csrf, "tipo": "nota", "titolo": "Sentito il cliente"})
    assert r.status_code == 303
    # i pulsanti che non può usare non compaiono
    dettaglio = client.get(f"/pratiche/{pa.id}").text
    assert f'action="/pratiche/{pa.id}/stato"' not in dettaglio and f'action="/pratiche/{pa.id}/modifica"' not in dettaglio


def test_commerciale_cambia_fase_ma_non_chiude(client, scenario):
    csrf = _login(client, "comm@latraccia.it")
    pa = scenario["pa"]
    assert client.post(f"/pratiche/{pa.id}/stato", data={"csrf_token": csrf, "stato": "offerta"}).status_code == 303
    assert client.post(f"/pratiche/{pa.id}/stato", data={"csrf_token": csrf, "stato": "chiusa"}).status_code == 403
    dettaglio = client.get(f"/pratiche/{pa.id}").text
    assert 'value="offerta" checked' in dettaglio and 'value="chiusa"' not in dettaglio


def test_commerciale_crea_nel_suo_team_e_non_in_un_altro(client, scenario, db):
    csrf = _login(client, "comm@latraccia.it")
    nuova = client.get("/pratiche/nuova").text
    assert f'<option value="{scenario["alfa"].id}" selected>Alfa</option>' in nuova  # l'unico team, già proposto
    assert ">Beta</option>" not in nuova
    dati = {"csrf_token": csrf, "titolo": "Nuova di Alfa", "team_id": str(scenario["alfa"].id)}
    r = client.post("/pratiche", data=dati)
    assert r.status_code == 303
    assert client.post("/pratiche", data={**dati, "team_id": str(scenario["beta"].id)}).status_code == 400


def test_amministratore_gestisce_team_e_ruoli(client, scenario, db):
    csrf = _login(client, "admin@latraccia.it")  # in PMO_ADMIN_EMAILS
    assert client.get("/impostazioni?scheda=team").status_code == 200
    r = client.post("/impostazioni/team", data={"csrf_token": csrf, "nome": "Gamma", "nuovo": "1"})
    assert r.status_code == 303
    gamma = db.scalar(select(Team).where(Team.nome == "Gamma"))
    assert gamma is not None and gamma.attivo
    op = scenario["op"]
    r = client.post(
        "/impostazioni/utenti",
        data={"csrf_token": csrf, "user_id": str(op.id), "email": op.email, "nome": "Op", "ruolo": "responsabile", "attivo": "1", "team_ids": [str(scenario["alfa"].id), str(gamma.id)]},
    )
    assert r.status_code == 303
    db.expire_all()
    op = db.get(User, op.id)
    assert op.ruolo == "responsabile" and {t.nome for t in op.teams} == {"Alfa", "Gamma"}
    # un ruolo inventato diventa "operativo"
    client.post("/impostazioni/utenti", data={"csrf_token": csrf, "user_id": str(op.id), "email": op.email, "ruolo": "ceo", "attivo": "1"})
    db.expire_all()
    assert db.get(User, op.id).ruolo == "operativo"


def test_utente_senza_team_vede_l_avviso(client, scenario, db):
    _login(client, "nuovo@latraccia.it")
    assert "Non fai ancora parte di un team" in client.get("/").text
    assert db.scalar(select(User.ruolo).where(User.email == "nuovo@latraccia.it")) == "operativo"


def test_elenco_dei_team_e_dettaglio(client, scenario, db):
    csrf = _login(client, "admin@latraccia.it")
    alfa, beta = scenario["alfa"], scenario["beta"]
    elenco = client.get("/impostazioni?scheda=team")
    assert elenco.status_code == 200
    # l'elenco è una tabella con il collegamento al dettaglio, senza i moduli di modifica di ogni team
    assert f'href="/impostazioni/team/{alfa.id}"' in elenco.text and f'href="/impostazioni/team/{beta.id}"' in elenco.text
    assert f"/impostazioni/team/{alfa.id}/membri" not in elenco.text

    dettaglio = client.get(f"/impostazioni/team/{alfa.id}")
    assert dettaglio.status_code == 200
    for atteso in ("Op", "Comm", "alfa@latraccia.it", "Pratica del team Alfa", "Commerciale"):
        assert atteso in dettaglio.text
    assert "Pratica del team Beta" not in dettaglio.text and "beta@latraccia.it" not in dettaglio.text
    assert client.get("/impostazioni/team/9999").status_code == 404

    # le azioni sui membri e sui dati riportano al dettaglio del team
    ceo = scenario["ceo"]
    r = client.post(f"/impostazioni/team/{alfa.id}/membri", data={"csrf_token": csrf, "user_id": str(ceo.id)})
    assert r.status_code == 303 and r.headers["location"] == f"/impostazioni/team/{alfa.id}"
    r = client.post(f"/impostazioni/team/{alfa.id}/membri/{ceo.id}/rimuovi", data={"csrf_token": csrf})
    assert r.headers["location"] == f"/impostazioni/team/{alfa.id}"
    r = client.post("/impostazioni/team", data={"csrf_token": csrf, "team_id": str(alfa.id), "nome": "Alfa Sanità", "attivo": "1"})
    assert r.headers["location"] == f"/impostazioni/team/{alfa.id}"
    r = client.post("/impostazioni/team", data={"csrf_token": csrf, "nome": "Delta", "nuovo": "1"})
    delta = db.scalar(select(Team).where(Team.nome == "Delta"))
    assert r.headers["location"] == f"/impostazioni/team/{delta.id}"


def test_il_dettaglio_del_team_e_solo_per_chi_gestisce_gli_utenti(client, scenario):
    _login(client, "ceo@latraccia.it")
    assert client.get(f"/impostazioni/team/{scenario['alfa'].id}").status_code == 403


@pytest.mark.parametrize(
    ("valore", "atteso"),
    [
        ("/revisione?scheda=messaggi", "/revisione?scheda=messaggi"),
        ("/solleciti?gravita=critico", "/solleciti?gravita=critico"),
        ("https://sito-esterno.it/", "/predefinito"),
        ("//sito-esterno.it/", "/predefinito"),
        ("/" + chr(92) + "sito-esterno.it/", "/predefinito"),  # barra rovesciata: i browser la leggono come "//"
        ("/revisione\r\nSet-Cookie: x=1", "/predefinito"),
        ("javascript:alert(1)", "/predefinito"),
        ("", "/predefinito"),
        (None, "/predefinito"),
    ],
)
def test_ritorno_sicuro(valore, atteso):
    from pmo.web.deps import ritorno_sicuro

    assert ritorno_sicuro(valore, "/predefinito") == atteso


def test_il_ritorno_dopo_un_azione_non_porta_fuori_dall_applicazione(client, scenario, db):
    csrf = _login(client, "admin@latraccia.it")
    messaggio = db.scalar(select(Messaggio))
    r = client.post(f"/messaggi/{messaggio.id}/ignora", data={"csrf_token": csrf, "ritorno": "https://sito-esterno.it/"})
    assert r.status_code == 303 and r.headers["location"] == "/revisione"
    r = client.post(f"/messaggi/{messaggio.id}/scollega", data={"csrf_token": csrf, "ritorno": "/revisione?scheda=messaggi"})
    assert r.headers["location"] == "/revisione?scheda=messaggi"
    pratica = scenario["pa"]
    r = client.post(f"/pratiche/{pratica.id}/stato", data={"csrf_token": csrf, "stato": "stima", "ritorno": "//sito-esterno.it"})
    assert r.headers["location"] == f"/pratiche/{pratica.id}"
