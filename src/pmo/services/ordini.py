"""Offerte, ordini e righe della commessa.

La commessa (nel codice ancora `Pratica`) ha offerte con revisioni e ordini; offerte e ordini sono fatti di righe.
Gli importi della commessa che prima si scrivevano a mano si ricavano dalle righe appena ci sono:
- `importo_ordine` = somma degli ordini confermati e non annullati;
- `importo_offerta` = totale delle revisioni accettate.
Un evento "ordine ricevuto" confermato (dall'IA o a mano) crea un ordine con una riga "Da dettagliare", una volta sola:
rileggere lo stesso ordine aggiorna quello che c'è già.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pmo.db.models import (
    AllocazioneRiga,
    Articolo,
    Cliente,
    Evento,
    Messaggio,
    Offerta,
    Ordine,
    Pratica,
    RevisioneOfferta,
    RigaOfferta,
    RigaOrdine,
    StatoRevisione,
    StatoVerifica,
    TipoEvento,
)

DA_DETTAGLIARE = "Da dettagliare"
CAMPI_RIGA = ("posizione", "articolo_id", "prodotto_id", "modulo", "descrizione", "quantita", "prezzo_unitario", "sconto_pct", "aliquota_iva")


def normalizza(testo: str | None) -> str:
    """Per confrontare moduli, descrizioni e condizioni: minuscole, solo lettere e cifre, spazi singoli."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", (testo or "").lower())).strip()


def normalizza_numero(numero: str | None) -> str:
    """Per confrontare numeri d'ordine e d'offerta: solo lettere e cifre, minuscole. «4500 123456», «4500-123456» e
    «4500123456» sono lo stesso numero."""
    return re.sub(r"[\W_]+", "", (numero or "").lower())


# --- offerte ---------------------------------------------------------------------------------------


def crea_offerta(
    session: Session,
    pratica: Pratica,
    *,
    cliente_id: int | None = None,
    numero: str = "",
    titolo: str = "",
    data: date | None = None,
    condizioni: str = "",
    note: str = "",
    user_id: int | None = None,
) -> Offerta:
    """Una nuova offerta con la sua prima revisione, in bozza e senza righe. Il cliente è obbligatorio: se non è
    indicato vale quello della commessa; una commessa ancora senza cliente prende quello dell'offerta."""
    cliente_id = cliente_obbligatorio(session, pratica, cliente_id)
    offerta = Offerta(
        pratica_id=pratica.id, cliente_id=cliente_id, numero=numero.strip()[:80], titolo=titolo.strip()[:300], note=note,
        created_by_user_id=user_id,
    )
    offerta.revisioni.append(RevisioneOfferta(numero_revisione=1, data=data, condizioni=condizioni, stato=StatoRevisione.BOZZA))
    session.add(offerta)
    session.flush()
    return offerta


def cliente_obbligatorio(session: Session, pratica: Pratica, cliente_id: int | None) -> int:
    """Il cliente di un'offerta: quello indicato o, in mancanza, quello della commessa. Senza nessuno dei due è un
    errore. La commessa senza cliente prende quello dell'offerta."""
    cliente_id = cliente_id or pratica.cliente_id
    if cliente_id is None or session.get(Cliente, cliente_id) is None:
        raise ValueError("Il cliente dell'offerta è obbligatorio.")
    if pratica.cliente_id is None:
        pratica.cliente_id = cliente_id
    return cliente_id


def nuova_revisione(session: Session, offerta: Offerta, *, data: date | None = None) -> RevisioneOfferta:
    """La revisione successiva, con le righe e le condizioni dell'ultima copiate: si modifica quel che cambia."""
    ultima = offerta.ultima_revisione
    revisione = RevisioneOfferta(
        numero_revisione=(ultima.numero_revisione + 1) if ultima else 1,
        data=data or date.today(),
        condizioni=ultima.condizioni if ultima else "",
        stato=StatoRevisione.BOZZA,
    )
    if ultima is not None:
        for riga in ultima.righe:
            revisione.righe.append(RigaOfferta(**{c: getattr(riga, c) for c in CAMPI_RIGA}, stato_verifica=riga.stato_verifica))
    offerta.revisioni.append(revisione)
    session.flush()
    return revisione


def imposta_stato_revisione(session: Session, revisione: RevisioneOfferta, stato: str) -> None:
    """Accettare una revisione rende "superata" quella accettata prima nella stessa offerta."""
    stato = StatoRevisione(stato).value
    era_accettata = revisione.stato == StatoRevisione.ACCETTATA
    if stato == StatoRevisione.ACCETTATA:
        for altra in revisione.offerta.revisioni:
            if altra.id != revisione.id and altra.stato == StatoRevisione.ACCETTATA:
                altra.stato = StatoRevisione.SUPERATA
    revisione.stato = stato
    session.flush()
    ricalcola_importi_commessa(session, revisione.offerta.pratica, azzera_offerta=era_accettata)


def revisioni_accettate(pratica: Pratica) -> list[RevisioneOfferta]:
    return [r for o in pratica.offerte for r in o.revisioni if r.stato == StatoRevisione.ACCETTATA]


def offerta_con_numero(session: Session, numero: str, *, pratica_id: int | None = None) -> Offerta | None:
    """L'offerta con lo stesso numero (senza badare a spazi e segni). Senza `pratica_id` la cerca in tutte le
    commesse: un numero d'offerta non si ripete in azienda, e la stessa offerta arriva da mail, Drive e protocollo."""
    cercato = normalizza_numero(numero)
    if not cercato:
        return None
    query = select(Offerta).where(Offerta.numero != "").order_by(Offerta.id)
    if pratica_id is not None:
        query = query.where(Offerta.pratica_id == pratica_id)
    return next((o for o in session.scalars(query) if normalizza_numero(o.numero) == cercato), None)


def offerta_da_evento(session: Session, pratica: Pratica, evento: Evento) -> tuple[RevisioneOfferta, str | None]:
    """La revisione d'offerta che corrisponde a un evento "offerta inviata/accettata" confermato.

    Una sola offerta per numero, in tutta l'azienda: la stessa offerta letta dalla mail d'invio, dall'archivio
    Drive o dal protocollo diventa una sola offerta con più fonti. Senza numero vale l'offerta già creata da
    questo evento, o l'unica offerta della commessa. Restituisce la revisione e, se l'offerta stava in un'altra
    commessa, la frase che lo dice.

    Gli eventi non creano nuove revisioni (lo stesso documento arriva più volte): l'ultima diventa "inviata",
    oppure "accettata" con un'offerta accettata; una revisione già accettata resta com'è."""
    riferimenti = (evento.dati or {}).get("riferimenti") or {}
    numero = str(riferimenti.get("numero_offerta") or "").strip()
    avviso = None
    offerta = session.scalar(select(Offerta).where(Offerta.evento_id == evento.id))
    if offerta is None and numero:
        offerta = offerta_con_numero(session, numero)
        if offerta is not None and offerta.pratica_id != pratica.id:
            avviso = f"L'offerta {offerta.nome} è già nella commessa {offerta.pratica.codice}: le righe vanno lì."
    if offerta is None and not numero:
        offerte = list(pratica.offerte)
        offerta = offerte[0] if len(offerte) == 1 else None
    if offerta is None:
        offerta = crea_offerta(
            session, pratica, numero=numero, titolo=evento.titolo or "",
            data=evento.data_evento.date() if evento.data_evento else None,
            note=f"Creata dall'evento \"{evento.titolo}\".",
        )
        offerta.evento_id = evento.id
    elif numero and not offerta.numero:
        offerta.numero = numero[:80]
    revisione = offerta.ultima_revisione or nuova_revisione(session, offerta)
    if revisione.messaggio_id is None and evento.messaggio_id:
        revisione.messaggio_id = evento.messaggio_id
    if revisione.documento_id is None and evento.documento_id:
        revisione.documento_id = evento.documento_id
    if revisione.data is None and evento.data_evento:
        revisione.data = evento.data_evento.date()
    # Come per gli ordini: finché nessuno legge le righe, l'importo dell'evento sta su una riga "Da dettagliare",
    # così l'offerta accettata non porta a zero l'importo offerto della commessa.
    if evento.importo is not None and not revisione.righe:
        revisione.righe.append(RigaOfferta(posizione=1, descrizione=DA_DETTAGLIARE, quantita=Decimal(1), prezzo_unitario=evento.importo))
    elif evento.importo is not None and len(revisione.righe) == 1 and revisione.righe[0].descrizione == DA_DETTAGLIARE:
        revisione.righe[0].prezzo_unitario = evento.importo
    session.flush()
    if evento.tipo == TipoEvento.OFFERTA_ACCETTATA and revisione.stato != StatoRevisione.ACCETTATA:
        imposta_stato_revisione(session, revisione, StatoRevisione.ACCETTATA)
    elif revisione.stato == StatoRevisione.BOZZA:
        imposta_stato_revisione(session, revisione, StatoRevisione.INVIATA)
    else:
        ricalcola_importi_commessa(session, offerta.pratica)
    return revisione, avviso


def _intatto(righe) -> bool:  # noqa: ANN001
    """Nessuno ci ha lavorato: solo la riga "Da dettagliare" o righe lette dai documenti e non ancora confermate."""
    return all((r.descrizione == DA_DETTAGLIARE and r.articolo_id is None) or r.stato_verifica == StatoVerifica.PROPOSTO for r in righe)


# --- ordini ----------------------------------------------------------------------------------------


def crea_ordine(
    session: Session,
    pratica: Pratica,
    *,
    numero: str = "",
    data_ordine: date | None = None,
    revisione_offerta_id: int | None = None,
    riferimento_offerta: str = "",
    condizioni: str = "",
    note: str = "",
    stato_verifica: str = StatoVerifica.CONFERMATO,
    chiave: str | None = None,
    user_id: int | None = None,
    messaggio_id: int | None = None,
    documento_id: int | None = None,
    evento_id: int | None = None,
) -> Ordine:
    """Un ordine nuovo. Con un numero, la chiave è quella del numero nella commessa: lo stesso ordine che arriva poi
    per mail viene riconosciuto. Due ordini con lo stesso numero nella stessa commessa non si creano."""
    numero = numero.strip()[:80]
    if numero:
        doppio = ordine_con_numero(session, pratica.id, numero)
        if doppio is not None:
            raise ValueError(f"Nella commessa c'è già {doppio.nome}.")
        chiave = chiave or chiave_ordine(pratica.id, numero)
    ordine = Ordine(
        pratica_id=pratica.id,
        numero=numero,
        data_ordine=data_ordine,
        revisione_offerta_id=revisione_offerta_id,
        riferimento_offerta=riferimento_offerta.strip()[:80],
        condizioni=condizioni,
        note=note,
        stato_verifica=stato_verifica,
        chiave=chiave,
        created_by_user_id=user_id,
        messaggio_id=messaggio_id,
        documento_id=documento_id,
        evento_id=evento_id,
    )
    session.add(ordine)
    session.flush()
    if messaggio_id:
        messaggio = session.get(Messaggio, messaggio_id)
        if messaggio is not None:
            ordine.messaggi.append(messaggio)
    return ordine


REVISIONI_ORDINABILI = (StatoRevisione.INVIATA, StatoRevisione.ACCETTATA)


def quantita_da_ordinare(riga_offerta: RigaOfferta) -> Decimal | None:
    """Quanto della riga d'offerta non è ancora coperto dagli ordini che contano (None se la quantità manca)."""
    if riga_offerta.quantita is None:
        return None
    return Decimal(riga_offerta.quantita) - allocato(riga_offerta)[0]


def ordine_da_revisione(
    session: Session,
    revisione: RevisioneOfferta,
    *,
    numero: str = "",
    data_ordine: date | None = None,
    user_id: int | None = None,
) -> Ordine:
    """Un ordine che riprende la revisione d'offerta: riferimento, condizioni e le righe con la quantità ancora da
    ordinare (le righe già ordinate per intero si saltano). Ogni riga copiata resta abbinata a mano alla sua riga
    d'offerta, così la verifica le confronta sempre giuste anche se poi si cambiano quantità o prezzi."""
    if revisione.stato not in REVISIONI_ORDINABILI:
        raise ValueError("L'ordine si crea da una revisione inviata o accettata.")
    da_copiare: list[tuple[RigaOfferta, Decimal | None]] = []
    for riga in sorted(revisione.righe, key=lambda r: (r.posizione or 0, r.id)):
        quantita = quantita_da_ordinare(riga)
        if quantita is None or quantita > 0:
            da_copiare.append((riga, quantita))
    if not da_copiare:
        raise ValueError("Tutte le righe dell'offerta sono già ordinate." if revisione.righe else "L'offerta non ha righe da ordinare.")
    offerta = revisione.offerta
    ordine = crea_ordine(
        session,
        offerta.pratica,
        numero=numero,
        data_ordine=data_ordine,
        revisione_offerta_id=revisione.id,
        riferimento_offerta=offerta.numero,
        condizioni=revisione.condizioni,
        user_id=user_id,
    )
    for posizione, (origine, quantita) in enumerate(da_copiare, start=1):
        riga = RigaOrdine(
            posizione=posizione,
            articolo_id=origine.articolo_id,
            prodotto_id=origine.prodotto_id,
            modulo=origine.modulo,
            descrizione=origine.descrizione,
            quantita=quantita,
            prezzo_unitario=origine.prezzo_unitario,
            sconto_pct=origine.sconto_pct,
            aliquota_iva=origine.aliquota_iva,
            stato_verifica=StatoVerifica.CONFERMATO,
        )
        riga.allocazioni.append(AllocazioneRiga(riga_offerta=origine, origine="manuale"))
        ordine.righe.append(riga)
    session.flush()
    ricalcola_importi_commessa(session, offerta.pratica)
    return ordine


def chiave_ordine(pratica_id: int, numero: str) -> str | None:
    numero = normalizza_numero(numero)
    return f"pratica:{pratica_id}:ordine:{numero}"[:300] if numero else None


def ordine_con_numero(session: Session, pratica_id: int, numero: str, *, escludi_id: int | None = None) -> Ordine | None:
    """L'ordine della commessa con lo stesso numero (senza badare a maiuscole, spazi e punteggiatura). Vale anche per
    gli ordini scritti a mano o nati dalla migrazione, che possono non avere la chiave."""
    cercato = normalizza_numero(numero)
    if not cercato:
        return None
    candidati = session.scalars(select(Ordine).where(Ordine.pratica_id == pratica_id, Ordine.numero != "")).all()
    return next((o for o in candidati if o.id != escludi_id and normalizza_numero(o.numero) == cercato), None)


def rinumera_ordine(session: Session, ordine: Ordine, numero: str) -> None:
    """Cambia il numero d'ordine, e con lui la chiave. Un numero già usato da un altro ordine della commessa è un
    errore chiaro, non un vincolo violato al salvataggio."""
    numero = numero.strip()[:80]
    if numero == ordine.numero:
        return
    doppio = ordine_con_numero(session, ordine.pratica_id, numero, escludi_id=ordine.id)
    if doppio is not None:
        raise ValueError(f"Nella commessa c'è già {doppio.nome}: scegli un altro numero.")
    ordine.numero = numero
    nuova = chiave_ordine(ordine.pratica_id, numero)
    if nuova is not None:
        ordine.chiave = nuova
    elif ordine.chiave and ordine.chiave.startswith("pratica:"):
        ordine.chiave = f"evento:{ordine.evento_id}" if ordine.evento_id else None
    session.flush()


def ordine_da_evento(session: Session, pratica: Pratica, evento: Evento) -> Ordine:
    """L'ordine che corrisponde a un evento "ordine ricevuto" confermato: lo crea o aggiorna quello già creato.

    Lo stesso ordine letto due volte (la stessa mail, un inoltro, il PDF su Drive) si riconosce dall'evento o dal
    numero d'ordine; l'importo dell'evento aggiorna la riga "Da dettagliare" solo finché nessuno ha scritto le righe.
    """
    riferimenti = (evento.dati or {}).get("riferimenti") or {}
    numero = str(riferimenti.get("numero_ordine") or "").strip()
    chiave = chiave_ordine(pratica.id, numero) or f"evento:{evento.id}"
    ordine = (
        session.scalar(select(Ordine).where(Ordine.evento_id == evento.id))
        or session.scalar(select(Ordine).where(Ordine.chiave == chiave))
        or ordine_con_numero(session, pratica.id, numero)
    )
    importo = evento.importo
    if ordine is None:
        # Il primo ordine di una commessa che aveva già un importo d'ordine scritto a mano lo eredita, se l'evento
        # non ne porta uno: altrimenti l'importo della commessa, che ora è la somma degli ordini, andrebbe a zero.
        if importo is None and pratica.importo_ordine is not None and not pratica.ordini:
            importo = pratica.importo_ordine
        ordine = crea_ordine(
            session,
            pratica,
            numero=numero,
            data_ordine=evento.data_evento.date() if evento.data_evento else date.today(),
            riferimento_offerta=str(riferimenti.get("numero_offerta") or ""),
            chiave=chiave,
            messaggio_id=evento.messaggio_id,
            documento_id=evento.documento_id,
            evento_id=evento.id,
            note=f"Creato dall'evento \"{evento.titolo}\".",
        )
    elif evento.messaggio_id and all(m.id != evento.messaggio_id for m in ordine.messaggi):
        messaggio = session.get(Messaggio, evento.messaggio_id)
        if messaggio is not None:
            ordine.messaggi.append(messaggio)
    if importo is not None:
        solo_da_dettagliare = len(ordine.righe) == 1 and ordine.righe[0].descrizione == DA_DETTAGLIARE
        if not ordine.righe:
            ordine.righe.append(RigaOrdine(posizione=1, descrizione=DA_DETTAGLIARE, quantita=Decimal(1), prezzo_unitario=importo))
        elif solo_da_dettagliare:
            ordine.righe[0].prezzo_unitario = importo
    session.flush()
    ricalcola_importi_commessa(session, pratica)
    return ordine


def evento_rimosso(session: Session, evento: Evento) -> str | None:
    """L'evento "ordine ricevuto" od "offerta" viene eliminato o scartato: l'ordine o l'offerta che aveva creato se
    ne va con lui, se nessuno ci ha ancora lavorato (solo la riga "Da dettagliare" o righe lette dai documenti e non
    confermate); altrimenti resta, staccato dall'evento. Restituisce una frase per l'utente, oppure None se l'evento
    non aveva creato niente."""
    ordini = session.scalars(select(Ordine).where(Ordine.evento_id == evento.id)).all()
    offerte = session.scalars(select(Offerta).where(Offerta.evento_id == evento.id)).all()
    if not ordini and not offerte:
        return None
    pratiche = {o.pratica for o in [*ordini, *offerte]}
    frasi = []
    for ordine in ordini:
        if _intatto(ordine.righe):
            frasi.append(f"eliminato anche {ordine.nome}, creato da questo evento")
            session.delete(ordine)
        else:
            ordine.evento_id = None
            if ordine.chiave == f"evento:{evento.id}":
                ordine.chiave = None
            frasi.append(f"{ordine.nome} resta perché ha righe confermate: eliminalo dalla sua pagina se non serve")
    for offerta in offerte:
        if all(_intatto(r.righe) for r in offerta.revisioni):
            frasi.append(f"eliminata anche l'offerta {offerta.nome}, creata da questo evento")
            session.delete(offerta)
        else:
            offerta.evento_id = None
            frasi.append(f"l'offerta {offerta.nome} resta perché ha righe confermate")
    session.flush()
    for pratica in pratiche:
        ricalcola_importi_commessa(session, pratica, azzera_ordine=bool(ordini), azzera_offerta=bool(offerte))
    return "; ".join(frasi).capitalize() + "."


def collega_messaggi(session: Session, ordine: Ordine, messaggio_ids: list[int]) -> None:
    """Le mail dell'ordine, scelte tra quelle della commessa. Una mail può stare su più ordini."""
    scelti = set(messaggio_ids)
    validi = session.scalars(select(Messaggio).where(Messaggio.pratica_id == ordine.pratica_id, Messaggio.id.in_(scelti or [0]))).all()
    ordine.messaggi = sorted(validi, key=lambda m: m.data)
    session.flush()


# --- righe -----------------------------------------------------------------------------------------


def salva_riga(session: Session, contenitore: RevisioneOfferta | Ordine, riga_id: int | None, campi: dict[str, Any]):  # noqa: ANN201
    """Crea o aggiorna una riga di una revisione d'offerta o di un ordine. Una riga scritta a mano è confermata."""
    classe = RigaOfferta if isinstance(contenitore, RevisioneOfferta) else RigaOrdine
    riga = None
    if riga_id is not None:
        riga = next((r for r in contenitore.righe if r.id == riga_id), None)
        if riga is None:
            raise ValueError("Riga non trovata")
    if riga is None:
        riga = classe()
        contenitore.righe.append(riga)
        if not campi.get("posizione"):
            campi["posizione"] = max((r.posizione or 0 for r in contenitore.righe), default=0) + 1
    vecchio_articolo_id = riga.articolo_id
    for campo in CAMPI_RIGA:
        if campo in campi and (campo != "posizione" or campi[campo]):
            setattr(riga, campo, campi[campo])
    if riga.articolo_id is not None:
        _da_listino(session, riga, vecchio_articolo_id)
    riga.stato_verifica = StatoVerifica.CONFERMATO
    session.flush()
    ricalcola_importi_commessa(session, _commessa(contenitore))
    return riga


def _da_listino(session: Session, riga: RigaOfferta | RigaOrdine, vecchio_articolo_id: int | None) -> None:
    """Completa la riga con i dati dell'articolo del listino. Codice e descrizione sono sempre quelli dell'articolo
    (non si modificano nella riga: per una descrizione diversa si usa una riga libera). Prezzo e IVA si riempiono se
    vuoti o rimasti uguali all'articolo scelto prima: un prezzo trattato a mano resta."""
    articolo = session.get(Articolo, riga.articolo_id)
    if articolo is None:
        riga.articolo_id = None
        return
    vecchio = session.get(Articolo, vecchio_articolo_id) if vecchio_articolo_id and vecchio_articolo_id != articolo.id else None

    def da_aggiornare(valore, valore_vecchio) -> bool:  # noqa: ANN001
        return valore in (None, "") or (vecchio is not None and valore == valore_vecchio)

    riga.modulo = articolo.codice
    riga.descrizione = articolo.descrizione
    if da_aggiornare(riga.prezzo_unitario, vecchio.prezzo_listino if vecchio else None):
        riga.prezzo_unitario = articolo.prezzo_listino
    if da_aggiornare(riga.aliquota_iva, vecchio.aliquota_iva if vecchio else None):
        riga.aliquota_iva = articolo.aliquota_iva
    if riga.prodotto_id is None or (vecchio is not None and riga.prodotto_id == vecchio.prodotto_id):
        riga.prodotto_id = articolo.prodotto_id


def elimina_riga(session: Session, contenitore: RevisioneOfferta | Ordine, riga_id: int) -> None:
    riga = next((r for r in contenitore.righe if r.id == riga_id), None)
    if riga is None:
        raise ValueError("Riga non trovata")
    # Gli abbinamenti spariscono con la riga: si tolgono anche dall'altra parte, così i residui sono subito giusti.
    for allocazione in list(riga.allocazioni):
        for lato in (allocazione.riga_offerta, allocazione.riga_ordine):
            if lato is not None and lato is not riga and allocazione in lato.allocazioni:
                lato.allocazioni.remove(allocazione)
    contenitore.righe.remove(riga)
    session.flush()
    ricalcola_importi_commessa(session, _commessa(contenitore))


def _commessa(contenitore: RevisioneOfferta | Ordine) -> Pratica:
    return contenitore.offerta.pratica if isinstance(contenitore, RevisioneOfferta) else contenitore.pratica


# --- importi e residui -----------------------------------------------------------------------------


def ricalcola_importi_commessa(
    session: Session, pratica: Pratica, *, azzera_ordine: bool = False, azzera_offerta: bool = False
) -> None:
    """Con degli ordini l'importo ordinato è la loro somma; con revisioni accettate l'importo offerto è il loro totale.

    Senza ordini (o senza revisioni accettate) l'importo scritto a mano resta, tranne quando è appena sparito ciò
    da cui era calcolato (`azzera_*`: eliminato l'ultimo ordine, tolta l'ultima accettazione): allora si svuota,
    invece di restare il totale di righe che non ci sono più."""
    session.flush()
    session.refresh(pratica, ["ordini", "offerte"])
    if pratica.ordini:
        pratica.importo_ordine = sum((o.totale for o in pratica.ordini if o.conta), Decimal("0.00"))
    elif azzera_ordine:
        pratica.importo_ordine = None
    accettate = revisioni_accettate(pratica)
    if accettate:
        pratica.importo_offerta = sum((r.totale for r in accettate), Decimal("0.00"))
    elif azzera_offerta:
        pratica.importo_offerta = None
    session.flush()


def importi_calcolati(pratica: Pratica) -> dict[str, bool]:
    """Quali importi della scheda vengono dalle righe e quindi non si scrivono a mano."""
    return {"importo_ordine": bool(pratica.ordini), "importo_offerta": bool(revisioni_accettate(pratica))}


def allocato(riga_offerta: RigaOfferta, *, escludi_ordine_id: int | None = None) -> tuple[Decimal, Decimal]:
    """Quantità e importo della riga d'offerta già coperti dagli ordini che contano (confermati, non annullati)."""
    quantita = importo = Decimal(0)
    for allocazione in riga_offerta.allocazioni:
        if allocazione.riga_ordine is None:
            continue
        ordine = allocazione.riga_ordine.ordine
        if not ordine.conta or ordine.id == escludi_ordine_id:
            continue
        quantita += Decimal(allocazione.quantita or 0)
        importo += Decimal(allocazione.importo or 0)
    return quantita, importo


def residuo_riga(riga_offerta: RigaOfferta) -> Decimal | None:
    if riga_offerta.importo is None:
        return None
    return riga_offerta.importo - allocato(riga_offerta)[1]


def residuo_revisione(revisione: RevisioneOfferta) -> Decimal:
    """Quanto dell'offerta non è ancora ordinato: resta un residuo, senza attività autorizzate."""
    return sum((max(r, Decimal(0)) for r in (residuo_riga(x) for x in revisione.righe) if r is not None), Decimal("0.00"))

