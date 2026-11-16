"""Impostazioni: regole, filtri anti-rumore, prodotti, utenti e ruoli, team. I clienti hanno una pagina loro."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from pmo.db.models import (
    STATI_TERMINALI,
    Account,
    Cliente,
    Pratica,
    Prodotto,
    Regola,
    StatoVerifica,
    Team,
    User,
)
from pmo.permessi import Azione, Ruolo, filtra_pratiche, puo, ruolo_valido
from pmo.rules.definitions import assicura_regole
from pmo.services.impostazioni import get_impostazione, set_impostazione
from pmo.services.mcp_guida import contesto_guida
from pmo.web.deps import flash, get_db, redirect, render, richiede, utente_corrente, verifica_csrf

router = APIRouter()

# Scheda -> azione richiesta per vederla e modificarla.
SCHEDE: dict[str, Azione] = {
    "regole": Azione.CONFIGURARE,
    "filtri": Azione.CONFIGURARE,
    "prodotti": Azione.CONFIGURARE,
    "utenti": Azione.GESTIRE_UTENTI,
    "team": Azione.GESTIRE_UTENTI,
    "claude": Azione.CONFIGURARE,
}
configurazione = richiede(Azione.CONFIGURARE)
gestione_utenti = richiede(Azione.GESTIRE_UTENTI)


def _righe(testo: str) -> list[str]:
    return [r.strip() for r in (testo or "").splitlines() if r.strip()]


@router.get("/impostazioni")
def pagina(request: Request, scheda: str = "", session: Session = Depends(get_db, scope="function"), utente: User = Depends(utente_corrente)):
    consentite = [k for k, azione in SCHEDE.items() if puo(utente, azione)]
    if not consentite:
        raise HTTPException(status_code=403, detail="Il tuo ruolo non consente di modificare le impostazioni")
    if scheda == "clienti":
        return redirect("/clienti")  # i clienti hanno una pagina loro: i vecchi collegamenti portano lì
    if scheda not in consentite:
        if scheda in SCHEDE:
            raise HTTPException(status_code=403, detail="Il tuo ruolo non consente di aprire questa sezione")
        scheda = consentite[0]
    regole = sorted(assicura_regole(session).values(), key=lambda r: r.nome) if scheda == "regole" else []
    return render(
        request,
        "impostazioni.html",
        utente,
        scheda=scheda,
        schede_consentite=consentite,
        mcp=contesto_guida() if scheda == "claude" else None,
        regole=regole,
        filtri=get_impostazione(session, "filtri"),
        prodotti=session.scalars(select(Prodotto).order_by(Prodotto.nome)).all(),
        utenti=session.scalars(select(User).options(selectinload(User.teams)).order_by(User.email)).all(),
        teams=session.scalars(select(Team).options(selectinload(Team.membri)).order_by(Team.nome)).all(),
        conteggi_team=_conteggi_team(session, utente) if scheda == "team" else {},
        ruoli=[r.value for r in Ruolo],
    )


@router.post("/impostazioni/regole/{regola_id}", dependencies=[Depends(verifica_csrf)])
def regola(request: Request, regola_id: int, soglia_giorni: str = Form(""), gravita: str = Form("avviso"), attiva: str = Form(""), session: Session = Depends(get_db, scope="function"), utente: User = Depends(configurazione)):
    r = session.get(Regola, regola_id)
    if r is None:
        raise HTTPException(status_code=404)
    r.soglia_giorni = int(soglia_giorni) if soglia_giorni.strip().isdigit() else r.soglia_giorni
    r.gravita = gravita
    r.attiva = bool(attiva)
    flash(request, f"Regola '{r.nome}' aggiornata.")
    return redirect("/impostazioni?scheda=regole")


@router.post("/impostazioni/filtri", dependencies=[Depends(verifica_csrf)])
def filtri(
    request: Request,
    ignore_senders: str = Form(""),
    ignore_domains: str = Form(""),
    keep_senders: str = Form(""),
    ignore_sender_patterns: str = Form(""),
    ignore_subject_patterns: str = Form(""),
    session: Session = Depends(get_db, scope="function"),
    utente: User = Depends(configurazione),
):
    set_impostazione(
        session,
        "filtri",
        {
            "ignore_senders": [s.lower() for s in _righe(ignore_senders)],
            "ignore_domains": [s.lower() for s in _righe(ignore_domains)],
            "keep_senders": [s.lower() for s in _righe(keep_senders)],
            "ignore_sender_patterns": _righe(ignore_sender_patterns),
            "ignore_subject_patterns": _righe(ignore_subject_patterns),
        },
    )
    flash(request, "Filtri aggiornati: valgono per i messaggi importati da ora in poi.")
    return redirect("/impostazioni?scheda=filtri")


@router.post("/impostazioni/prodotti", dependencies=[Depends(verifica_csrf)])
def prodotto(request: Request, prodotto_id: str = Form(""), nome: str = Form(...), alias: str = Form(""), descrizione: str = Form(""), attivo: str = Form("1"), session: Session = Depends(get_db, scope="function"), utente: User = Depends(configurazione)):
    p = session.get(Prodotto, int(prodotto_id)) if prodotto_id else None
    if p is None:
        p = Prodotto(nome=nome.strip())
        session.add(p)
    p.nome = nome.strip()
    p.alias = [a.strip() for a in alias.split(",") if a.strip()]
    p.descrizione = descrizione.strip()
    p.attivo = bool(attivo)
    flash(request, f"Prodotto '{p.nome}' salvato.")
    return redirect("/impostazioni?scheda=prodotti")


@router.post("/impostazioni/utenti", dependencies=[Depends(verifica_csrf)])
def utente_salva(
    request: Request,
    user_id: str = Form(""),
    email: str = Form(...),
    nome: str = Form(""),
    ruolo: str = Form(Ruolo.OPERATIVO.value),
    attivo: str = Form(""),
    riceve_digest: str = Form(""),
    team_ids: list[int] = Form([]),
    session: Session = Depends(get_db, scope="function"),
    utente: User = Depends(gestione_utenti),
):
    u = session.get(User, int(user_id)) if user_id else None
    if u is None:
        u = session.scalar(select(User).where(User.email == email.lower().strip()))
    if u is None:
        u = User(email=email.lower().strip())
        session.add(u)
    u.nome = nome.strip() or u.nome or u.email
    u.ruolo = (ruolo_valido(ruolo) or Ruolo.OPERATIVO).value
    u.attivo = bool(attivo) or u.id is None
    u.riceve_digest = bool(riceve_digest)
    u.teams = list(session.scalars(select(Team).where(Team.id.in_(team_ids or [0]))).all())
    if u.id == utente.id:
        # Nessuno si toglie da solo la possibilità di gestire gli utenti, né si disattiva.
        if not puo(u, Azione.GESTIRE_UTENTI):
            u.ruolo = utente.ruolo
        u.attivo = True
    flash(request, f"Utente {u.email} salvato.")
    return redirect("/impostazioni?scheda=utenti")


# --- team -----------------------------------------------------------------------------------------


def _pratiche_attive(utente: User):
    """Le pratiche ancora in corso (non chiuse, annullate o rifiutate) visibili all'utente."""
    return lambda query: filtra_pratiche(
        query.where(Pratica.stato.not_in([s.value for s in STATI_TERMINALI]), Pratica.stato_verifica != StatoVerifica.RIFIUTATO.value),
        utente,
    )


def _conteggi_team(session: Session, utente: User) -> dict[int, dict[str, int]]:
    """Per ogni team: pratiche attive e caselle, per l'elenco."""
    attive = _pratiche_attive(utente)
    pratiche = dict(session.execute(attive(select(Pratica.team_id, func.count(Pratica.id)).where(Pratica.team_id.is_not(None))).group_by(Pratica.team_id)).all())
    caselle = dict(session.execute(select(Account.team_id, func.count(Account.id)).where(Account.team_id.is_not(None)).group_by(Account.team_id)).all())
    return {tid: {"pratiche": pratiche.get(tid, 0), "caselle": caselle.get(tid, 0)} for tid in set(pratiche) | set(caselle)}


def _vai_al_team(team_id: int):
    return redirect(f"/impostazioni/team/{team_id}")


@router.get("/impostazioni/team/{team_id}")
def team_dettaglio(request: Request, team_id: int, session: Session = Depends(get_db, scope="function"), utente: User = Depends(gestione_utenti)):
    team = session.scalar(select(Team).options(selectinload(Team.membri).selectinload(User.teams)).where(Team.id == team_id))
    if team is None:
        raise HTTPException(status_code=404, detail="Team non trovato")
    attive = _pratiche_attive(utente)
    pratiche = session.scalars(
        attive(select(Pratica).where(Pratica.team_id == team.id)).options(selectinload(Pratica.cliente), selectinload(Pratica.responsabile)).order_by(Pratica.ultimo_evento_at.desc()).limit(10)
    ).all()
    n_pratiche = session.scalar(attive(select(func.count(Pratica.id)).where(Pratica.team_id == team.id))) or 0
    return render(
        request,
        "team/dettaglio.html",
        utente,
        team=team,
        membri=sorted(team.membri, key=lambda m: (m.nome or m.email).lower()),
        candidati=session.scalars(select(User).where(User.attivo.is_(True), User.id.not_in([m.id for m in team.membri] or [0])).order_by(User.nome, User.email)).all(),
        caselle=session.scalars(select(Account).options(selectinload(Account.owner)).where(Account.team_id == team.id).order_by(Account.email)).all(),
        clienti=session.scalars(select(Cliente).where(Cliente.team_id == team.id).order_by(Cliente.nome)).all(),
        pratiche=pratiche,
        n_pratiche=n_pratiche,
    )


@router.post("/impostazioni/team", dependencies=[Depends(verifica_csrf)])
def team_salva(
    request: Request,
    team_id: str = Form(""),
    nome: str = Form(...),
    descrizione: str = Form(""),
    attivo: str = Form(""),
    nuovo: str = Form(""),
    session: Session = Depends(get_db, scope="function"),
    utente: User = Depends(gestione_utenti),
):
    team = session.get(Team, int(team_id)) if team_id.strip().isdigit() else None
    if team is None:
        team = Team()
        session.add(team)
    team.nome = nome.strip()[:120]
    team.descrizione = descrizione.strip()
    team.attivo = bool(attivo) or bool(nuovo)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        flash(request, f"Esiste già un team chiamato '{nome.strip()}'.", "errore")
        return _vai_al_team(int(team_id)) if team_id.strip().isdigit() else redirect("/impostazioni?scheda=team")
    flash(request, f"Team '{team.nome}' {'creato: ora aggiungi i membri' if nuovo else 'salvato'}.")
    return _vai_al_team(team.id)


@router.post("/impostazioni/team/{team_id}/membri", dependencies=[Depends(verifica_csrf)])
def team_aggiungi_membro(request: Request, team_id: int, user_id: int = Form(...), session: Session = Depends(get_db, scope="function"), utente: User = Depends(gestione_utenti)):
    team = session.get(Team, team_id)
    persona = session.get(User, user_id)
    if team is None or persona is None:
        raise HTTPException(status_code=404)
    if persona not in team.membri:
        team.membri.append(persona)
    flash(request, f"{persona.nome or persona.email} aggiunto al team '{team.nome}'.")
    return _vai_al_team(team.id)


@router.post("/impostazioni/team/{team_id}/membri/{user_id}/rimuovi", dependencies=[Depends(verifica_csrf)])
def team_rimuovi_membro(request: Request, team_id: int, user_id: int, session: Session = Depends(get_db, scope="function"), utente: User = Depends(gestione_utenti)):
    team = session.get(Team, team_id)
    persona = session.get(User, user_id)
    if team is None or persona is None:
        raise HTTPException(status_code=404)
    if persona in team.membri:
        team.membri.remove(persona)
    flash(request, f"{persona.nome or persona.email} tolto dal team '{team.nome}'.")
    return _vai_al_team(team.id)
