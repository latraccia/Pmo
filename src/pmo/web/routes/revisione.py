"""Coda di revisione: pratiche proposte, messaggi e documenti da verificare, errori."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from pmo.db.models import (
    Documento,
    Estrazione,
    Evento,
    Messaggio,
    Pratica,
    StatoEstrazione,
    StatoMessaggio,
    StatoVerifica,
    User,
)
from pmo.permessi import filtra_elementi, filtra_pratiche
from pmo.web.deps import get_db, pagina_per, render, utente_corrente

router = APIRouter()


PER_PAGINA = {"pratiche": 24, "messaggi": 20, "documenti": 24, "letture": 30, "errori": 30}


@router.get("/revisione")
def revisione(request: Request, scheda: str = "pratiche", session: Session = Depends(get_db, scope="function"), utente: User = Depends(utente_corrente)):
    if scheda not in PER_PAGINA:
        scheda = "pratiche"
    filtri = {
        "pratiche": (Pratica, Pratica.stato_verifica == StatoVerifica.PROPOSTO.value),
        "messaggi": (Messaggio, Messaggio.stato == StatoMessaggio.DA_RIVEDERE.value),
        "documenti": (Documento, Documento.stato == StatoMessaggio.DA_RIVEDERE.value),
        "letture": (Estrazione, Estrazione.stato == StatoEstrazione.ERRORE.value),
        "errori": (Messaggio, Messaggio.stato == StatoMessaggio.ERRORE.value),
    }
    def base(chiave: str, *colonne):  # noqa: ANN002, ANN202
        """La select di una scheda, ristretta a ciò che l'utente può vedere."""
        modello, condizione = filtri[chiave]
        query = select(*colonne).where(condizione)
        if modello is Estrazione:  # le letture si vedono se si vede la loro commessa
            return filtra_pratiche(query.join(Pratica, Estrazione.pratica_id == Pratica.id), utente)
        return filtra_pratiche(query, utente) if modello is Pratica else filtra_elementi(query, utente, modello)

    conteggi = {k: session.scalar(base(k, func.count(m.id))) or 0 for k, (m, _) in filtri.items()}
    pagina = pagina_per(request, conteggi[scheda], PER_PAGINA[scheda])

    pratiche: list = []
    messaggi: list = []
    documenti: list = []
    errori: list = []
    letture: list = []
    if scheda == "pratiche":
        pratiche = session.scalars(
            base("pratiche", Pratica)
            .options(selectinload(Pratica.cliente), selectinload(Pratica.eventi), selectinload(Pratica.messaggi))
            .order_by(Pratica.id.desc())
            .offset(pagina.offset)
            .limit(pagina.per_pagina)
        ).all()
    elif scheda == "messaggi":
        messaggi = session.scalars(
            base("messaggi", Messaggio)
            .options(selectinload(Messaggio.pratica))
            .order_by(Messaggio.data.desc())
            .offset(pagina.offset)
            .limit(pagina.per_pagina)
        ).all()
    elif scheda == "documenti":
        documenti = session.scalars(
            base("documenti", Documento)
            .options(selectinload(Documento.pratica))
            .order_by(Documento.modified_time.desc())
            .offset(pagina.offset)
            .limit(pagina.per_pagina)
        ).all()
    elif scheda == "letture":
        letture = session.scalars(
            base("letture", Estrazione)
            .options(selectinload(Estrazione.pratica))
            .order_by(Estrazione.elaborata_at.desc())
            .offset(pagina.offset)
            .limit(pagina.per_pagina)
        ).all()
    else:
        errori = session.scalars(
            base("errori", Messaggio).order_by(Messaggio.data.desc()).offset(pagina.offset).limit(pagina.per_pagina)
        ).all()
    eventi_per_messaggio: dict[int, list[Evento]] = {}
    if messaggi:
        for e in session.scalars(select(Evento).where(Evento.messaggio_id.in_([m.id for m in messaggi]))).all():
            eventi_per_messaggio.setdefault(e.messaggio_id, []).append(e)
    pratiche_aperte = session.scalars(
        filtra_pratiche(select(Pratica), utente).where(Pratica.stato_verifica == StatoVerifica.CONFERMATO.value, Pratica.stato.notin_(["chiusa", "annullata"])).order_by(Pratica.codice.desc()).limit(300)
    ).all()
    return render(
        request,
        "revisione.html",
        utente,
        scheda=scheda,
        conteggi=conteggi,
        pagina=pagina,
        pratiche=pratiche,
        messaggi=messaggi,
        documenti=documenti,
        errori=errori,
        letture=letture,
        eventi_per_messaggio=eventi_per_messaggio,
        pratiche_aperte=pratiche_aperte,
    )
