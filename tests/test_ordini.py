"""Offerte, ordini e righe; verifica dell'ordine contro l'offerta, residui, deroghe; ordini nati dagli eventi."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from pmo.db.models import (
    Cliente,
    Conformita,
    Evento,
    RigaOrdine,
    StatoPratica,
    StatoRevisione,
    TipoEvento,
    User,
)
from pmo.rules.definitions import assicura_regole
from pmo.services import ordini as svc_ordini
from pmo.services import pratiche as svc
from pmo.services.verifica_ordini import confronto, registra_deroga, verifica_ordine

D = Decimal


def _riga(modulo: str, prezzo: str, quantita: str = "1", **altri) -> dict:
    return {"modulo": modulo, "descrizione": f"Modulo {modulo}", "quantita": D(quantita), "prezzo_unitario": D(prezzo), **altri}


@pytest.fixture
def commessa(db):
    cliente = Cliente(nome="ASL Prova")
    db.add(cliente)
    db.flush()
    return svc.crea_pratica(db, "Commessa di prova", stato=StatoPratica.OFFERTA, cliente=cliente)


@pytest.fixture
def offerta_30k(db, commessa):
    """L'esempio del documento di analisi: offerta da 30.000 € con A 10.000, B 12.000, C 8.000, accettata."""
    offerta = svc_ordini.crea_offerta(db, commessa, numero="OFF-12")
    revisione = offerta.revisioni[0]
    for modulo, prezzo in (("A", "10000"), ("B", "12000"), ("C", "8000")):
        svc_ordini.salva_riga(db, revisione, None, _riga(modulo, prezzo))
    svc_ordini.imposta_stato_revisione(db, revisione, StatoRevisione.ACCETTATA)
    return revisione


def _ordine(db, commessa, righe, **campi):
    ordine = svc_ordini.crea_ordine(db, commessa, **campi)
    for r in righe:
        svc_ordini.salva_riga(db, ordine, None, r)
    return ordine


def test_importi_delle_righe_e_totali(db, commessa):
    offerta = svc_ordini.crea_offerta(db, commessa)
    riga = svc_ordini.salva_riga(db, offerta.revisioni[0], None, _riga("X", "250", "4", sconto_pct=D("10"), aliquota_iva=D("22")))
    assert riga.importo == D("900.00")  # 4 × 250 − 10%, senza IVA
    assert riga.prezzo_netto == D("225.00")
    svc_ordini.salva_riga(db, offerta.revisioni[0], None, {"modulo": "Y", "descrizione": "senza prezzo", "quantita": D(1)})
    assert offerta.revisioni[0].totale == D("900.00")  # una riga senza prezzo non conta


def test_ordine_parziale_conforme_con_residuo(db, commessa, offerta_30k):
    ordine = _ordine(db, commessa, [_riga("A", "10000"), _riga("C", "8000")], numero="ORD-1", riferimento_offerta="off 12")
    esito = verifica_ordine(db, ordine)
    assert ordine.conformita == Conformita.CONFORME and ordine.parziale
    assert esito["scostamenti"] == [] and D(esito["residuo"]) == D("12000.00")
    assert ordine.totale == D("18000.00") and commessa.importo_ordine == D("18000.00")
    assert commessa.importo_offerta == D("30000.00")
    assert ordine.etichetta_conformita == "Conforme, parziale"
    # il confronto affianca le tre righe d'offerta: B resta tutta da ordinare
    righe = {r["offerta"].modulo: r for r in confronto(ordine)["righe"]}
    assert righe["B"]["ordine"] == [] and righe["B"]["residuo"] == D("12000.00")
    assert righe["A"]["residuo"] == D("0.00")
    assert svc_ordini.residuo_revisione(offerta_30k) == D("12000.00")


def test_secondo_ordine_esaurisce_il_residuo(db, commessa, offerta_30k):
    primo = _ordine(db, commessa, [_riga("A", "10000"), _riga("C", "8000")])
    verifica_ordine(db, primo)
    secondo = _ordine(db, commessa, [_riga("B", "12000")])
    verifica_ordine(db, secondo)
    assert secondo.conformita == Conformita.CONFORME and not secondo.parziale
    assert commessa.importo_ordine == D("30000.00")
    # ordinare di nuovo A supera ciò che resta dell'offerta
    terzo = _ordine(db, commessa, [_riga("A", "10000")])
    esito = verifica_ordine(db, terzo)
    assert terzo.conformita == Conformita.SCOSTAMENTI
    assert [s["tipo"] for s in esito["scostamenti"]] == ["quantita"]


def test_riga_non_prevista_e_prezzo_fuori_tolleranza(db, commessa, offerta_30k):
    assicura_regole(db)
    ordine = _ordine(db, commessa, [_riga("A", "10500"), _riga("Z", "500")])
    esito = verifica_ordine(db, ordine)
    tipi = sorted(s["tipo"] for s in esito["scostamenti"])
    assert ordine.conformita == Conformita.SCOSTAMENTI and tipi == ["importo", "perimetro"]
    # entro la tolleranza (1% di 10.000 = 100 €) il prezzo va bene; il primo ordine, annullato, non consuma l'offerta
    ordine.avanzamento = "annullato"
    entro = _ordine(db, commessa, [_riga("A", "10080")])
    verifica_ordine(db, entro)
    assert entro.conformita == Conformita.CONFORME


def test_riferimento_offerta_e_condizioni_diverse(db, commessa, offerta_30k):
    offerta_30k.condizioni = "Pagamento a 60 giorni"
    ordine = _ordine(db, commessa, [_riga("A", "10000")], riferimento_offerta="OFF-99", condizioni="Pagamento a 30 giorni")
    esito = verifica_ordine(db, ordine)
    assert sorted(s["tipo"] for s in esito["scostamenti"]) == ["condizioni", "riferimenti"]


def test_senza_offerta_o_senza_prezzi_non_determinabile(db, commessa):
    ordine = _ordine(db, commessa, [_riga("A", "100")])
    verifica_ordine(db, ordine)
    assert ordine.conformita == Conformita.NON_DETERMINABILE and not ordine.parziale
    vuoto = svc_ordini.crea_ordine(db, commessa)
    esito = verifica_ordine(db, vuoto)
    assert vuoto.conformita == Conformita.NON_DETERMINABILE and "non ha righe" in " ".join(esito["non_determinabile"])


def test_abbinamento_manuale_vale_sull_automatico(db, commessa, offerta_30k):
    ordine = _ordine(db, commessa, [{"modulo": "", "descrizione": "Licenza annuale", "quantita": D(1), "prezzo_unitario": D("12000")}])
    verifica_ordine(db, ordine)
    assert ordine.conformita == Conformita.SCOSTAMENTI  # la descrizione non corrisponde a nessuna riga
    riga_b = next(r for r in offerta_30k.righe if r.modulo == "B")
    from pmo.db.models import AllocazioneRiga

    ordine.righe[0].allocazioni.append(AllocazioneRiga(riga_offerta=riga_b, origine="manuale"))
    verifica_ordine(db, ordine)
    assert ordine.conformita == Conformita.CONFORME and ordine.parziale


def test_deroga_registrata_e_decaduta(db, commessa, offerta_30k):
    utente = User(email="resp@latraccia.it", nome="Resp", ruolo="responsabile")
    db.add(utente)
    db.flush()
    ordine = _ordine(db, commessa, [_riga("A", "11000")])
    verifica_ordine(db, ordine)
    with pytest.raises(ValueError):
        registra_deroga(db, ordine, "  ", utente)
    registra_deroga(db, ordine, "Prezzo concordato al telefono", utente)
    assert ordine.derogato and ordine.etichetta_conformita == "Conforme con deroga"
    # rifare la verifica senza cambiamenti non tocca la deroga
    verifica_ordine(db, ordine)
    assert ordine.derogato
    # cambiano le righe: l'esito cambia e la deroga decade, con una nota in cronologia
    svc_ordini.salva_riga(db, ordine, None, _riga("Z", "50"))
    verifica_ordine(db, ordine)
    assert not ordine.derogato and ordine.deroga_at is None
    assert any("Deroga decaduta" in e.titolo for e in commessa.eventi)


def test_deroga_solo_con_scostamenti(db, commessa, offerta_30k):
    utente = User(email="resp2@latraccia.it", nome="Resp", ruolo="responsabile")
    db.add(utente)
    ordine = _ordine(db, commessa, [_riga("A", "10000")])
    verifica_ordine(db, ordine)
    with pytest.raises(ValueError):
        registra_deroga(db, ordine, "motivo", utente)


def test_nuova_revisione_copia_le_righe_e_supera_la_precedente(db, commessa, offerta_30k):
    offerta = offerta_30k.offerta
    seconda = svc_ordini.nuova_revisione(db, offerta)
    assert seconda.numero_revisione == 2 and len(seconda.righe) == 3 and seconda.totale == D("30000.00")
    svc_ordini.salva_riga(db, seconda, seconda.righe[1].id, {"prezzo_unitario": D("11000")})
    svc_ordini.imposta_stato_revisione(db, seconda, StatoRevisione.ACCETTATA)
    assert offerta_30k.stato == StatoRevisione.SUPERATA
    assert commessa.importo_offerta == D("29000.00")


def test_evento_ordine_crea_un_ordine_una_volta_sola(db, commessa):
    dati = {"riferimenti": {"numero_ordine": "4500-77"}}
    svc.aggiungi_evento(
        db, commessa, tipo=TipoEvento.ORDINE_RICEVUTO, titolo="Ordine", importo=18000, chiave="email:1:a:ordine_ricevuto",
        data_evento=datetime(2026, 10, 1, tzinfo=UTC), dati=dati,
    )
    assert commessa.stato == StatoPratica.ORDINE
    assert len(commessa.ordini) == 1
    ordine = commessa.ordini[0]
    assert ordine.numero == "4500-77" and ordine.righe[0].descrizione == "Da dettagliare" and ordine.totale == D("18000.00")
    # lo stesso ordine da un'altra mail (un inoltro), con lo stesso numero: nessun duplicato
    svc.aggiungi_evento(
        db, commessa, tipo=TipoEvento.ORDINE_RICEVUTO, titolo="Ordine (inoltro)", importo=18500, chiave="email:1:b:ordine_ricevuto", dati=dati,
    )
    db.expire(commessa, ["ordini"])
    assert len(commessa.ordini) == 1 and commessa.ordini[0].totale == D("18500.00")
    assert commessa.importo_ordine == D("18500.00")
    # quando qualcuno ha scritto le righe, l'importo dell'evento non le tocca più
    svc_ordini.salva_riga(db, ordine, None, _riga("A", "100"))
    svc.aggiungi_evento(db, commessa, tipo=TipoEvento.ORDINE_RICEVUTO, titolo="Ancora", importo=1, chiave="k3", dati=dati)
    assert ordine.totale == D("18600.00")


def test_evento_proposto_non_crea_l_ordine_finche_non_e_confermato(db, commessa):
    evento = svc.aggiungi_evento(db, commessa, tipo=TipoEvento.ORDINE_RICEVUTO, titolo="Ordine", importo=500, stato_verifica="proposto")
    assert commessa.ordini == []
    svc.conferma_evento(db, evento, user_id=None)
    db.expire(commessa, ["ordini"])
    assert len(commessa.ordini) == 1 and commessa.importo_ordine == D("500.00")
    assert isinstance(db.get(Evento, evento.id), Evento)


def test_ordine_annullato_non_conta(db, commessa, offerta_30k):
    ordine = _ordine(db, commessa, [_riga("A", "10000")])
    verifica_ordine(db, ordine)
    ordine.avanzamento = "annullato"
    svc_ordini.ricalcola_importi_commessa(db, commessa)
    assert commessa.importo_ordine == D("0.00")
    assert svc_ordini.residuo_revisione(offerta_30k) == D("30000.00")


def test_offerta_senza_cliente_non_si_crea(db):
    senza = svc.crea_pratica(db, "Commessa senza cliente")
    with pytest.raises(ValueError, match="cliente"):
        svc_ordini.crea_offerta(db, senza, numero="X")
    # indicato il cliente, l'offerta nasce e la commessa lo prende
    cliente = Cliente(nome="AOU Nuova")
    db.add(cliente)
    db.flush()
    offerta = svc_ordini.crea_offerta(db, senza, cliente_id=cliente.id)
    assert offerta.cliente_id == cliente.id and senza.cliente_id == cliente.id


def test_cliente_dell_offerta_diverso_dalla_commessa(db, commessa, offerta_30k):
    altro = Cliente(nome="Altro ente")
    db.add(altro)
    db.flush()
    offerta_30k.offerta.cliente_id = altro.id
    db.flush()
    db.expire(offerta_30k.offerta, ["cliente"])
    ordine = _ordine(db, commessa, [_riga("A", "10000")])
    esito = verifica_ordine(db, ordine)
    assert [s["tipo"] for s in esito["scostamenti"]] == ["cliente"]


def test_unione_di_commesse_porta_gli_ordini(db):
    a = svc.crea_pratica(db, "Doppione", stato=StatoPratica.ORDINE)
    b = svc.crea_pratica(db, "Originale", stato=StatoPratica.OFFERTA)
    dati = {"riferimenti": {"numero_ordine": "77"}}
    svc.aggiungi_evento(db, a, tipo=TipoEvento.ORDINE_RICEVUTO, titolo="Ordine", importo=700, chiave="k-a", dati=dati)
    svc.unisci_pratiche(db, a, b)
    db.flush()
    db.expire_all()
    assert len(b.ordini) == 1 and b.ordini[0].chiave == f"pratica:{b.id}:ordine:77" and b.importo_ordine == D("700.00")
    assert db.query(RigaOrdine).count() == 1


def test_regola_ordine_con_scostamenti(db, commessa, offerta_30k):
    from pmo.rules.engine import esegui_regole

    commessa.stato = StatoPratica.ORDINE
    ordine = _ordine(db, commessa, [_riga("Z", "10")], numero="Z1")
    verifica_ordine(db, ordine)
    esegui_regole(db)
    from pmo.db.models import Sollecito

    assert db.query(Sollecito).filter_by(pratica_id=commessa.id, regola="ordine_con_scostamenti").count() == 1


def test_riga_dal_listino_e_abbinamento_per_articolo(db, commessa):
    from pmo.db.models import Articolo

    lic = Articolo(codice="GPD-LIC", descrizione="Licenza Gepadial annuale", prezzo_listino=D("1200"), aliquota_iva=D("22"))
    form = Articolo(codice="FORM-GU", descrizione="Formazione, giornata", prezzo_listino=D("600"), aliquota_iva=D("22"))
    db.add_all([lic, form])
    db.flush()
    offerta = svc_ordini.crea_offerta(db, commessa, numero="OFF-L")
    rev = offerta.revisioni[0]
    # scelto l'articolo, la riga ne prende codice, descrizione, prezzo e IVA
    riga = svc_ordini.salva_riga(db, rev, None, {"articolo_id": lic.id, "quantita": D(2)})
    assert (riga.modulo, riga.descrizione, riga.prezzo_unitario, riga.aliquota_iva) == ("GPD-LIC", "Licenza Gepadial annuale", D("1200"), D("22"))
    assert riga.importo == D("2400.00")
    # codice e descrizione sono quelli del listino anche se il modulo ne manda altri
    svc_ordini.salva_riga(db, rev, riga.id, {"modulo": "ALTRO", "descrizione": "Testo cambiato"})
    assert (riga.modulo, riga.descrizione) == ("GPD-LIC", "Licenza Gepadial annuale")
    # un prezzo trattato a mano resta anche cambiando articolo; codice e descrizione seguono l'articolo
    svc_ordini.salva_riga(db, rev, riga.id, {"prezzo_unitario": D("1100")})
    svc_ordini.salva_riga(db, rev, riga.id, {"articolo_id": form.id})
    assert (riga.modulo, riga.descrizione, riga.prezzo_unitario) == ("FORM-GU", "Formazione, giornata", D("1100"))
    svc_ordini.salva_riga(db, rev, riga.id, {"articolo_id": lic.id, "prezzo_unitario": D("1200")})
    svc_ordini.salva_riga(db, rev, None, {"articolo_id": form.id, "quantita": D(3)})
    svc_ordini.imposta_stato_revisione(db, rev, StatoRevisione.ACCETTATA)
    # nell'ordine con lo stesso articolo l'abbinamento regge; la descrizione è quella del listino
    ordine = svc_ordini.crea_ordine(db, commessa)
    svc_ordini.salva_riga(db, ordine, None, {"articolo_id": lic.id, "descrizione": "Canone software", "quantita": D(2)})
    verifica_ordine(db, ordine)
    assert ordine.conformita == Conformita.CONFORME and ordine.parziale
    assert ordine.righe[0].descrizione == "Licenza Gepadial annuale"
    # tornando a una riga libera, codice e descrizione si scrivono di nuovo a mano
    svc_ordini.salva_riga(db, ordine, ordine.righe[0].id, {"articolo_id": None, "modulo": "X", "descrizione": "Canone software"})
    assert (ordine.righe[0].modulo, ordine.righe[0].descrizione) == ("X", "Canone software")


def test_listino_dal_seed(db, tmp_path):
    from pmo.db.models import Articolo
    from pmo.services.seed import carica_seed

    seed = tmp_path / "seed.yaml"
    seed.write_text(
        "prodotti:\n  - nome: Gepadial\narticoli:\n"
        "  - {codice: GPD-LIC, descrizione: Licenza annuale, prodotto: Gepadial, unita: anno, prezzo: 1200, iva: 22}\n",
        encoding="utf-8",
    )
    assert carica_seed(db, seed)["articoli"] == 1
    assert carica_seed(db, seed)["articoli"] == 0  # idempotente
    articolo = db.query(Articolo).one()
    assert articolo.prodotto.nome == "Gepadial" and articolo.prezzo_listino == D("1200") and articolo.unita == "anno"


def test_ordine_scritto_a_mano_riconosciuto_dalla_mail(db, commessa):
    manuale = svc_ordini.crea_ordine(db, commessa, numero="4500-1")
    svc_ordini.salva_riga(db, manuale, None, _riga("A", "18000"))
    svc.aggiungi_evento(
        db, commessa, tipo=TipoEvento.ORDINE_RICEVUTO, titolo="Ordine", importo=18000, chiave="k-mail",
        dati={"riferimenti": {"numero_ordine": "4500 1"}},
    )
    db.expire(commessa, ["ordini"])
    assert len(commessa.ordini) == 1 and commessa.importo_ordine == D("18000.00")


def test_ordine_senza_chiave_della_migrazione_riconosciuto(db, commessa):
    vecchio = svc_ordini.crea_ordine(db, commessa, numero="ORD-7")
    vecchio.chiave = None  # come gli ordini creati dalla migrazione 0003
    db.flush()
    svc.aggiungi_evento(db, commessa, tipo=TipoEvento.ORDINE_RICEVUTO, titolo="Ordine", importo=5, chiave="k2", dati={"riferimenti": {"numero_ordine": "ord-7"}})
    db.expire(commessa, ["ordini"])
    assert len(commessa.ordini) == 1


def test_numero_d_ordine_doppio(db, commessa):
    svc_ordini.crea_ordine(db, commessa, numero="100")
    with pytest.raises(ValueError, match="c'è già"):
        svc_ordini.crea_ordine(db, commessa, numero=" 100 ")
    altro = svc_ordini.crea_ordine(db, commessa, numero="200")
    with pytest.raises(ValueError, match="c'è già"):
        svc_ordini.rinumera_ordine(db, altro, "100")
    svc_ordini.rinumera_ordine(db, altro, "300")
    assert altro.numero == "300" and altro.chiave == f"pratica:{commessa.id}:ordine:300"
    # spazi e segni dentro il numero non contano: è lo stesso ordine
    svc_ordini.crea_ordine(db, commessa, numero="4500123456")
    for variante in ("4500 123456", "4500-123456", "4500.123.456"):
        with pytest.raises(ValueError, match="c'è già"):
            svc_ordini.crea_ordine(db, commessa, numero=variante)


def test_importi_si_svuotano_quando_sparisce_cio_da_cui_erano_calcolati(db, commessa, offerta_30k):
    ordine = _ordine(db, commessa, [_riga("A", "10000")])
    assert commessa.importo_ordine == D("10000.00") and commessa.importo_offerta == D("30000.00")
    db.delete(ordine)
    svc_ordini.ricalcola_importi_commessa(db, commessa, azzera_ordine=True)
    assert commessa.importo_ordine is None
    svc_ordini.imposta_stato_revisione(db, offerta_30k, StatoRevisione.BOZZA)
    assert commessa.importo_offerta is None
    # senza ordini, un'operazione sull'offerta non tocca l'importo d'ordine scritto a mano
    commessa.importo_ordine = D("500")
    svc_ordini.imposta_stato_revisione(db, offerta_30k, StatoRevisione.ACCETTATA)
    assert commessa.importo_ordine == D("500") and commessa.importo_offerta == D("30000.00")


def test_evento_scartato_porta_via_l_ordine_non_toccato(db, commessa):
    evento = svc.aggiungi_evento(db, commessa, tipo=TipoEvento.ORDINE_RICEVUTO, titolo="Ordine per errore", importo=50000, chiave="k-err")
    assert len(commessa.ordini) == 1 and commessa.importo_ordine == D("50000.00")
    nota = svc.rifiuta_evento(db, evento)
    db.expire(commessa, ["ordini"])
    assert commessa.ordini == [] and commessa.importo_ordine is None and "eliminato" in nota.lower()
    # se qualcuno ha scritto le righe, l'ordine resta (staccato dall'evento)
    evento2 = svc.aggiungi_evento(db, commessa, tipo=TipoEvento.ORDINE_RICEVUTO, titolo="Ordine vero", importo=100, chiave="k-ok")
    ordine = commessa.ordini[0]
    svc_ordini.salva_riga(db, ordine, None, _riga("A", "200"))
    nota = svc.rifiuta_evento(db, evento2)
    db.expire(commessa, ["ordini"])
    assert len(commessa.ordini) == 1 and commessa.ordini[0].evento_id is None and "resta" in nota
