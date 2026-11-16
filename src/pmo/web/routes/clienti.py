"""Clienti: anagrafica, contatti e pratiche di ciascun cliente.

Tutti consultano i clienti; li crea e li modifica chi ha il permesso GESTIRE_CLIENTI. Le pratiche mostrate nel
dettaglio sono solo quelle che l'utente può vedere.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from pmo.db.models import STATI_TERMINALI, Cliente, ContattoCliente, Pratica, StatoVerifica, Team, User
from pmo.permessi import Azione, filtra_pratiche
from pmo.web.deps import flash, get_db, pagina_per, redirect, render, richiede, utente_corrente, verifica_csrf

router = APIRouter()

PER_PAGINA = 30
TIPI_CLIENTE: list[tuple[str, str]] = [
    ("sanita", "Sanità"),
    ("pa", "Pubblica amministrazione"),
    ("partner", "Partner"),
    ("altro", "Altro"),
]
CAMPI_ANAGRAFICA = (
    "ragione_sociale",
    "partita_iva",
    "codice_fiscale",
    "indirizzo",
    "citta",
    "telefono",
    "email",
    "pec",
    "codice_oracle",
    "codice_vte",
)
gestione_clienti = richiede(Azione.GESTIRE_CLIENTI)


def _elenco(testo: str) -> list[str]:
    return [v.strip() for v in testo.replace("\n", ",").split(",") if v.strip()]


def _carica(session: Session, cliente_id: int) -> Cliente:
    cliente = session.get(Cliente, cliente_id)
    if cliente is None:
        raise HTTPException(status_code=404, detail="Cliente non trovato")
    return cliente


def _like(testo: str) -> str:
    return "%" + testo.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


@router.get("/clienti")
def elenco(request: Request, q: str = "", tutti: int = 0, session: Session = Depends(get_db, scope="function"), utente: User = Depends(utente_corrente)):
    query = select(Cliente)
    if not tutti:
        query = query.where(Cliente.attivo.is_(True))
    for parola in q.split():
        like = _like(parola)
        query = query.where(
            or_(
                Cliente.nome.ilike(like, escape="\\"),
                Cliente.ragione_sociale.ilike(like, escape="\\"),
                Cliente.partita_iva.ilike(like, escape="\\"),
                Cliente.citta.ilike(like, escape="\\"),
                Cliente.codice_oracle.ilike(like, escape="\\"),
                Cliente.codice_vte.ilike(like, escape="\\"),
            )
        )
    totale = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    pagina = pagina_per(request, totale, PER_PAGINA)
    clienti = session.scalars(
        query.options(selectinload(Cliente.team), selectinload(Cliente.contatti)).order_by(Cliente.nome).offset(pagina.offset).limit(pagina.per_pagina)
    ).all()
    # Pratiche attive visibili all'utente, per cliente.
    terminali = [s.value for s in STATI_TERMINALI]
    attive = dict(
        session.execute(
            filtra_pratiche(
                select(Pratica.cliente_id, func.count(Pratica.id)).where(
                    Pratica.cliente_id.in_([c.id for c in clienti] or [0]),
                    Pratica.stato.not_in(terminali),
                    Pratica.stato_verifica != StatoVerifica.RIFIUTATO.value,
                ),
                utente,
            ).group_by(Pratica.cliente_id)
        ).all()
    )
    return render(
        request,
        "clienti/elenco.html",
        utente,
        clienti=clienti,
        pagina=pagina,
        attive=attive,
        filtri={"q": q, "tutti": tutti},
        tipi_cliente=TIPI_CLIENTE,
    )


@router.get("/clienti/{cliente_id}")
def dettaglio(request: Request, cliente_id: int, session: Session = Depends(get_db, scope="function"), utente: User = Depends(utente_corrente)):
    cliente = _carica(session, cliente_id)
    pratiche = session.scalars(
        filtra_pratiche(select(Pratica), utente)
        .options(selectinload(Pratica.team), selectinload(Pratica.responsabile))
        .where(Pratica.cliente_id == cliente.id, Pratica.stato_verifica != StatoVerifica.RIFIUTATO.value)
        .order_by(Pratica.ultimo_evento_at.desc().nullslast(), Pratica.id.desc())
        .limit(200)
    ).all()
    return render(
        request,
        "clienti/dettaglio.html",
        utente,
        cliente=cliente,
        pratiche=pratiche,
        tipi_cliente=TIPI_CLIENTE,
        teams=session.scalars(select(Team).where(Team.attivo.is_(True)).order_by(Team.nome)).all(),
    )


@router.post("/clienti", dependencies=[Depends(verifica_csrf)])
def crea(
    request: Request,
    nome: str = Form(...),
    alias: str = Form(""),
    domini: str = Form(""),
    tipo: str = Form("altro"),
    session: Session = Depends(get_db, scope="function"),
    utente: User = Depends(gestione_clienti),
):
    nome = nome.strip()
    if session.scalar(select(Cliente.id).where(func.lower(Cliente.nome) == nome.lower())) is not None:
        flash(request, f"Esiste già un cliente chiamato '{nome}'.", "errore")
        return redirect("/clienti")
    cliente = Cliente(nome=nome, alias=_elenco(alias), domini=[d.lower() for d in _elenco(domini)], tipo=tipo)
    session.add(cliente)
    session.flush()
    flash(request, f"Cliente '{cliente.nome}' creato: completa l'anagrafica e i contatti.")
    return redirect(f"/clienti/{cliente.id}")


@router.post("/clienti/{cliente_id}", dependencies=[Depends(verifica_csrf)])
def modifica(
    request: Request,
    cliente_id: int,
    nome: str = Form(...),
    alias: str = Form(""),
    domini: str = Form(""),
    tipo: str = Form("altro"),
    ragione_sociale: str = Form(""),
    partita_iva: str = Form(""),
    codice_fiscale: str = Form(""),
    indirizzo: str = Form(""),
    citta: str = Form(""),
    telefono: str = Form(""),
    email: str = Form(""),
    pec: str = Form(""),
    codice_oracle: str = Form(""),
    codice_vte: str = Form(""),
    team_id: str = Form(""),
    note: str = Form(""),
    attivo: str = Form(""),
    session: Session = Depends(get_db, scope="function"),
    utente: User = Depends(gestione_clienti),
):
    cliente = _carica(session, cliente_id)
    nome = nome.strip()
    if not nome:
        raise HTTPException(status_code=400, detail="Il nome è obbligatorio")
    valori = {
        "ragione_sociale": ragione_sociale,
        "partita_iva": partita_iva,
        "codice_fiscale": codice_fiscale,
        "indirizzo": indirizzo,
        "citta": citta,
        "telefono": telefono,
        "email": email,
        "pec": pec,
        "codice_oracle": codice_oracle,
        "codice_vte": codice_vte,
    }
    cliente.nome = nome[:255]
    cliente.alias = _elenco(alias)
    cliente.domini = [d.lower() for d in _elenco(domini)]
    cliente.tipo = tipo if tipo in dict(TIPI_CLIENTE) else "altro"
    for campo in CAMPI_ANAGRAFICA:
        setattr(cliente, campo, valori[campo].strip())
    cliente.team_id = int(team_id) if team_id.strip().isdigit() and session.get(Team, int(team_id)) else None
    cliente.note = note
    cliente.attivo = bool(attivo)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        flash(request, f"Esiste già un cliente chiamato '{nome}'.", "errore")
        return redirect(f"/clienti/{cliente_id}")
    flash(request, f"Cliente '{cliente.nome}' salvato.")
    return redirect(f"/clienti/{cliente.id}")


@router.post("/clienti/{cliente_id}/contatti", dependencies=[Depends(verifica_csrf)])
def contatto_salva(
    request: Request,
    cliente_id: int,
    contatto_id: str = Form(""),
    nome: str = Form(...),
    ruolo: str = Form(""),
    email: str = Form(""),
    telefono: str = Form(""),
    note: str = Form(""),
    attivo: str = Form(""),
    nuovo: str = Form(""),
    session: Session = Depends(get_db, scope="function"),
    utente: User = Depends(gestione_clienti),
):
    cliente = _carica(session, cliente_id)
    contatto = session.get(ContattoCliente, int(contatto_id)) if contatto_id.strip().isdigit() else None
    if contatto is not None and contatto.cliente_id != cliente.id:
        raise HTTPException(status_code=404)
    if contatto is None:
        contatto = ContattoCliente(cliente=cliente)
        session.add(contatto)
    contatto.nome = nome.strip()[:255]
    contatto.ruolo = ruolo.strip()[:120]
    contatto.email = email.strip().lower()[:255]
    contatto.telefono = telefono.strip()[:50]
    contatto.note = note
    contatto.attivo = bool(attivo) or bool(nuovo)
    flash(request, f"Contatto '{contatto.nome}' salvato.")
    return redirect(f"/clienti/{cliente.id}#contatti")
