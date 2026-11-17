"""Pagine di offerte e ordini: visibilità per team, permessi per ruolo, deroghe, mail su più ordini, portafoglio."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from pmo.db.models import Account, Cliente, Messaggio, Offerta, Ordine, Team, User
from pmo.services import ordini as svc_ordini
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
    """Team Alfa e Beta; una commessa per team; in Alfa un'offerta accettata da 30.000 € (A, B, C)."""
    alfa, beta = Team(nome="Alfa"), Team(nome="Beta")
    db.add_all([alfa, beta])
    db.flush()
    utenti = {
        "op": User(email="op@latraccia.it", nome="Op", ruolo="operativo", teams=[alfa]),
        "comm": User(email="comm@latraccia.it", nome="Comm", ruolo="commerciale", teams=[alfa]),
        "amm": User(email="amm@latraccia.it", nome="Amm", ruolo="amministrazione"),
    }
    db.add_all(utenti.values())
    cliente = Cliente(nome="ASL Alfa")
    db.add(cliente)
    db.flush()
    pa = svc.crea_pratica(db, "Commessa Alfa", team_id=alfa.id, cliente=cliente)
    pb = svc.crea_pratica(db, "Commessa Beta", team_id=beta.id)
    offerta = svc_ordini.crea_offerta(db, pa, numero="OFF-1")
    for modulo, prezzo in (("A", "10000"), ("B", "12000"), ("C", "8000")):
        svc_ordini.salva_riga(db, offerta.revisioni[0], None, {"modulo": modulo, "descrizione": f"Modulo {modulo}", "quantita": Decimal(1), "prezzo_unitario": Decimal(prezzo)})
    svc_ordini.imposta_stato_revisione(db, offerta.revisioni[0], "accettata")
    ob = svc_ordini.crea_ordine(db, pb, numero="BETA-1")
    casella = Account(email="alfa@latraccia.it", team_id=alfa.id, token_cifrato="x")
    db.add(casella)
    db.flush()
    mail = Messaggio(
        account_id=casella.id, gmail_message_id="g1", gmail_thread_id="t1", subject="Ordine firmato", from_email="x@cliente.it",
        data=datetime.now(UTC), pratica_id=pa.id,
    )
    db.add(mail)
    db.commit()
    return {"pa": pa, "pb": pb, "offerta": offerta, "ob": ob, "mail": mail, "cliente": cliente, **utenti}


def _login(client: TestClient, email: str) -> str:
    client.cookies.clear()  # si cambia utente: nuova sessione
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/login").text).group(1)
    r = client.post("/auth/dev", data={"email": email, "csrf_token": csrf, "next": "/"})
    assert r.status_code == 303
    return csrf


def _id_da(location: str) -> int:
    return int(location.rstrip("/").split("/")[-1].split("?")[0])


def test_commerciale_crea_ordine_parziale_conforme(client, scenario, db):
    csrf = _login(client, "comm@latraccia.it")
    pa = scenario["pa"]
    r = client.post(f"/pratiche/{pa.id}/ordini", data={"csrf_token": csrf, "numero": "4500-1", "data_ordine": "2026-11-10", "riferimento_offerta": "OFF-1"})
    assert r.status_code == 303
    oid = _id_da(r.headers["location"])
    for modulo, prezzo in (("A", "10.000,00"), ("C", "8000")):
        r = client.post(f"/ordini/{oid}/righe", data={"csrf_token": csrf, "modulo": modulo, "descrizione": f"Modulo {modulo}", "quantita": "1", "prezzo_unitario": prezzo})
        assert r.status_code == 303
    pagina = client.get(f"/ordini/{oid}")
    assert pagina.status_code == 200
    assert "Conforme, parziale" in pagina.text and "12.000,00 €" in pagina.text
    db.expire_all()
    assert db.get(Ordine, oid).conformita == "conforme"
    assert db.get(type(pa), pa.id).stato == "ordine"  # come per un ordine arrivato per mail
    # la commessa mostra la scheda Offerte e ordini e il portafoglio l'ordine
    commessa = client.get(f"/pratiche/{pa.id}")
    assert "Offerte e ordini" in commessa.text and "Ordine 4500-1" in commessa.text
    portafoglio = client.get("/ordini")
    assert "Ordine 4500-1" in portafoglio.text and "BETA-1" not in portafoglio.text  # Beta non è visibile
    assert "Ordine 4500-1" not in client.get("/ordini?conformita=scostamenti").text
    assert "Ordine 4500-1" in client.get("/ordini?conformita=conforme").text
    # un secondo ordine con B completa l'offerta: il primo non è più parziale
    r = client.post(f"/pratiche/{pa.id}/ordini", data={"csrf_token": csrf, "numero": "4500-2", "riferimento_offerta": "OFF-1"})
    secondo = _id_da(r.headers["location"])
    client.post(f"/ordini/{secondo}/righe", data={"csrf_token": csrf, "modulo": "B", "descrizione": "Modulo B", "quantita": "1", "prezzo_unitario": "12000"})
    db.expire_all()
    assert db.get(Ordine, oid).parziale is False and db.get(Ordine, secondo).parziale is False
    assert "Conforme, parziale" not in client.get(f"/ordini/{oid}").text


def test_visibilita_per_team(client, scenario):
    _login(client, "op@latraccia.it")
    assert client.get(f"/ordini/{scenario['ob'].id}").status_code == 404
    assert client.get(f"/offerte/{scenario['offerta'].id}").status_code == 200
    _login(client, "amm@latraccia.it")  # l'amministrazione vede tutto
    assert client.get(f"/ordini/{scenario['ob'].id}").status_code == 200


def test_operativo_e_amministrazione_non_modificano(client, scenario):
    rev = scenario["offerta"].revisioni[0]
    for email in ("op@latraccia.it", "amm@latraccia.it"):
        csrf = _login(client, email)
        assert client.post(f"/revisioni/{rev.id}/righe", data={"csrf_token": csrf, "descrizione": "X", "quantita": "1", "prezzo_unitario": "1"}).status_code == 403
        assert client.post(f"/pratiche/{scenario['pa'].id}/ordini", data={"csrf_token": csrf}).status_code == 403
        pagina = client.get(f"/offerte/{scenario['offerta'].id}")
        assert "Nuova revisione" not in pagina.text


def test_deroga_solo_a_chi_puo(client, scenario, db):
    csrf = _login(client, "comm@latraccia.it")
    pa = scenario["pa"]
    oid = _id_da(client.post(f"/pratiche/{pa.id}/ordini", data={"csrf_token": csrf}).headers["location"])
    client.post(f"/ordini/{oid}/righe", data={"csrf_token": csrf, "modulo": "A", "descrizione": "Modulo A", "quantita": "1", "prezzo_unitario": "11000"})
    db.expire_all()
    assert db.get(Ordine, oid).conformita == "scostamenti"
    csrf_amm = _login(client, "amm@latraccia.it")
    assert client.post(f"/ordini/{oid}/deroga", data={"csrf_token": csrf_amm, "motivo": "ok"}).status_code == 403
    csrf = _login(client, "comm@latraccia.it")
    assert "Accetta con deroga" in client.get(f"/ordini/{oid}").text
    client.post(f"/ordini/{oid}/deroga", data={"csrf_token": csrf, "motivo": "Sconto concordato"})
    db.expire_all()
    assert db.get(Ordine, oid).derogato
    assert "Conforme con deroga" in client.get(f"/ordini/{oid}").text


def test_mail_su_due_ordini(client, scenario, db):
    csrf = _login(client, "comm@latraccia.it")
    pa, mail = scenario["pa"], scenario["mail"]
    ids = [_id_da(client.post(f"/pratiche/{pa.id}/ordini", data={"csrf_token": csrf, "numero": n}).headers["location"]) for n in ("X1", "X2")]
    for oid in ids:
        client.post(f"/ordini/{oid}/messaggi", data={"csrf_token": csrf, "messaggio_ids": [str(mail.id)]})
    db.expire_all()
    assert all([m.id for m in db.get(Ordine, oid).messaggi] == [mail.id] for oid in ids)


def test_offerta_nuova_revisione_e_righe(client, scenario, db):
    csrf = _login(client, "comm@latraccia.it")
    offerta = scenario["offerta"]
    r = client.post(f"/offerte/{offerta.id}/revisioni", data={"csrf_token": csrf})
    assert r.headers["location"].endswith("?rev=2")
    pagina = client.get(f"/offerte/{offerta.id}?rev=2")
    assert pagina.status_code == 200 and "Revisione <em>2</em>" in pagina.text and "30.000,00 €" in pagina.text
    nuova = offerta.revisioni[-1]
    db.refresh(offerta)
    nuova = offerta.revisioni[-1]
    r = client.post(f"/revisioni/{nuova.id}/modifica", data={"csrf_token": csrf, "data": "2026-11-12", "stato": "accettata", "condizioni": ""})
    assert r.status_code == 303
    db.expire_all()
    assert [rv.stato for rv in db.get(type(offerta), offerta.id).revisioni] == ["superata", "accettata"]


def test_pagine_dicono_commessa(client, scenario):
    _login(client, "comm@latraccia.it")
    menu = client.get("/").text
    assert ">Commesse<" in menu and ">Ordini<" in menu


def test_listino_in_impostazioni(client, scenario, db):
    from pmo.db.models import Articolo

    csrf = _login(client, "comm@latraccia.it")
    assert "Listino" in client.get("/impostazioni").text  # il commerciale vede solo il listino
    assert client.get("/impostazioni?scheda=regole").status_code == 403
    r = client.post("/impostazioni/listino", data={"csrf_token": csrf, "codice": "MOD-A", "descrizione": "Modulo A", "prezzo_listino": "10.000,00", "aliquota_iva": "22", "nuovo": "1"})
    assert r.status_code == 303
    articolo = db.query(Articolo).filter_by(codice="MOD-A").one()
    assert articolo.prezzo_listino == Decimal("10000.00") and articolo.attivo
    # codice doppio: errore chiaro, nessun duplicato
    client.post("/impostazioni/listino", data={"csrf_token": csrf, "codice": "MOD-A", "nuovo": "1"})
    assert db.query(Articolo).filter_by(codice="MOD-A").count() == 1
    # nelle righe dell'offerta si sceglie l'articolo
    pagina = client.get(f"/offerte/{scenario['offerta'].id}")
    assert 'data-codice="MOD-A"' in pagina.text
    rev = scenario["offerta"].revisioni[0]
    client.post(f"/revisioni/{rev.id}/righe", data={"csrf_token": csrf, "articolo_id": str(articolo.id), "quantita": "1"})
    db.expire_all()
    nuova = [r for r in db.get(type(rev), rev.id).righe if r.articolo_id == articolo.id]
    assert len(nuova) == 1 and nuova[0].descrizione == "Modulo A" and nuova[0].importo == Decimal("10000.00")
    # l'operativo non tocca il listino
    csrf = _login(client, "op@latraccia.it")
    assert client.post("/impostazioni/listino", data={"csrf_token": csrf, "codice": "X"}).status_code == 403


def test_pagina_offerte_e_cliente_obbligatorio(client, scenario, db):
    csrf = _login(client, "comm@latraccia.it")
    assert ">Offerte<" in client.get("/").text  # voce di menu
    elenco = client.get("/offerte")
    assert elenco.status_code == 200 and "OFF-1" in elenco.text and "ASL Alfa" in elenco.text and "18.000" not in elenco.text
    assert "OFF-1" in client.get("/offerte?stato=accettata").text
    assert "OFF-1" not in client.get("/offerte?stato=bozza").text
    # senza cliente la commessa Beta non può avere un'offerta: errore chiaro, nessuna offerta
    csrf_amm = _login(client, "admin@latraccia.it")
    r = client.post(f"/pratiche/{scenario['pb'].id}/offerte", data={"csrf_token": csrf_amm, "numero": "SENZA"})
    assert r.status_code == 303 and db.query(Offerta).filter_by(numero="SENZA").count() == 0
    # con il cliente scelto nasce, e la commessa prende quel cliente
    r = client.post(f"/pratiche/{scenario['pb'].id}/offerte", data={"csrf_token": csrf_amm, "numero": "CON", "cliente_id": str(scenario["cliente"].id)})
    assert r.status_code == 303 and r.headers["location"].startswith("/offerte/")
    db.expire_all()
    assert db.query(Offerta).filter_by(numero="CON").one().cliente_id == scenario["cliente"].id
    # il commerciale di Alfa non vede le offerte della commessa Beta
    _login(client, "comm@latraccia.it")
    assert "CON" not in client.get("/offerte").text
    del csrf


def test_numero_doppio_e_evento_eliminato_dalle_pagine(client, scenario, db):
    from pmo.db.models import Evento, TipoEvento

    csrf = _login(client, "comm@latraccia.it")
    pa = scenario["pa"]
    uno = _id_da(client.post(f"/pratiche/{pa.id}/ordini", data={"csrf_token": csrf, "numero": "N-1"}).headers["location"])
    r = client.post(f"/pratiche/{pa.id}/ordini", data={"csrf_token": csrf, "numero": "n 1"})
    assert r.status_code == 303 and r.headers["location"].endswith("#ordini")  # non creato: messaggio d'errore
    due = _id_da(client.post(f"/pratiche/{pa.id}/ordini", data={"csrf_token": csrf, "numero": "N-2"}).headers["location"])
    r = client.post(f"/ordini/{due}/modifica", data={"csrf_token": csrf, "numero": "N-1", "avanzamento": "aperto", "attesa": "nessuna"})
    assert r.status_code == 303  # niente errore 500
    db.expire_all()
    assert db.get(Ordine, due).numero == "N-2" and db.get(Ordine, uno).numero == "N-1"
    # un "ordine ricevuto" eliminato dalla cronologia porta via l'ordine che aveva creato
    evento = svc.aggiungi_evento(db, db.get(type(pa), pa.id), tipo=TipoEvento.ORDINE_RICEVUTO, titolo="Per errore", importo=9, chiave="k-web")
    db.commit()
    eid = evento.id
    creato = db.query(Ordine).filter_by(evento_id=eid).one().id
    client.post(f"/pratiche/{pa.id}/eventi/{eid}/elimina", data={"csrf_token": csrf})
    db.expire_all()
    assert db.get(Ordine, creato) is None and db.get(Evento, eid) is None


def test_crea_ordine_dall_offerta(client, scenario, db):
    revisione = scenario["offerta"].revisioni[0]
    pa = scenario["pa"]
    csrf = _login(client, "op@latraccia.it")
    assert client.post(f"/revisioni/{revisione.id}/ordine", data={"csrf_token": csrf, "numero": "X"}).status_code == 403
    csrf = _login(client, "comm@latraccia.it")
    assert "Crea l'ordine da questa revisione" in client.get(f"/offerte/{scenario['offerta'].id}").text
    r = client.post(f"/revisioni/{revisione.id}/ordine", data={"csrf_token": csrf, "numero": "4500-9", "data_ordine": "2026-11-16"})
    assert r.status_code == 303
    oid = _id_da(r.headers["location"])
    db.expire_all()
    ordine = db.get(Ordine, oid)
    assert [x.modulo for x in ordine.righe] == ["A", "B", "C"] and ordine.totale == Decimal("30000.00")
    assert ordine.conformita == "conforme" and not ordine.parziale and ordine.riferimento_offerta == "OFF-1"
    assert db.get(type(pa), pa.id).stato == "ordine"
    # tutto ordinato: il modulo sparisce e un nuovo tentativo dà un messaggio, non un ordine vuoto
    assert "Crea l'ordine da questa revisione" not in client.get(f"/offerte/{scenario['offerta'].id}").text
    r = client.post(f"/revisioni/{revisione.id}/ordine", data={"csrf_token": csrf, "numero": "4500-10"}, follow_redirects=True)
    assert "già ordinate" in r.text


def _ordine_con_righe_lette(db, pratica, stato="fatta"):
    """Un ordine con tre righe lette da un documento (una con un codice fuori listino)."""
    from pmo.db.models import Estrazione
    from pmo.services import estrazioni as svc_estr

    ordine = svc_ordini.crea_ordine(db, pratica, numero="LETTO-1", riferimento_offerta="OFF-1")
    estrazione = Estrazione(pratica_id=pratica.id, fonte="mail", ordine_id=ordine.id)
    db.add(estrazione)
    db.flush()
    svc_estr.applica_risultato(db, estrazione, {
        "tipo": "ordine", "righe": [
            {"codice": "", "descrizione": "Modulo A", "quantita": 1, "prezzo_unitario": 10000},
            {"codice": "", "descrizione": "Modulo C", "quantita": 1, "prezzo_unitario": 8000},
            {"codice": "NUOVO-1", "descrizione": "Voce nuova", "quantita": 1, "prezzo_unitario": 50},
        ],
    })
    if stato == "errore":
        estrazione.stato, estrazione.errore = "errore", "PDF illeggibile"
    db.commit()
    return ordine, estrazione


def test_righe_lette_conferma_e_listino(client, scenario, db):
    from pmo.db.models import Articolo

    ordine, estrazione = _ordine_con_righe_lette(db, scenario["pa"])
    csrf = _login(client, "op@latraccia.it")
    pagina = client.get(f"/ordini/{ordine.id}").text
    assert "Righe <em>lette</em> dal documento" in pagina and "Riga letta dal documento, da controllare" in pagina
    assert "Conferma le righe" not in pagina  # l'operativo vede ma non conferma
    assert client.post(f"/ordini/{ordine.id}/righe/conferma", data={"csrf_token": csrf}).status_code == 403
    nuova = ordine.righe[2]
    assert client.post(f"/ordini/{ordine.id}/righe/{nuova.id}/listino", data={"csrf_token": csrf}).status_code == 403

    csrf = _login(client, "comm@latraccia.it")
    pagina = client.get(f"/ordini/{ordine.id}").text
    assert "Conferma le righe (3)" in pagina and "Aggiungi al listino le voci nuove (1)" in pagina and ">Aggiungi al listino<" in pagina
    r = client.post(f"/ordini/{ordine.id}/righe/{nuova.id}/listino", data={"csrf_token": csrf})
    assert r.status_code == 303
    db.expire_all()
    assert db.query(Articolo).filter(Articolo.codice == "NUOVO-1").one().id == db.get(type(nuova), nuova.id).articolo_id
    r = client.post(f"/ordini/{ordine.id}/righe/conferma", data={"csrf_token": csrf}, follow_redirects=True)
    assert "3 righe confermate" in r.text
    db.expire_all()
    ordine = db.get(Ordine, ordine.id)
    assert all(not x.proposta for x in ordine.righe) and ordine.conformita == "scostamenti"  # NUOVO-1 non è nell'offerta
    assert "Conferma le righe" not in client.get(f"/ordini/{ordine.id}").text


def test_letture_non_riuscite_e_rileggi(client, scenario, db):
    _, estrazione = _ordine_con_righe_lette(db, scenario["pa"], stato="errore")
    csrf = _login(client, "comm@latraccia.it")
    pagina = client.get("/revisione?scheda=letture").text
    assert "PDF illeggibile" in pagina and "Rileggi" in pagina
    r = client.post(f"/estrazioni/{estrazione.id}/riprova", data={"csrf_token": csrf})
    assert r.status_code == 303
    db.expire_all()
    assert db.get(type(estrazione), estrazione.id).stato == "in_attesa"
    # un utente di un altro team non la vede
    db.add(User(email="beta@latraccia.it", nome="Beta", ruolo="commerciale", teams=[db.query(Team).filter(Team.nome == "Beta").one()]))
    db.commit()
    csrf = _login(client, "beta@latraccia.it")
    assert client.post(f"/estrazioni/{estrazione.id}/riprova", data={"csrf_token": csrf}).status_code == 404
