"""Righe di ordini e offerte lette dai documenti con Claude: coda, applicazione, conferma e proposte di listino.

Il percorso di un'estrazione:

1. un evento "ordine ricevuto" od "offerta inviata/accettata" confermato, che viene da una mail con allegati o da
   un documento Drive, la mette in coda (`accoda_da_evento`);
2. il giro di sincronizzazione scarica i file e li fa leggere a Claude (`pmo.ingest.estrazione_righe`);
3. `applica_risultato` mette le righe lette sull'ordine o sulla revisione, come righe *proposte*: sostituiscono la
   riga "Da dettagliare" e le altre righe proposte, non toccano mai quelle confermate;
4. chi lavora sulla commessa le controlla e le conferma (`conferma_righe`), aggiungendo al listino le voci nuove.

La stessa offerta (o lo stesso ordine) arriva spesso da più fonti: mail d'invio, archivio Drive, protocollo. Se la
destinazione ha già righe lette da un altro documento, la nuova fonte viene registrata ma non riletta.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from pmo.db.base import utcnow
from pmo.db.models import (
    Articolo,
    Estrazione,
    Evento,
    FonteEstrazione,
    Messaggio,
    Ordine,
    RevisioneOfferta,
    RigaOfferta,
    RigaOrdine,
    StatoEstrazione,
    StatoRevisione,
    StatoVerifica,
)
from pmo.services import ordini as ordini_svc
from pmo.services.verifica_ordini import _euro, verifica_ordine

ESTENSIONI_LEGGIBILI = (".pdf", ".docx")
TOLLERANZA_RIGA = Decimal("0.05")  # arrotondamenti dei documenti: 5 centesimi su una riga
TOLLERANZA_TOTALE_PCT = Decimal("0.5")  # e mezzo punto percentuale sul totale


def leggibile(nome: str, mime: str) -> bool:
    nome = (nome or "").lower()
    return mime == "application/pdf" or nome.endswith(ESTENSIONI_LEGGIBILI)


def allegati_leggibili(messaggio: Messaggio) -> list[dict]:
    """Gli allegati della mail che Claude può leggere (PDF, Word), riscaricabili da Gmail."""
    return [a for a in messaggio.attachments or [] if a.get("attachment_id") and leggibile(a.get("filename", ""), a.get("mime_type", ""))]


# --- coda ------------------------------------------------------------------------------------------------------


def _righe_lavorate(righe) -> bool:  # noqa: ANN001
    """Qualcuno ha già scritto o confermato le righe: un documento non le sostituisce."""
    return any(r.stato_verifica != StatoVerifica.PROPOSTO and r.descrizione != ordini_svc.DA_DETTAGLIARE for r in righe)


def _estrazioni_di(session: Session, *, ordine: Ordine | None, revisione: RevisioneOfferta | None) -> list[Estrazione]:
    query = select(Estrazione).order_by(Estrazione.id)
    query = query.where(Estrazione.ordine_id == ordine.id) if ordine is not None else query.where(Estrazione.revisione_offerta_id == revisione.id)
    return list(session.scalars(query))


def accoda(
    session: Session,
    *,
    pratica_id: int,
    ordine: Ordine | None = None,
    revisione: RevisioneOfferta | None = None,
    messaggio: Messaggio | None = None,
    documento_id: int | None = None,
    fonte: str | None = None,
    riferimento_esterno: str = "",
) -> Estrazione | None:
    """Mette in coda la lettura delle righe dal documento d'origine, se serve.

    - la stessa origine per la stessa destinazione non si accoda due volte;
    - se la destinazione ha righe già scritte o confermate, non si legge niente;
    - se ha già righe lette da un'altra fonte (lo stesso ordine o la stessa offerta da protocollo, Drive, mail),
      la nuova fonte si registra come "non riletta", così si vede da dove arriva senza pagare una seconda lettura.
    """
    if (ordine is None) == (revisione is None):
        raise ValueError("Serve un ordine oppure una revisione d'offerta.")
    if messaggio is None and documento_id is None and not riferimento_esterno:
        return None
    if messaggio is not None and not allegati_leggibili(messaggio):
        return None
    fonte = fonte or (FonteEstrazione.MAIL if messaggio is not None else FonteEstrazione.DRIVE)
    esistenti = _estrazioni_di(session, ordine=ordine, revisione=revisione)
    for e in esistenti:
        if (messaggio is not None and e.messaggio_id == messaggio.id) or (documento_id is not None and e.documento_id == documento_id) or (
            riferimento_esterno and e.fonte == fonte and e.riferimento_esterno == riferimento_esterno
        ):
            return e
    destinazione = ordine or revisione
    estrazione = Estrazione(
        pratica_id=pratica_id,
        fonte=fonte,
        messaggio_id=messaggio.id if messaggio is not None else None,
        documento_id=documento_id,
        riferimento_esterno=riferimento_esterno,
        ordine_id=ordine.id if ordine is not None else None,
        revisione_offerta_id=revisione.id if revisione is not None else None,
    )
    letta = next((e for e in esistenti if e.stato == StatoEstrazione.FATTA), None)
    if letta is not None:
        _saltata(estrazione, f"Stesso documento già letto da {_descrivi_fonte(letta)}: righe non rilette.")
    elif _righe_lavorate(destinazione.righe):
        _saltata(estrazione, "Le righe erano già scritte o confermate: il documento non è stato letto.")
    session.add(estrazione)
    session.flush()
    return estrazione


def accoda_da_evento(session: Session, evento: Evento, *, ordine: Ordine | None = None, revisione: RevisioneOfferta | None = None) -> Estrazione | None:
    """L'evento confermato ha creato o ritrovato l'ordine o l'offerta: si legge il documento da cui viene."""
    messaggio = session.get(Messaggio, evento.messaggio_id) if evento.messaggio_id else None
    pratica_id = (ordine.pratica_id if ordine is not None else revisione.offerta.pratica_id)
    if messaggio is not None:
        return accoda(session, pratica_id=pratica_id, ordine=ordine, revisione=revisione, messaggio=messaggio)
    if evento.documento_id:
        return accoda(session, pratica_id=pratica_id, ordine=ordine, revisione=revisione, documento_id=evento.documento_id)
    return None


def _descrivi_fonte(estrazione: Estrazione) -> str:
    if estrazione.fonte == FonteEstrazione.MAIL:
        return "mail"
    if estrazione.fonte == FonteEstrazione.DRIVE:
        return "Drive"
    return f"protocollo {estrazione.riferimento_esterno}".strip()


def _saltata(estrazione: Estrazione, motivo: str) -> None:
    estrazione.stato = StatoEstrazione.SALTATA
    estrazione.errore = ""
    estrazione.risultato = {**(estrazione.risultato or {}), "avvisi": [*(estrazione.risultato or {}).get("avvisi", []), motivo]}
    estrazione.elaborata_at = utcnow()


def gia_letto(session: Session, estrazione: Estrazione, hash_file: set[str]) -> Estrazione | None:
    """Un'altra estrazione ha già letto lo stesso file (stesso hash): la stessa offerta archiviata su Drive e
    protocollata, o lo stesso PDF inoltrato. Restituisce quella, così non si rilegge."""
    if not hash_file:
        return None
    candidate = session.scalars(
        select(Estrazione).where(
            Estrazione.id != estrazione.id,
            Estrazione.stato == StatoEstrazione.FATTA,
            or_(Estrazione.ordine_id.is_not(None), Estrazione.revisione_offerta_id.is_not(None)),
        )
    )
    for altra in candidate:
        if hash_file & {f.get("sha256") for f in altra.file or [] if f.get("usato", True)}:
            return altra
    return None


def riprova(estrazione: Estrazione) -> None:
    """Rimette in coda un'estrazione andata in errore (o saltata), da capo."""
    estrazione.stato = StatoEstrazione.IN_ATTESA
    estrazione.tentativi = 0
    estrazione.errore = ""
    estrazione.risultato = {}


# --- risultato ---------------------------------------------------------------------------------------------------


def _decimale(valore: Any) -> Decimal | None:
    if valore is None or valore == "":
        return None
    try:
        return Decimal(str(valore).replace(",", ".")).quantize(Decimal("0.001"))
    except (InvalidOperation, ValueError):
        return None


def _data(valore: Any) -> date | None:
    try:
        return date.fromisoformat(str(valore)[:10]) if valore else None
    except ValueError:
        return None


def avvisi_risultato(risultato: dict) -> list[str]:
    """Controlli sui numeri letti: ogni riga deve tornare (quantità × prezzo − sconto = importo), la somma delle righe
    deve tornare con il totale del documento, e ogni riga deve avere un prezzo."""
    avvisi: list[str] = []
    somma = Decimal(0)
    for i, riga in enumerate(risultato.get("righe") or [], start=1):
        quantita, prezzo = _decimale(riga.get("quantita")), _decimale(riga.get("prezzo_unitario"))
        sconto, importo = _decimale(riga.get("sconto_pct")) or Decimal(0), _decimale(riga.get("importo"))
        nome = riga.get("codice") or riga.get("descrizione") or f"riga {i}"
        if prezzo is None:
            avvisi.append(f"Riga {i} («{str(nome)[:60]}»): manca il prezzo.")
            continue
        calcolato = ((quantita or Decimal(1)) * prezzo * (Decimal(100) - sconto) / Decimal(100)).quantize(Decimal("0.01"))
        somma += calcolato
        if importo is not None and abs(calcolato - importo) > TOLLERANZA_RIGA:
            avvisi.append(f"Riga {i} («{str(nome)[:60]}»): quantità × prezzo dà {_euro(calcolato)}, il documento dice {_euro(importo)}.")
    totale = _decimale(risultato.get("totale_imponibile"))
    if totale is not None and somma and abs(somma - totale) > max(TOLLERANZA_RIGA, abs(totale) * TOLLERANZA_TOTALE_PCT / 100):
        avvisi.append(f"La somma delle righe ({_euro(somma)}) non torna con il totale del documento ({_euro(totale)}).")
    return avvisi


def _riga_da_risultato(classe, riga: dict, posizione: int, estrazione: Estrazione, articoli: dict[str, Articolo]):  # noqa: ANN001, ANN202
    codice = str(riga.get("codice") or "").strip()[:120]
    articolo = articoli.get(ordini_svc.normalizza_numero(codice)) if codice else None
    return classe(
        posizione=posizione,
        articolo_id=articolo.id if articolo else None,
        prodotto_id=articolo.prodotto_id if articolo else None,
        modulo=articolo.codice if articolo else codice,
        descrizione=str(riga.get("descrizione") or "").strip() or (articolo.descrizione if articolo else ""),
        quantita=_decimale(riga.get("quantita")) if riga.get("quantita") is not None else Decimal(1),
        prezzo_unitario=_decimale(riga.get("prezzo_unitario")),
        sconto_pct=_decimale(riga.get("sconto_pct")),
        aliquota_iva=_decimale(riga.get("aliquota_iva")) if riga.get("aliquota_iva") is not None else (articolo.aliquota_iva if articolo else None),
        stato_verifica=StatoVerifica.PROPOSTO,
        estrazione_id=estrazione.id,
    )


def _revisione_indicata(session: Session, revisione: RevisioneOfferta, numero: Any) -> RevisioneOfferta:
    """Il documento dice "rev. 2": le righe vanno sulla revisione 2 dell'offerta (creata se non c'è)."""
    try:
        numero = int(numero)
    except (TypeError, ValueError):
        return revisione
    if numero < 1 or numero == revisione.numero_revisione:
        return revisione
    offerta = revisione.offerta
    altra = next((r for r in offerta.revisioni if r.numero_revisione == numero), None)
    if altra is None:
        altra = RevisioneOfferta(numero_revisione=numero, stato=StatoRevisione.INVIATA, messaggio_id=revisione.messaggio_id, documento_id=revisione.documento_id)
        offerta.revisioni.append(altra)
        offerta.revisioni.sort(key=lambda r: r.numero_revisione)
        session.flush()
    return altra


def applica_risultato(session: Session, estrazione: Estrazione, risultato: dict) -> None:
    """Mette le righe lette sull'ordine o sulla revisione d'offerta, come righe proposte.

    Sostituisce la riga "Da dettagliare" e le righe ancora proposte; se nel frattempo qualcuno ha scritto o
    confermato delle righe, non tocca niente. Riempie i dati di testata ancora vuoti (numero, data, offerta citata,
    condizioni). Poi ricalcola gli importi della commessa e, per un ordine, rifà la verifica contro l'offerta."""
    avvisi = [*avvisi_risultato(risultato), *(risultato.get("avvisi") or [])]
    risultato = {**risultato, "avvisi": avvisi}
    estrazione.risultato = risultato
    destinazione = estrazione.destinazione
    atteso = "ordine" if estrazione.ordine_id else "offerta"
    if destinazione is None:
        _saltata(estrazione, "L'ordine o l'offerta non c'è più.")
        return
    if risultato.get("tipo") not in (atteso, None):
        _saltata(estrazione, f"Il documento non sembra {'un ordine' if atteso == 'ordine' else 'un offerta'}: righe non applicate.")
        return
    righe_lette = [r for r in risultato.get("righe") or [] if r.get("descrizione") or r.get("codice")]
    if not righe_lette:
        _saltata(estrazione, "Nel documento non ci sono righe da leggere.")
        return
    if isinstance(destinazione, RevisioneOfferta):
        destinazione = _revisione_indicata(session, destinazione, risultato.get("revisione"))
        estrazione.revisione_offerta_id = destinazione.id
        estrazione.revisione_offerta = destinazione
    if _righe_lavorate(destinazione.righe):
        _saltata(estrazione, "Nel frattempo le righe sono state scritte o confermate: quelle lette non sono state applicate.")
        return

    session.flush()
    for riga in list(destinazione.righe):
        ordini_svc.elimina_riga(session, destinazione, riga.id)
    articoli = {ordini_svc.normalizza_numero(a.codice): a for a in session.scalars(select(Articolo))}
    classe = RigaOrdine if isinstance(destinazione, Ordine) else RigaOfferta
    for posizione, riga in enumerate(righe_lette, start=1):
        destinazione.righe.append(_riga_da_risultato(classe, riga, posizione, estrazione, articoli))

    condizioni = str(risultato.get("condizioni") or "").strip()
    if isinstance(destinazione, Ordine):
        numero = str(risultato.get("numero") or "").strip()
        if numero and not destinazione.numero and ordini_svc.ordine_con_numero(session, destinazione.pratica_id, numero, escludi_id=destinazione.id) is None:
            ordini_svc.rinumera_ordine(session, destinazione, numero)
        # Un ordine nato da un evento ha la data della mail: quella del documento è più giusta.
        if _data(risultato.get("data")) and (destinazione.evento_id is not None or destinazione.data_ordine is None):
            destinazione.data_ordine = _data(risultato.get("data"))
        if not destinazione.riferimento_offerta and risultato.get("numero_offerta_citata"):
            destinazione.riferimento_offerta = str(risultato["numero_offerta_citata"]).strip()[:80]
        destinazione.condizioni = destinazione.condizioni or condizioni
    else:
        offerta = destinazione.offerta
        if _data(risultato.get("data")) and (offerta.evento_id is not None or destinazione.data is None):
            destinazione.data = _data(risultato.get("data"))
        destinazione.condizioni = destinazione.condizioni or condizioni
        if not offerta.numero and risultato.get("numero") and ordini_svc.offerta_con_numero(session, str(risultato["numero"])) is None:
            offerta.numero = str(risultato["numero"]).strip()[:80]

    estrazione.stato = StatoEstrazione.FATTA
    estrazione.errore = ""
    estrazione.elaborata_at = utcnow()
    session.flush()
    pratica = estrazione.pratica
    ordini_svc.ricalcola_importi_commessa(session, pratica)
    if isinstance(destinazione, Ordine):
        verifica_ordine(session, destinazione)
    for ordine in pratica.ordini:  # le righe nuove cambiano i residui e i confronti degli altri ordini
        if ordine is not destinazione and ordine.verificato_at is not None:
            verifica_ordine(session, ordine)


# --- conferma e listino ------------------------------------------------------------------------------------------


def conferma_righe(session: Session, contenitore: Ordine | RevisioneOfferta) -> int:
    """Le righe lette e controllate diventano confermate. Restituisce quante erano."""
    proposte = [r for r in contenitore.righe if r.stato_verifica == StatoVerifica.PROPOSTO]
    for riga in proposte:
        riga.stato_verifica = StatoVerifica.CONFERMATO
    session.flush()
    return len(proposte)


def ultima_estrazione(session: Session, contenitore: Ordine | RevisioneOfferta) -> Estrazione | None:
    estrazioni = _estrazioni_di(
        session, ordine=contenitore if isinstance(contenitore, Ordine) else None,
        revisione=contenitore if isinstance(contenitore, RevisioneOfferta) else None,
    )
    fatte = [e for e in estrazioni if e.stato == StatoEstrazione.FATTA]
    return (fatte or estrazioni or [None])[-1]


def estrazioni_di(session: Session, contenitore: Ordine | RevisioneOfferta) -> list[Estrazione]:
    return _estrazioni_di(
        session, ordine=contenitore if isinstance(contenitore, Ordine) else None,
        revisione=contenitore if isinstance(contenitore, RevisioneOfferta) else None,
    )


def proposte_listino(session: Session, righe) -> dict[int, dict]:  # noqa: ANN001
    """Per le righe libere con un codice o una descrizione: l'articolo del listino a cui abbinarle (stesso codice
    scritto in altro modo, o stessa descrizione), oppure la proposta di aggiungere la voce al listino."""
    libere = [r for r in righe if r.articolo_id is None and r.descrizione != ordini_svc.DA_DETTAGLIARE and (r.modulo or r.descrizione)]
    if not libere:
        return {}
    articoli = [a for a in session.scalars(select(Articolo).where(Articolo.attivo.is_(True)))]
    per_codice = {ordini_svc.normalizza_numero(a.codice): a for a in articoli}
    per_descrizione = {ordini_svc.normalizza(a.descrizione): a for a in articoli if a.descrizione}
    proposte: dict[int, dict] = {}
    for riga in libere:
        articolo = per_codice.get(ordini_svc.normalizza_numero(riga.modulo)) if riga.modulo else None
        articolo = articolo or per_descrizione.get(ordini_svc.normalizza(riga.descrizione))
        if articolo is not None:
            proposte[riga.id] = {"abbina": articolo}
        elif riga.modulo:
            proposte[riga.id] = {"nuovo": True}
    return proposte


def abbina_articolo(session: Session, riga: RigaOrdine | RigaOfferta, articolo: Articolo) -> None:
    """La riga è quella voce del listino: codice e prodotto dal listino; descrizione e prezzo restano quelli letti."""
    riga.articolo_id = articolo.id
    riga.modulo = articolo.codice
    riga.prodotto_id = riga.prodotto_id or articolo.prodotto_id
    session.flush()


def articolo_da_riga(session: Session, riga: RigaOrdine | RigaOfferta) -> Articolo:
    """Aggiunge al listino la voce della riga (codice, descrizione, prezzo, IVA) e la abbina. Se il codice c'è già,
    abbina quello invece di farne un doppione."""
    codice = (riga.modulo or "").strip()
    if not codice:
        raise ValueError("La riga non ha un codice da mettere nel listino.")
    articolo = next((a for a in session.scalars(select(Articolo)) if ordini_svc.normalizza_numero(a.codice) == ordini_svc.normalizza_numero(codice)), None)
    if articolo is None:
        articolo = Articolo(
            codice=codice[:60], descrizione=riga.descrizione, prodotto_id=riga.prodotto_id,
            prezzo_listino=riga.prezzo_unitario, aliquota_iva=riga.aliquota_iva, note="Aggiunto da una riga letta da un documento.",
        )
        session.add(articolo)
        session.flush()
    abbina_articolo(session, riga, articolo)
    return articolo


def voci_nuove_al_listino(session: Session, contenitore: Ordine | RevisioneOfferta) -> int:
    """Tutte le righe libere con un codice nuovo vanno nel listino (e quelle con un codice già presente si abbinano)."""
    n = 0
    for riga in contenitore.righe:
        if riga.articolo_id is None and riga.modulo and riga.descrizione != ordini_svc.DA_DETTAGLIARE:
            articolo_da_riga(session, riga)
            n += 1
    return n
