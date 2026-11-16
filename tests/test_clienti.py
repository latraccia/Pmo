"""Pagina Clienti: anagrafica, referenti, pratiche filtrate e riconoscimento delle mail dai referenti."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from pmo.db.models import Cliente, ContattoCliente, Team, User
from pmo.ingest.catalogo import carica_catalogo
from pmo.services import pratiche as svc


@pytest.fixture
def client(db):
    from pmo.web.app import create_app

    with TestClient(create_app(), follow_redirects=False) as c:
        yield c


def _login(client: TestClient, email: str) -> str:
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/login").text).group(1)
    assert client.post("/auth/dev", data={"email": email, "csrf_token": csrf, "next": "/"}).status_code == 303
    return csrf


def test_creazione_modifica_e_referenti(client, db):
    csrf = _login(client, "admin@latraccia.it")
    r = client.post("/clienti", data={"csrf_token": csrf, "nome": "ASL Bari", "domini": "asl.bari.it", "tipo": "sanita"})
    assert r.status_code == 303
    cliente = db.scalar(select(Cliente).where(Cliente.nome == "ASL Bari"))
    assert r.headers["location"] == f"/clienti/{cliente.id}"
    # un doppione non viene creato
    client.post("/clienti", data={"csrf_token": csrf, "nome": "asl bari"})
    assert len(db.scalars(select(Cliente)).all()) == 1

    r = client.post(
        f"/clienti/{cliente.id}",
        data={"csrf_token": csrf, "nome": "ASL Bari", "ragione_sociale": "Azienda Sanitaria Locale di Bari", "partita_iva": "06534340721", "codice_oracle": "C0042", "codice_vte": "ACC-77", "tipo": "sanita", "domini": "asl.bari.it", "attivo": "1"},
    )
    assert r.status_code == 303
    r = client.post(f"/clienti/{cliente.id}/contatti", data={"csrf_token": csrf, "nome": "Mario Rossi", "ruolo": "RUP", "email": "M.Rossi@Gmail.com", "nuovo": "1"})
    assert r.status_code == 303
    db.expire_all()
    cliente = db.get(Cliente, cliente.id)
    assert cliente.codice_oracle == "C0042" and cliente.partita_iva == "06534340721"
    assert [(k.nome, k.email, k.attivo) for k in cliente.contatti] == [("Mario Rossi", "m.rossi@gmail.com", True)]

    pagina = client.get(f"/clienti/{cliente.id}").text
    assert "Azienda Sanitaria Locale di Bari" in pagina and "Mario Rossi" in pagina
    assert "ASL Bari" in client.get("/clienti?q=0653434").text

    # disattivazione del referente: il modulo senza la spunta "attivo"
    contatto = cliente.contatti[0]
    client.post(f"/clienti/{cliente.id}/contatti", data={"csrf_token": csrf, "contatto_id": str(contatto.id), "nome": "Mario Rossi"})
    db.expire_all()
    assert db.get(ContattoCliente, contatto.id).attivo is False


def test_operativo_consulta_ma_non_modifica_e_vede_solo_le_sue_pratiche(client, db):
    _login(client, "admin@latraccia.it")  # il primo utente diventa amministratore
    client.post("/logout", data={"csrf_token": re.search(r'name="csrf_token" value="([^"]+)"', client.get("/").text).group(1)})
    alfa, beta = Team(nome="Alfa"), Team(nome="Beta")
    cliente = Cliente(nome="Regione Puglia")
    db.add_all([alfa, beta, cliente, User(email="op@latraccia.it", nome="Op", ruolo="operativo", teams=[alfa])])
    db.flush()
    svc.crea_pratica(db, "Portale della Regione", cliente=cliente, team_id=alfa.id)
    svc.crea_pratica(db, "Gara riservata", cliente=cliente, team_id=beta.id)
    db.commit()

    csrf = _login(client, "op@latraccia.it")
    pagina = client.get(f"/clienti/{cliente.id}").text
    assert "Portale della Regione" in pagina and "Gara riservata" not in pagina
    assert f'action="/clienti/{cliente.id}"' not in pagina  # niente modulo di modifica
    assert client.post(f"/clienti/{cliente.id}", data={"csrf_token": csrf, "nome": "X"}).status_code == 403
    assert client.post("/clienti", data={"csrf_token": csrf, "nome": "Nuovo"}).status_code == 403


def test_il_catalogo_riconosce_il_cliente_dall_indirizzo_del_referente(db):
    cliente = Cliente(nome="Ospedale Test", domini=["ospedaletest.it"])
    cliente.contatti.append(ContattoCliente(nome="Dottoressa", email="dott.bianchi@gmail.com"))
    db.add(cliente)
    db.commit()
    catalogo = carica_catalogo(db)
    assert catalogo.cliente_per_dominio("dott.bianchi@gmail.com").nome == "Ospedale Test"
    assert catalogo.cliente_per_dominio("altro@gmail.com") is None
    assert catalogo.cliente_per_dominio("x@ospedaletest.it").nome == "Ospedale Test"
