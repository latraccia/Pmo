"""Righe di ordini e offerte lette dai documenti: coda, lettura (con un estrattore finto), applicazione, una sola
offerta da più fonti, conferma e proposte di listino."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from pmo.db.models import (
    Account,
    Articolo,
    Cliente,
    Documento,
    Estrazione,
    Messaggio,
    Offerta,
    StatoEstrazione,
    StatoMessaggio,
    StatoPratica,
    StatoRevisione,
    TipoEvento,
)
from pmo.ingest.classifier import ErroreClassificazione, UsoToken
from pmo.ingest.estrattore import DocumentoLetto, RigaLetta
from pmo.ingest.estrazione_righe import Clienti, elabora_estrazioni
from pmo.services import estrazioni as svc_estr
from pmo.services import ordini as svc_ordini
from pmo.services import pratiche as svc

D = Decimal
PDF = b"%PDF-1.4 ordine 4500-9"


class EstrattoreFinto:
    modello = "finto"

    def __init__(self, *risposte):
        self.risposte = list(risposte)
        self.chiamate: list[tuple[str, list[str], str]] = []
        self.uso = UsoToken()

    def estrai(self, destinazione, file, contesto):
        self.chiamate.append((destinazione, [f.nome for f in file], contesto))
        self.uso.chiamate += 1
        risposta = self.risposte.pop(0) if len(self.risposte) > 1 else self.risposte[0]
        if isinstance(risposta, Exception):
            raise risposta
        return risposta


class GmailFinto:
    def __init__(self, allegati: dict[str, bytes]):
        self.allegati = allegati

    def get_attachment(self, message_id, attachment_id):
        return self.allegati[attachment_id]


class DriveFinto:
    def __init__(self, file: dict[str, bytes]):
        self.file = file

    def scarica(self, file_id, mime_type=""):
        return self.file[file_id]


def _clienti(account, settings, gmail=None, drive=None) -> Clienti:
    return Clienti(account, settings, gmail=lambda a: gmail or GmailFinto({}), drive=lambda a: drive or DriveFinto({}))


def _ordine_letto(**altri) -> DocumentoLetto:
    dati = {
        "documento_usato": "ordine.pdf", "tipo": "ordine", "numero": "4500-9", "data": "2026-11-10",
        "numero_offerta_citata": "OFF-7", "condizioni": "Pagamento 60 gg",
        "righe": [
            RigaLetta(codice="GEP-LIC", descrizione="Licenza Gepadial", quantita=1, prezzo_unitario=18000, importo=18000),
            RigaLetta(codice="GEP-AVV", descrizione="Avviamento", quantita=10, prezzo_unitario=650, importo=6500),
            RigaLetta(codice="XYZ-99", descrizione="Voce nuova", quantita=2, prezzo_unitario=100, importo=200),
        ],
        "totale_imponibile": 24700,
    }
    dati.update(altri)
    return DocumentoLetto(**dati)


@pytest.fixture
def mondo(db):
    account = Account(email="comm@latraccia.it", token_cifrato="x")
    cliente = Cliente(nome="ASL Prova")
    db.add_all([account, cliente, Articolo(codice="GEP-LIC", descrizione="Gepadial licenza", prezzo_listino=D(18000), aliquota_iva=D(22)),
                Articolo(codice="GEP AVV", descrizione="Gepadial avviamento", prezzo_listino=D(650))])
    db.flush()
    pratica = svc.crea_pratica(db, "Commessa estrazioni", stato=StatoPratica.OFFERTA, cliente=cliente)
    return {"account": account, "pratica": pratica, "cliente": cliente}


def _mail(db, account, mid="m1", allegati=(("ordine.pdf", "a1"),)) -> Messaggio:
    m = Messaggio(
        account_id=account.id, gmail_message_id=mid, gmail_thread_id="t1", subject="Ordine", from_email="x@cliente.it",
        data=datetime.now(UTC), stato=StatoMessaggio.CLASSIFICATO, has_attachments=bool(allegati),
        attachments=[{"filename": n, "mime_type": "application/pdf", "size": 1000, "attachment_id": a} for n, a in allegati],
    )
    db.add(m)
    db.flush()
    return m


def _evento_ordine(db, pratica, messaggio=None, documento=None, numero="4500-9", chiave="k1"):
    return svc.aggiungi_evento(
        db, pratica, tipo=TipoEvento.ORDINE_RICEVUTO, titolo="Ordine ricevuto", importo=24700, chiave=chiave,
        messaggio_id=messaggio.id if messaggio else None, documento_id=documento.id if documento else None,
        dati={"riferimenti": {"numero_ordine": numero}},
    )


def test_ordine_da_mail_letto_e_applicato(db, settings, mondo):
    acc, pratica = mondo["account"], mondo["pratica"]
    _evento_ordine(db, pratica, _mail(db, acc))
    ordine = pratica.ordini[0]
    assert [r.descrizione for r in ordine.righe] == ["Da dettagliare"]
    estrazione = db.query(Estrazione).one()
    assert estrazione.stato == StatoEstrazione.IN_ATTESA and estrazione.ordine_id == ordine.id and estrazione.fonte == "mail"

    estrattore = EstrattoreFinto(_ordine_letto())
    run = elabora_estrazioni(db, acc, settings, estrattore=estrattore, clienti=_clienti(acc, settings, gmail=GmailFinto({"a1": PDF})))
    assert run.contatori["fatte"] == 1
    db.refresh(ordine)
    assert [(r.modulo, r.stato_verifica) for r in ordine.righe] == [("GEP-LIC", "proposto"), ("GEP AVV", "proposto"), ("XYZ-99", "proposto")]
    assert ordine.righe[0].articolo_id is not None and ordine.righe[1].articolo_id is not None  # "GEP-AVV" = "GEP AVV"
    assert ordine.righe[2].articolo_id is None
    assert ordine.righe[0].aliquota_iva == D(22)  # IVA dal listino quando il documento non la dice
    assert ordine.totale == D("24700.00") and pratica.importo_ordine == D("24700.00")
    assert ordine.riferimento_offerta == "OFF-7" and ordine.condizioni == "Pagamento 60 gg"
    assert str(ordine.data_ordine) == "2026-11-10"  # la data del documento, non quella della mail
    assert ordine.verificato_at is not None  # verifica contro l'offerta già fatta
    assert estrazione.stato == StatoEstrazione.FATTA and estrazione.file[0]["sha256"] and estrazione.avvisi == []
    assert estrattore.chiamate[0][0] == "ordine" and "GEP-LIC" in estrattore.chiamate[0][2]  # il listino va nel contesto

    # rielaborare non rilegge; lo stesso evento confermato di nuovo non accoda un'altra lettura
    assert elabora_estrazioni(db, acc, settings, estrattore=estrattore) is None
    _evento_ordine(db, pratica, db.get(Messaggio, estrazione.messaggio_id), chiave="k1")
    assert db.query(Estrazione).count() == 1


def test_avvisi_e_documento_che_non_e_un_ordine(db, settings, mondo):
    acc, pratica = mondo["account"], mondo["pratica"]
    _evento_ordine(db, pratica, _mail(db, acc))
    letto = _ordine_letto(totale_imponibile=30000)
    letto.righe[1].importo = 6000  # 10 × 650 = 6500
    elabora_estrazioni(db, acc, settings, estrattore=EstrattoreFinto(letto), clienti=_clienti(acc, settings, gmail=GmailFinto({"a1": PDF})))
    estrazione = db.query(Estrazione).one()
    assert any("6.500,00 €" in a for a in estrazione.avvisi) and any("non torna con il totale" in a for a in estrazione.avvisi)

    _evento_ordine(db, pratica, _mail(db, acc, mid="m2", allegati=(("lettera.pdf", "a2"),)), numero="4500-10", chiave="k2")
    elabora_estrazioni(
        db, acc, settings, estrattore=EstrattoreFinto(DocumentoLetto(tipo="altro")), clienti=_clienti(acc, settings, gmail=GmailFinto({"a2": b"%PDF altro"})),
    )
    seconda = db.query(Estrazione).order_by(Estrazione.id.desc()).first()
    assert seconda.stato == StatoEstrazione.SALTATA and "non sembra un ordine" in seconda.avvisi[0]
    assert [r.descrizione for r in seconda.ordine.righe] == ["Da dettagliare"]  # niente applicato


def test_righe_confermate_non_si_toccano(db, settings, mondo):
    acc, pratica = mondo["account"], mondo["pratica"]
    m = _mail(db, acc)
    ordine = svc_ordini.crea_ordine(db, pratica, numero="4500-9")
    svc_ordini.salva_riga(db, ordine, None, {"descrizione": "Scritta a mano", "quantita": D(1), "prezzo_unitario": D(5)})
    _evento_ordine(db, pratica, m)
    estrazione = db.query(Estrazione).one()
    assert estrazione.stato == StatoEstrazione.SALTATA  # righe già scritte: non si legge niente
    assert [r.descrizione for r in ordine.righe] == ["Scritta a mano"]


def test_errori_temporanei_poi_errore_e_riprova(db, settings, mondo):
    acc, pratica = mondo["account"], mondo["pratica"]
    _evento_ordine(db, pratica, _mail(db, acc))
    guasto = EstrattoreFinto(ErroreClassificazione("rete", temporaneo=True))
    clienti = _clienti(acc, settings, gmail=GmailFinto({"a1": PDF}))
    for _ in range(3):
        elabora_estrazioni(db, acc, settings, estrattore=guasto, clienti=clienti)
    estrazione = db.query(Estrazione).one()
    assert estrazione.stato == StatoEstrazione.ERRORE and estrazione.tentativi == 3 and estrazione.errore == "rete"
    svc_estr.riprova(estrazione)
    db.commit()
    elabora_estrazioni(db, acc, settings, estrattore=EstrattoreFinto(_ordine_letto()), clienti=clienti)
    assert estrazione.stato == StatoEstrazione.FATTA


def test_offerta_da_mail_e_da_drive_una_sola(db, settings, mondo):
    """La stessa offerta inviata per mail e archiviata su Drive: una sola offerta, una sola lettura."""
    acc, pratica = mondo["account"], mondo["pratica"]
    m = _mail(db, acc, allegati=(("OFF-7.pdf", "a1"),))
    svc.aggiungi_evento(
        db, pratica, tipo=TipoEvento.OFFERTA_INVIATA, titolo="Offerta inviata", importo=24700, chiave="o1", messaggio_id=m.id,
        dati={"riferimenti": {"numero_offerta": "OFF-7"}},
    )
    assert len(pratica.offerte) == 1
    offerta = pratica.offerte[0]
    revisione = offerta.revisioni[0]
    assert offerta.numero == "OFF-7" and revisione.stato == StatoRevisione.INVIATA and revisione.totale == D("24700.00")
    letto = _ordine_letto(tipo="offerta", numero="OFF-7", documento_usato="OFF-7.pdf")
    estrattore = EstrattoreFinto(letto)
    elabora_estrazioni(db, acc, settings, estrattore=estrattore, clienti=_clienti(acc, settings, gmail=GmailFinto({"a1": PDF})))
    assert [r.modulo for r in revisione.righe] == ["GEP-LIC", "GEP AVV", "XYZ-99"]

    # lo stesso PDF archiviato su Drive (scritto "OFF 7"), accettato: stessa offerta, nessuna seconda lettura
    doc = Documento(account_id=acc.id, drive_file_id="d1", nome="OFF 7.pdf", mime_type="application/pdf", stato=StatoMessaggio.CLASSIFICATO)
    db.add(doc)
    db.flush()
    svc.aggiungi_evento(
        db, pratica, tipo=TipoEvento.OFFERTA_ACCETTATA, titolo="Offerta accettata", chiave="o2", documento_id=doc.id,
        dati={"riferimenti": {"numero_offerta": "OFF 7"}},
    )
    assert db.query(Offerta).count() == 1 and revisione.stato == StatoRevisione.ACCETTATA
    assert pratica.importo_offerta == D("24700.00")
    seconda = db.query(Estrazione).filter(Estrazione.documento_id == doc.id).one()
    assert seconda.stato == StatoEstrazione.SALTATA and "già letto" in seconda.avvisi[0]
    assert len(estrattore.chiamate) == 1


def test_stesso_file_da_due_fonti_non_riletto(db, settings, mondo):
    acc, pratica = mondo["account"], mondo["pratica"]
    m = _mail(db, acc, allegati=(("OFF-8.pdf", "a1"),))
    svc.aggiungi_evento(db, pratica, tipo=TipoEvento.OFFERTA_INVIATA, titolo="Offerta", chiave="o1", messaggio_id=m.id,
                        dati={"riferimenti": {"numero_offerta": "OFF-8"}})
    # un'altra commessa riceve per sbaglio lo stesso PDF come offerta senza numero
    altra = svc.crea_pratica(db, "Altra commessa", cliente=mondo["cliente"])
    doc = Documento(account_id=acc.id, drive_file_id="d9", nome="copia.pdf", mime_type="application/pdf", stato=StatoMessaggio.CLASSIFICATO)
    db.add(doc)
    db.flush()
    svc.aggiungi_evento(db, altra, tipo=TipoEvento.OFFERTA_INVIATA, titolo="Offerta", chiave="o3", documento_id=doc.id)
    estrattore = EstrattoreFinto(_ordine_letto(tipo="offerta", numero="OFF-8"))
    clienti = _clienti(acc, settings, gmail=GmailFinto({"a1": PDF}), drive=DriveFinto({"d9": PDF}))
    elabora_estrazioni(db, acc, settings, estrattore=estrattore, clienti=clienti)
    assert len(estrattore.chiamate) == 1  # stesso hash: il secondo non va a Claude
    saltata = db.query(Estrazione).filter(Estrazione.documento_id == doc.id).one()
    assert saltata.stato == StatoEstrazione.SALTATA and "già letto" in saltata.avvisi[0]


def test_offerta_di_un_altra_commessa_e_revisione_indicata(db, settings, mondo):
    acc, pratica = mondo["account"], mondo["pratica"]
    offerta = svc_ordini.crea_offerta(db, pratica, numero="OFF-2026-31")
    altra = svc.crea_pratica(db, "Altra commessa", cliente=mondo["cliente"])
    m = _mail(db, acc, allegati=(("off.pdf", "a1"),))
    evento = svc.aggiungi_evento(db, altra, tipo=TipoEvento.OFFERTA_INVIATA, titolo="Offerta", chiave="o1", messaggio_id=m.id,
                                 dati={"riferimenti": {"numero_offerta": "off 2026/31"}})
    assert db.query(Offerta).count() == 1 and "già nella commessa" in evento.dati["avviso"]
    elabora_estrazioni(db, acc, settings, estrattore=EstrattoreFinto(_ordine_letto(tipo="offerta", numero="OFF-2026-31", revisione=2)),
                       clienti=_clienti(acc, settings, gmail=GmailFinto({"a1": PDF})))
    db.refresh(offerta)
    assert [r.numero_revisione for r in offerta.revisioni] == [1, 2]
    assert len(offerta.revisioni[1].righe) == 3 and offerta.revisioni[0].righe == []


def test_conferma_listino_e_evento_rifiutato(db, settings, mondo):
    acc, pratica = mondo["account"], mondo["pratica"]
    evento = _evento_ordine(db, pratica, _mail(db, acc))
    elabora_estrazioni(db, acc, settings, estrattore=EstrattoreFinto(_ordine_letto()), clienti=_clienti(acc, settings, gmail=GmailFinto({"a1": PDF})))
    ordine = pratica.ordini[0]
    nuova = ordine.righe[2]
    proposte = svc_estr.proposte_listino(db, ordine.righe)
    assert proposte == {nuova.id: {"nuovo": True}}
    assert svc_estr.voci_nuove_al_listino(db, ordine) == 1
    assert nuova.articolo_id is not None and db.query(Articolo).filter(Articolo.codice == "XYZ-99").count() == 1
    assert svc_estr.voci_nuove_al_listino(db, ordine) == 0  # niente doppioni
    # rifiutare l'evento quando le righe sono solo proposte toglie anche l'ordine
    nota = svc.rifiuta_evento(db, evento)
    db.expire(pratica, ["ordini"])
    assert "eliminato anche" in nota.lower() and pratica.ordini == [] and db.query(Estrazione).count() == 0

    # con le righe confermate l'ordine resta
    evento2 = _evento_ordine(db, pratica, _mail(db, acc, mid="m3", allegati=(("o.pdf", "a3"),)), numero="4500-11", chiave="k9")
    elabora_estrazioni(db, acc, settings, estrattore=EstrattoreFinto(_ordine_letto(numero="4500-11")),
                       clienti=_clienti(acc, settings, gmail=GmailFinto({"a3": b"%PDF diverso"})))
    ordine2 = next(o for o in pratica.ordini if o.numero == "4500-11")
    assert svc_estr.conferma_righe(db, ordine2) == 3 and all(not r.proposta for r in ordine2.righe)
    assert "resta" in svc.rifiuta_evento(db, evento2)


def test_abbina_per_descrizione(db, mondo):
    pratica = mondo["pratica"]
    ordine = svc_ordini.crea_ordine(db, pratica, numero="1")
    riga = svc_ordini.salva_riga(db, ordine, None, {"descrizione": "Gepadial  LICENZA", "quantita": D(1), "prezzo_unitario": D(1)})
    proposta = svc_estr.proposte_listino(db, ordine.righe)[riga.id]
    assert proposta["abbina"].codice == "GEP-LIC"
    svc_estr.abbina_articolo(db, riga, proposta["abbina"])
    assert riga.articolo_id == proposta["abbina"].id and riga.modulo == "GEP-LIC" and riga.descrizione == "Gepadial  LICENZA"


def test_senza_ia_la_coda_resta(db, settings, mondo):
    acc, pratica = mondo["account"], mondo["pratica"]
    _evento_ordine(db, pratica, _mail(db, acc))
    settings.ai_enabled = False
    assert elabora_estrazioni(db, acc, settings) is None
    assert db.query(Estrazione).one().stato == StatoEstrazione.IN_ATTESA


def test_richiesta_a_claude_con_il_pdf(settings):
    """Il PDF va come blocco document, il listino nel system in cache; modello ed effort dalle impostazioni."""
    import base64
    from types import SimpleNamespace

    from pmo.config import Settings
    from pmo.ingest.estrattore import EstrattoreClaude, FileDaLeggere

    class Cliente:
        def __init__(self):
            self.beta = self
            self.messages = self
            self.kwargs = None

        def parse(self, **kwargs):
            self.kwargs = kwargs
            usage = SimpleNamespace(input_tokens=1500, output_tokens=300, cache_read_input_tokens=0, cache_creation_input_tokens=0)
            return SimpleNamespace(stop_reason="end_turn", stop_details=None, usage=usage, parsed_output=_ordine_letto())

    cliente = Cliente()
    impostazioni = Settings(_env_file=None, anthropic_api_key="x", claude_model_estrazione="claude-sonnet-5-5")
    estrattore = EstrattoreClaude(impostazioni, client=cliente)
    letto = estrattore.estrai("ordine", [FileDaLeggere(nome="o.pdf", mime="application/pdf", dati=PDF), FileDaLeggere(nome="c.docx", mime="x", testo="tabella")], "Listino: GEP-LIC")
    assert letto.numero == "4500-9" and estrattore.uso.input == 1500 and estrattore.modello == "claude-sonnet-5-5"
    k = cliente.kwargs
    assert k["model"] == "claude-sonnet-5-5" and k["output_config"] == {"effort": "medium"} and k["output_format"] is DocumentoLetto
    documento, word, richiesta = k["messages"][0]["content"]
    assert documento["type"] == "document" and base64.b64decode(documento["source"]["data"]) == PDF
    assert word["type"] == "text" and "tabella" in word["text"] and "ordine del cliente" in richiesta["text"]
    assert k["system"][1] == {"type": "text", "text": "Listino: GEP-LIC", "cache_control": {"type": "ephemeral"}}


def test_quantita_leggibili():
    from pmo.services.verifica_ordini import quantita_leggibile

    assert [quantita_leggibile(x) for x in (D("1.000"), D("10.000"), D("2.500"), D("0.000"), 3)] == ["1", "10", "2,5", "0", "3"]
