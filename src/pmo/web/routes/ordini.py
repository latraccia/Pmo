"""Offerte e ordini della commessa: righe, verifica contro l'offerta, deroghe, stati, attese e portafoglio.

Un'offerta o un ordine si vede quando si vede la sua commessa (altrimenti 404). Li crea e modifica chi ha
GESTIRE_ORDINI; la deroga richiede APPROVARE_DEROGHE. Ogni modifica a un ordine, o alle righe dell'offerta che
lo riguarda, rifà la verifica: conformità e residuo sono sempre quelli delle righe attuali.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from pmo.db.models import (
    ETICHETTE_ATTESA,
    ETICHETTE_AVANZAMENTO,
    ETICHETTE_CONFORMITA,
    ETICHETTE_REVISIONE,
    FASI_ORDINATE,
    STATI_TERMINALI,
    AllocazioneRiga,
    Articolo,
    Attesa,
    Avanzamento,
    Cliente,
    Messaggio,
    Offerta,
    Ordine,
    Pratica,
    RevisioneOfferta,
    RigaOfferta,
    RigaOrdine,
    StatoPratica,
    StatoVerifica,
    Team,
    User,
)
from pmo.permessi import (
    Azione,
    carica_pratica_visibile,
    filtra_pratiche,
    pratica_visibile,
    team_ids,
    vede_tutto,
)
from pmo.services import ordini as svc
from pmo.services import pratiche as svc_pratiche
from pmo.services.verifica_ordini import confronto, registra_deroga, revisione_di_riferimento, verifica_ordine
from pmo.web.deps import flash, get_db, pagina_per, redirect, render, richiede, utente_corrente, verifica_csrf
from pmo.web.routes.pratiche import _date, _decimal, _intero_o

router = APIRouter()

PER_PAGINA = 30
gestione = richiede(Azione.GESTIRE_ORDINI)
deroghe = richiede(Azione.APPROVARE_DEROGHE)
Db = Annotated[Session, Depends(get_db, scope="function")]


# --- caricamento con visibilità --------------------------------------------------------------------


def _ordine(session: Session, ordine_id: int, utente: User) -> Ordine:
    ordine = session.get(Ordine, ordine_id)
    if ordine is None or not pratica_visibile(session, ordine.pratica, utente):
        raise HTTPException(status_code=404, detail="Ordine non trovato")
    return ordine


def _offerta(session: Session, offerta_id: int, utente: User) -> Offerta:
    offerta = session.get(Offerta, offerta_id)
    if offerta is None or not pratica_visibile(session, offerta.pratica, utente):
        raise HTTPException(status_code=404, detail="Offerta non trovata")
    return offerta


def _revisione(session: Session, revisione_id: int, utente: User) -> RevisioneOfferta:
    revisione = session.get(RevisioneOfferta, revisione_id)
    if revisione is None or not pratica_visibile(session, revisione.offerta.pratica, utente):
        raise HTTPException(status_code=404, detail="Revisione non trovata")
    return revisione


def _campi_riga(
    modulo: str,
    descrizione: str,
    quantita: str,
    prezzo_unitario: str,
    sconto_pct: str,
    aliquota_iva: str,
    prodotto_id: str,
    posizione: str,
    articolo_id: str = "",
) -> dict:
    return {
        "articolo_id": _intero_o(articolo_id, None),
        "modulo": modulo.strip()[:120],
        "descrizione": descrizione.strip(),
        "quantita": _decimal(quantita),
        "prezzo_unitario": _decimal(prezzo_unitario),
        "sconto_pct": _decimal(sconto_pct),
        "aliquota_iva": _decimal(aliquota_iva),
        "prodotto_id": _intero_o(prodotto_id, None),
        "posizione": _intero_o(posizione, None),
    }


def _riverifica(session: Session, pratica: Pratica, *, escludi: Ordine | None = None) -> None:
    """Dopo una modifica all'offerta o a un ordine: rifà la verifica degli ordini della commessa già verificati
    (tranne `escludi`, appena verificato)."""
    session.expire(pratica, ["ordini"])
    for ordine in pratica.ordini:
        if ordine.verificato_at is not None and ordine is not escludi:
            verifica_ordine(session, ordine)


def _clienti(session: Session) -> list[Cliente]:
    return list(session.scalars(select(Cliente).where(Cliente.attivo.is_(True)).order_by(Cliente.nome)).all())


def _teams_visibili(session: Session, utente: User) -> list[Team]:
    query = select(Team).where(Team.attivo.is_(True)).order_by(Team.nome)
    if not vede_tutto(utente):
        query = query.where(Team.id.in_(team_ids(utente) or [0]))
    return list(session.scalars(query).all())


def _articoli(session: Session) -> list[Articolo]:
    """Il listino per la scelta nelle righe: anche gli articoli disattivati, che il modello propone solo se già usati."""
    return list(session.scalars(select(Articolo).order_by(Articolo.codice)).all())


# --- portafoglio -----------------------------------------------------------------------------------


@router.get("/ordini")
def portafoglio(
    request: Request,
    session: Db,
    q: str = "",
    team_id: str = "",
    cliente_id: str = "",
    conformita: str = "",
    avanzamento: str = "",
    attesa: str = "",
    tutti: str = "",
    utente: User = Depends(utente_corrente),
):
    query = filtra_pratiche(select(Ordine).join(Pratica, Ordine.pratica_id == Pratica.id), utente)
    if not tutti:
        query = query.where(
            Ordine.avanzamento.not_in([Avanzamento.CHIUSO.value, Avanzamento.ANNULLATO.value]),
            Pratica.stato.not_in([s.value for s in STATI_TERMINALI]),
        )
    if (t := _intero_o(team_id, None)) is not None:
        query = query.where(Pratica.team_id == t)
    if (c := _intero_o(cliente_id, None)) is not None:
        query = query.where(Pratica.cliente_id == c)
    if conformita == "derogato":
        query = query.where(Ordine.conformita == "scostamenti", Ordine.deroga_at.is_not(None))
    elif conformita in ETICHETTE_CONFORMITA:
        query = query.where(Ordine.conformita == conformita)
        if conformita == "scostamenti":
            query = query.where(Ordine.deroga_at.is_(None))
    if avanzamento in ETICHETTE_AVANZAMENTO:
        query = query.where(Ordine.avanzamento == avanzamento)
    if attesa in ETICHETTE_ATTESA:
        query = query.where(Ordine.attesa == attesa)
    for parola in q.split():
        like = f"%{parola.replace('%', '').replace('_', '')}%"
        query = query.where(or_(Ordine.numero.ilike(like), Pratica.codice.ilike(like), Pratica.titolo.ilike(like)))
    filtrati = query.subquery()
    totale = session.scalar(select(func.count()).select_from(filtrati)) or 0
    netto = RigaOrdine.quantita * RigaOrdine.prezzo_unitario * (100 - func.coalesce(RigaOrdine.sconto_pct, 0)) / 100
    ordinato = session.scalar(
        select(func.coalesce(func.sum(netto), 0))
        .join(Ordine, RigaOrdine.ordine_id == Ordine.id)
        .where(Ordine.id.in_(select(filtrati.c.id)), Ordine.stato_verifica == StatoVerifica.CONFERMATO.value, Ordine.avanzamento != "annullato")
    )
    pagina = pagina_per(request, totale, PER_PAGINA)
    ordini = session.scalars(
        query.options(selectinload(Ordine.righe), selectinload(Ordine.pratica).selectinload(Pratica.cliente))
        .order_by(Ordine.data_ordine.desc().nulls_last(), Ordine.id.desc())
        .offset(pagina.offset)
        .limit(pagina.per_pagina)
    ).all()
    return render(
        request,
        "ordini/elenco.html",
        utente,
        ordini=ordini,
        pagina=pagina,
        ordinato=ordinato,
        filtri={"q": q, "team_id": team_id, "cliente_id": cliente_id, "conformita": conformita, "avanzamento": avanzamento, "attesa": attesa, "tutti": tutti},
        teams=_teams_visibili(session, utente),
        clienti=_clienti(session),
        conformita_opzioni=list(ETICHETTE_CONFORMITA.items()) + [("derogato", "Conforme con deroga")],
        avanzamenti=list(ETICHETTE_AVANZAMENTO.items()),
        attese=list(ETICHETTE_ATTESA.items()),
    )


# --- offerte ---------------------------------------------------------------------------------------


@router.get("/offerte")
def offerte_elenco(
    request: Request,
    session: Db,
    q: str = "",
    team_id: str = "",
    cliente_id: str = "",
    stato: str = "",
    tutte: str = "",
    utente: User = Depends(utente_corrente),
):
    """Tutte le offerte delle commesse visibili. Lo stato è quello della revisione che conta: l'accettata, se c'è,
    altrimenti l'ultima."""
    query = filtra_pratiche(select(Offerta).join(Pratica, Offerta.pratica_id == Pratica.id), utente)
    if not tutte:
        query = query.where(Pratica.stato.not_in([s.value for s in STATI_TERMINALI]))
    if (t := _intero_o(team_id, None)) is not None:
        query = query.where(Pratica.team_id == t)
    if (c := _intero_o(cliente_id, None)) is not None:
        query = query.where(Offerta.cliente_id == c)
    if stato in ETICHETTE_REVISIONE:
        accettata = select(RevisioneOfferta.id).where(RevisioneOfferta.offerta_id == Offerta.id, RevisioneOfferta.stato == "accettata").exists()
        if stato == "accettata":
            query = query.where(accettata)
        else:
            ultima = (
                select(RevisioneOfferta.stato)
                .where(RevisioneOfferta.offerta_id == Offerta.id)
                .order_by(RevisioneOfferta.numero_revisione.desc())
                .limit(1)
                .scalar_subquery()
            )
            query = query.where(~accettata, ultima == stato)
    for parola in q.split():
        like = f"%{parola.replace('%', '').replace('_', '')}%"
        query = query.where(or_(Offerta.numero.ilike(like), Offerta.titolo.ilike(like), Pratica.codice.ilike(like), Pratica.titolo.ilike(like)))
    totale = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    pagina = pagina_per(request, totale, PER_PAGINA)
    offerte = session.scalars(
        query.options(
            selectinload(Offerta.cliente),
            # tutto ciò che serve a revisione corrente, residuo e ordini collegati, caricato in poche query
            selectinload(Offerta.revisioni)
            .selectinload(RevisioneOfferta.righe)
            .selectinload(RigaOfferta.allocazioni)
            .selectinload(AllocazioneRiga.riga_ordine)
            .selectinload(RigaOrdine.ordine),
            selectinload(Offerta.pratica).selectinload(Pratica.ordini).selectinload(Ordine.revisione_offerta),
            selectinload(Offerta.pratica).selectinload(Pratica.offerte).selectinload(Offerta.revisioni),
        )
        .order_by(Offerta.created_at.desc(), Offerta.id.desc())
        .offset(pagina.offset)
        .limit(pagina.per_pagina)
    ).all()
    righe = []
    for o in offerte:
        corrente = o.revisione_accettata or o.ultima_revisione
        righe.append(
            {
                "offerta": o,
                "revisione": corrente,
                "residuo": svc.residuo_revisione(corrente) if corrente is not None and corrente.stato == "accettata" else None,
                "ordini": sum(1 for ordine in o.pratica.ordini if corrente is not None and revisione_di_riferimento(ordine) is corrente),
            }
        )
    return render(
        request,
        "offerte/elenco.html",
        utente,
        righe=righe,
        pagina=pagina,
        filtri={"q": q, "team_id": team_id, "cliente_id": cliente_id, "stato": stato, "tutte": tutte},
        teams=_teams_visibili(session, utente),
        clienti=_clienti(session),
        stati=list(ETICHETTE_REVISIONE.items()),
    )


@router.post("/pratiche/{pratica_id}/offerte", dependencies=[Depends(verifica_csrf)])
def offerta_nuova(
    request: Request,
    pratica_id: int,
    session: Db,
    cliente_id: str = Form(""),
    numero: str = Form(""),
    titolo: str = Form(""),
    data: str = Form(""),
    utente: User = Depends(gestione),
):
    pratica = carica_pratica_visibile(session, pratica_id, utente)
    try:
        offerta = svc.crea_offerta(
            session, pratica, cliente_id=_intero_o(cliente_id, None), numero=numero, titolo=titolo, data=_date(data) or date.today(),
            user_id=utente.id,
        )
    except ValueError as exc:
        flash(request, str(exc), "errore")
        return redirect(f"/pratiche/{pratica.id}#ordini")
    flash(request, "Offerta creata: aggiungi le righe.")
    return redirect(f"/offerte/{offerta.id}")


@router.get("/offerte/{offerta_id}")
def offerta_dettaglio(request: Request, offerta_id: int, session: Db, rev: int = 0, utente: User = Depends(utente_corrente)):
    offerta = _offerta(session, offerta_id, utente)
    revisione = next((r for r in offerta.revisioni if r.numero_revisione == rev), None) or offerta.ultima_revisione
    ordini = [o for o in offerta.pratica.ordini if revisione is not None and revisione_di_riferimento(o) is revisione]
    return render(
        request,
        "offerte/dettaglio.html",
        utente,
        offerta=offerta,
        pratica=offerta.pratica,
        revisione=revisione,
        residui={r.id: svc.residuo_riga(r) for r in revisione.righe} if revisione else {},
        residuo=svc.residuo_revisione(revisione) if revisione else 0,
        ordini=ordini,
        ordinabile=revisione is not None
        and revisione.stato in svc.REVISIONI_ORDINABILI
        and any(q is None or q > 0 for q in map(svc.quantita_da_ordinare, revisione.righe)),
        articoli=_articoli(session),
        clienti=_clienti(session),
        stati_revisione=list(ETICHETTE_REVISIONE.items()),
    )


@router.post("/offerte/{offerta_id}/modifica", dependencies=[Depends(verifica_csrf)])
def offerta_modifica(
    request: Request,
    offerta_id: int,
    session: Db,
    cliente_id: str = Form(""),
    numero: str = Form(""),
    titolo: str = Form(""),
    note: str = Form(""),
    utente: User = Depends(gestione),
):
    offerta = _offerta(session, offerta_id, utente)
    try:
        offerta.cliente_id = svc.cliente_obbligatorio(session, offerta.pratica, _intero_o(cliente_id, None))
    except ValueError as exc:
        flash(request, str(exc), "errore")
        return redirect(f"/offerte/{offerta.id}")
    offerta.numero, offerta.titolo, offerta.note = numero.strip()[:80], titolo.strip()[:300], note
    session.flush()
    session.expire(offerta, ["cliente"])
    _riverifica(session, offerta.pratica)
    flash(request, "Offerta aggiornata.")
    return redirect(f"/offerte/{offerta.id}")


@router.post("/offerte/{offerta_id}/revisioni", dependencies=[Depends(verifica_csrf)])
def offerta_revisione_nuova(request: Request, offerta_id: int, session: Db, utente: User = Depends(gestione)):
    offerta = _offerta(session, offerta_id, utente)
    revisione = svc.nuova_revisione(session, offerta)
    flash(request, f"Revisione {revisione.numero_revisione} creata con le righe della precedente.")
    return redirect(f"/offerte/{offerta.id}?rev={revisione.numero_revisione}")


@router.post("/offerte/{offerta_id}/elimina", dependencies=[Depends(verifica_csrf)])
def offerta_elimina(request: Request, offerta_id: int, session: Db, utente: User = Depends(gestione)):
    offerta = _offerta(session, offerta_id, utente)
    pratica = offerta.pratica
    session.delete(offerta)
    session.flush()
    svc.ricalcola_importi_commessa(session, pratica, azzera_offerta=True)
    _riverifica(session, pratica)
    flash(request, "Offerta eliminata.")
    return redirect(f"/pratiche/{pratica.id}#ordini")


def _ritorno_revisione(revisione: RevisioneOfferta):  # noqa: ANN202
    return redirect(f"/offerte/{revisione.offerta_id}?rev={revisione.numero_revisione}")


@router.post("/revisioni/{revisione_id}/modifica", dependencies=[Depends(verifica_csrf)])
def revisione_modifica(
    request: Request,
    revisione_id: int,
    session: Db,
    data: str = Form(""),
    stato: str = Form(""),
    condizioni: str = Form(""),
    utente: User = Depends(gestione),
):
    revisione = _revisione(session, revisione_id, utente)
    revisione.data = _date(data)
    revisione.condizioni = condizioni
    if stato and stato != revisione.stato:
        if stato not in ETICHETTE_REVISIONE:
            raise HTTPException(status_code=400, detail="Stato non valido")
        svc.imposta_stato_revisione(session, revisione, stato)
    else:
        svc.ricalcola_importi_commessa(session, revisione.offerta.pratica)
    _riverifica(session, revisione.offerta.pratica)
    flash(request, f"Revisione {revisione.numero_revisione} aggiornata.")
    return _ritorno_revisione(revisione)


@router.post("/revisioni/{revisione_id}/righe", dependencies=[Depends(verifica_csrf)])
def revisione_riga(
    request: Request,
    revisione_id: int,
    session: Db,
    riga_id: str = Form(""),
    modulo: str = Form(""),
    descrizione: str = Form(""),
    quantita: str = Form(""),
    prezzo_unitario: str = Form(""),
    sconto_pct: str = Form(""),
    aliquota_iva: str = Form(""),
    prodotto_id: str = Form(""),
    posizione: str = Form(""),
    articolo_id: str = Form(""),
    utente: User = Depends(gestione),
):
    revisione = _revisione(session, revisione_id, utente)
    try:
        svc.salva_riga(
            session, revisione, _intero_o(riga_id, None),
            _campi_riga(modulo, descrizione, quantita, prezzo_unitario, sconto_pct, aliquota_iva, prodotto_id, posizione, articolo_id),
        )
    except ValueError:
        raise HTTPException(status_code=404, detail="Riga non trovata") from None
    _riverifica(session, revisione.offerta.pratica)
    flash(request, "Riga salvata.")
    return _ritorno_revisione(revisione)


@router.post("/revisioni/{revisione_id}/righe/{riga_id}/elimina", dependencies=[Depends(verifica_csrf)])
def revisione_riga_elimina(request: Request, revisione_id: int, riga_id: int, session: Db, utente: User = Depends(gestione)):
    revisione = _revisione(session, revisione_id, utente)
    try:
        svc.elimina_riga(session, revisione, riga_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Riga non trovata") from None
    _riverifica(session, revisione.offerta.pratica)
    flash(request, "Riga eliminata.")
    return _ritorno_revisione(revisione)


# --- ordini ----------------------------------------------------------------------------------------


@router.post("/pratiche/{pratica_id}/ordini", dependencies=[Depends(verifica_csrf)])
def ordine_nuovo(
    request: Request,
    pratica_id: int,
    session: Db,
    numero: str = Form(""),
    data_ordine: str = Form(""),
    riferimento_offerta: str = Form(""),
    utente: User = Depends(gestione),
):
    pratica = carica_pratica_visibile(session, pratica_id, utente)
    try:
        ordine = svc.crea_ordine(
            session, pratica, numero=numero, data_ordine=_date(data_ordine) or date.today(), riferimento_offerta=riferimento_offerta,
            user_id=utente.id,
        )
    except ValueError as exc:
        flash(request, str(exc), "errore")
        return redirect(f"/pratiche/{pratica.id}#ordini")
    _fase_ordine(session, pratica, ordine, utente)
    flash(request, "Ordine creato: aggiungi le righe.")
    return redirect(f"/ordini/{ordine.id}")


def _fase_ordine(session: Session, pratica: Pratica, ordine: Ordine, utente: User) -> None:
    """Come per un ordine arrivato per mail: la commessa passa almeno alla fase "ordine" (mai all'indietro)."""
    if pratica.stato in FASI_ORDINATE and FASI_ORDINATE.index(StatoPratica(pratica.stato)) < FASI_ORDINATE.index(StatoPratica.ORDINE):
        svc_pratiche.cambia_stato(session, pratica, StatoPratica.ORDINE, motivo=f"Registrato {ordine.nome}", user_id=utente.id)


@router.post("/revisioni/{revisione_id}/ordine", dependencies=[Depends(verifica_csrf)])
def ordine_da_revisione(
    request: Request,
    revisione_id: int,
    session: Db,
    numero: str = Form(""),
    data_ordine: str = Form(""),
    utente: User = Depends(gestione),
):
    """L'ordine che riprende la revisione: righe con la quantità ancora da ordinare, da correggere se il cliente ha
    ordinato meno."""
    revisione = _revisione(session, revisione_id, utente)
    pratica = revisione.offerta.pratica
    try:
        ordine = svc.ordine_da_revisione(session, revisione, numero=numero, data_ordine=_date(data_ordine) or date.today(), user_id=utente.id)
    except ValueError as exc:
        flash(request, str(exc), "errore")
        return _ritorno_revisione(revisione)
    verifica_ordine(session, ordine, user_id=utente.id)
    _riverifica(session, pratica, escludi=ordine)
    _fase_ordine(session, pratica, ordine, utente)
    flash(request, f"{ordine.nome} creato dall'offerta con {len(ordine.righe)} righe: togli o correggi quello che il cliente non ha ordinato.")
    return redirect(f"/ordini/{ordine.id}")


@router.get("/ordini/{ordine_id}")
def ordine_dettaglio(request: Request, ordine_id: int, session: Db, utente: User = Depends(utente_corrente)):
    ordine = _ordine(session, ordine_id, utente)
    pratica = ordine.pratica
    revisioni = [r for o in pratica.offerte for r in o.revisioni]
    messaggi = session.scalars(select(Messaggio).where(Messaggio.pratica_id == pratica.id).order_by(Messaggio.data.desc()).limit(200)).all()
    return render(
        request,
        "ordini/dettaglio.html",
        utente,
        ordine=ordine,
        pratica=pratica,
        revisioni=revisioni,
        confronto=confronto(ordine),
        messaggi=messaggi,
        collegati={m.id for m in ordine.messaggi},
        articoli=_articoli(session),
        avanzamenti=list(ETICHETTE_AVANZAMENTO.items()),
        attese=list(ETICHETTE_ATTESA.items()),
    )


@router.post("/ordini/{ordine_id}/modifica", dependencies=[Depends(verifica_csrf)])
def ordine_modifica(
    request: Request,
    ordine_id: int,
    session: Db,
    numero: str = Form(""),
    data_ordine: str = Form(""),
    revisione_offerta_id: str = Form(""),
    riferimento_offerta: str = Form(""),
    condizioni: str = Form(""),
    note: str = Form(""),
    avanzamento: str = Form(Avanzamento.APERTO.value),
    attesa: str = Form(Attesa.NESSUNA.value),
    attesa_motivo: str = Form(""),
    utente: User = Depends(gestione),
):
    ordine = _ordine(session, ordine_id, utente)
    if avanzamento not in ETICHETTE_AVANZAMENTO or attesa not in ETICHETTE_ATTESA:
        raise HTTPException(status_code=400, detail="Stato non valido")
    revisione_id = _intero_o(revisione_offerta_id, None)
    if revisione_id is not None:
        revisione = session.get(RevisioneOfferta, revisione_id)
        if revisione is None or revisione.offerta.pratica_id != ordine.pratica_id:
            raise HTTPException(status_code=400, detail="Revisione d'offerta non valida")
    try:
        svc.rinumera_ordine(session, ordine, numero)
    except ValueError as exc:
        flash(request, str(exc), "errore")
        return redirect(f"/ordini/{ordine.id}")
    ordine.data_ordine = _date(data_ordine)
    ordine.revisione_offerta_id = revisione_id
    ordine.riferimento_offerta = riferimento_offerta.strip()[:80]
    ordine.condizioni = condizioni
    ordine.note = note
    ordine.avanzamento = avanzamento
    if attesa != ordine.attesa:
        ordine.attesa_dal = date.today() if attesa != Attesa.NESSUNA else None
    ordine.attesa = attesa
    ordine.attesa_motivo = attesa_motivo.strip()[:500] if attesa != Attesa.NESSUNA else ""
    session.flush()
    session.expire(ordine, ["revisione_offerta"])
    svc.ricalcola_importi_commessa(session, ordine.pratica)
    verifica_ordine(session, ordine, user_id=utente.id)
    _riverifica(session, ordine.pratica, escludi=ordine)  # un ordine annullato o riaperto cambia i residui degli altri
    flash(request, "Ordine aggiornato.")
    return redirect(f"/ordini/{ordine.id}")


@router.post("/ordini/{ordine_id}/conferma", dependencies=[Depends(verifica_csrf)])
def ordine_conferma(request: Request, ordine_id: int, session: Db, utente: User = Depends(gestione)):
    ordine = _ordine(session, ordine_id, utente)
    ordine.stato_verifica = StatoVerifica.CONFERMATO.value
    svc.ricalcola_importi_commessa(session, ordine.pratica)
    _riverifica(session, ordine.pratica)
    flash(request, "Ordine confermato.")
    return redirect(f"/ordini/{ordine.id}")


@router.post("/ordini/{ordine_id}/elimina", dependencies=[Depends(verifica_csrf)])
def ordine_elimina(request: Request, ordine_id: int, session: Db, utente: User = Depends(gestione)):
    ordine = _ordine(session, ordine_id, utente)
    pratica = ordine.pratica
    session.delete(ordine)
    session.flush()
    svc.ricalcola_importi_commessa(session, pratica, azzera_ordine=True)
    _riverifica(session, pratica)
    flash(request, "Ordine eliminato.")
    return redirect(f"/pratiche/{pratica.id}#ordini")


@router.post("/ordini/{ordine_id}/righe", dependencies=[Depends(verifica_csrf)])
def ordine_riga(
    request: Request,
    ordine_id: int,
    session: Db,
    riga_id: str = Form(""),
    modulo: str = Form(""),
    descrizione: str = Form(""),
    quantita: str = Form(""),
    prezzo_unitario: str = Form(""),
    sconto_pct: str = Form(""),
    aliquota_iva: str = Form(""),
    prodotto_id: str = Form(""),
    posizione: str = Form(""),
    articolo_id: str = Form(""),
    utente: User = Depends(gestione),
):
    ordine = _ordine(session, ordine_id, utente)
    try:
        svc.salva_riga(
            session, ordine, _intero_o(riga_id, None),
            _campi_riga(modulo, descrizione, quantita, prezzo_unitario, sconto_pct, aliquota_iva, prodotto_id, posizione, articolo_id),
        )
    except ValueError:
        raise HTTPException(status_code=404, detail="Riga non trovata") from None
    verifica_ordine(session, ordine, user_id=utente.id)
    _riverifica(session, ordine.pratica, escludi=ordine)  # le righe di un ordine cambiano i residui degli altri
    flash(request, "Riga salvata e ordine verificato.")
    return redirect(f"/ordini/{ordine.id}")


@router.post("/ordini/{ordine_id}/righe/{riga_id}/elimina", dependencies=[Depends(verifica_csrf)])
def ordine_riga_elimina(request: Request, ordine_id: int, riga_id: int, session: Db, utente: User = Depends(gestione)):
    ordine = _ordine(session, ordine_id, utente)
    try:
        svc.elimina_riga(session, ordine, riga_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Riga non trovata") from None
    verifica_ordine(session, ordine, user_id=utente.id)
    _riverifica(session, ordine.pratica, escludi=ordine)  # le righe di un ordine cambiano i residui degli altri
    flash(request, "Riga eliminata.")
    return redirect(f"/ordini/{ordine.id}")


@router.post("/ordini/{ordine_id}/righe/{riga_id}/abbina", dependencies=[Depends(verifica_csrf)])
def ordine_riga_abbina(
    request: Request, ordine_id: int, riga_id: int, session: Db, riga_offerta_id: str = Form(""), utente: User = Depends(gestione)
):
    """Sceglie a mano la riga d'offerta che una riga d'ordine copre; vuoto torna all'abbinamento automatico."""
    ordine = _ordine(session, ordine_id, utente)
    riga = next((r for r in ordine.righe if r.id == riga_id), None)
    if riga is None:
        raise HTTPException(status_code=404, detail="Riga non trovata")
    for allocazione in list(riga.allocazioni):
        riga.allocazioni.remove(allocazione)
        if allocazione in allocazione.riga_offerta.allocazioni:
            allocazione.riga_offerta.allocazioni.remove(allocazione)
    scelta = _intero_o(riga_offerta_id, None)
    if scelta is not None:
        revisione = revisione_di_riferimento(ordine)
        riga_offerta = next((r for r in revisione.righe if r.id == scelta), None) if revisione else None
        if riga_offerta is None:
            raise HTTPException(status_code=400, detail="Riga d'offerta non valida")
        riga.allocazioni.append(AllocazioneRiga(riga_offerta=riga_offerta, origine="manuale"))
    session.flush()
    verifica_ordine(session, ordine, user_id=utente.id)
    _riverifica(session, ordine.pratica, escludi=ordine)  # le righe di un ordine cambiano i residui degli altri
    flash(request, "Abbinamento aggiornato e ordine verificato.")
    return redirect(f"/ordini/{ordine.id}")


@router.post("/ordini/{ordine_id}/verifica", dependencies=[Depends(verifica_csrf)])
def ordine_verifica(request: Request, ordine_id: int, session: Db, utente: User = Depends(gestione)):
    ordine = _ordine(session, ordine_id, utente)
    verifica_ordine(session, ordine, user_id=utente.id)
    flash(request, f"Verifica: {ordine.etichetta_conformita.lower()}.")
    return redirect(f"/ordini/{ordine.id}")


@router.post("/ordini/{ordine_id}/deroga", dependencies=[Depends(verifica_csrf)])
def ordine_deroga(request: Request, ordine_id: int, session: Db, motivo: str = Form(""), utente: User = Depends(deroghe)):
    ordine = _ordine(session, ordine_id, utente)
    try:
        registra_deroga(session, ordine, motivo, utente)
    except ValueError as exc:
        flash(request, str(exc), "errore")
        return redirect(f"/ordini/{ordine.id}")
    flash(request, "Deroga registrata: l'ordine è accettato con i suoi scostamenti.")
    return redirect(f"/ordini/{ordine.id}")


@router.post("/ordini/{ordine_id}/messaggi", dependencies=[Depends(verifica_csrf)])
def ordine_messaggi(request: Request, ordine_id: int, session: Db, messaggio_ids: list[int] = Form([]), utente: User = Depends(gestione)):
    ordine = _ordine(session, ordine_id, utente)
    svc.collega_messaggi(session, ordine, messaggio_ids)
    flash(request, f"Mail dell'ordine aggiornate ({len(ordine.messaggi)}).")
    return redirect(f"/ordini/{ordine.id}")

