"""Cruscotto: situazione complessiva delle pratiche, solleciti, revisione e caselle."""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from pmo.db.models import (
    FASI_ORDINATE,
    Account,
    Documento,
    Evento,
    Messaggio,
    Pratica,
    Sollecito,
    StatoMessaggio,
    StatoPratica,
    StatoSollecito,
    StatoVerifica,
    SyncRun,
    TipoFonte,
    User,
)
from pmo.permessi import (
    filtra_caselle,
    filtra_elementi,
    filtra_pratiche,
    ids_pratiche_visibili,
    team_ids,
    vede_tutto,
)
from pmo.rules.engine import ordina_gravita
from pmo.services.richieste import attese_cliente, richieste_in_attesa
from pmo.web.deps import get_db, render, utente_corrente

router = APIRouter()


@router.get("/")
def dashboard(request: Request, session: Session = Depends(get_db, scope="function"), utente: User = Depends(utente_corrente)):
    visibili = filtra_pratiche(select(Pratica.id), utente)
    per_stato_raw = dict(
        session.execute(
            filtra_pratiche(select(Pratica.stato, func.count()).where(Pratica.stato_verifica == StatoVerifica.CONFERMATO.value), utente).group_by(Pratica.stato)
        ).all()
    )
    per_stato = [(s.value, per_stato_raw.get(s.value, 0)) for s in FASI_ORDINATE if s != StatoPratica.CHIUSA]
    totale_attive = sum(n for _, n in per_stato)

    solleciti = session.scalars(
        select(Sollecito)
        .options(selectinload(Sollecito.pratica).selectinload(Pratica.cliente))
        .where(Sollecito.stato.in_([StatoSollecito.APERTO.value, StatoSollecito.IN_CARICO.value]), Sollecito.pratica_id.in_(visibili))
    ).all()
    oggi = date.today()
    solleciti = [s for s in solleciti if not (s.snooze_fino and s.snooze_fino > oggi)]
    solleciti.sort(key=lambda s: (ordina_gravita(s.gravita), s.aperto_il), reverse=False)
    per_gravita = {"critico": 0, "avviso": 0, "info": 0}
    for s in solleciti:
        per_gravita[s.gravita] = per_gravita.get(s.gravita, 0) + 1

    def conta_messaggi(stato: StatoMessaggio) -> int:
        return session.scalar(filtra_elementi(select(func.count(Messaggio.id)).where(Messaggio.stato == stato.value), utente, Messaggio)) or 0

    da_rivedere = {
        "messaggi": conta_messaggi(StatoMessaggio.DA_RIVEDERE),
        "documenti": session.scalar(filtra_elementi(select(func.count(Documento.id)).where(Documento.stato == StatoMessaggio.DA_RIVEDERE.value), utente, Documento)) or 0,
        "pratiche": session.scalar(filtra_pratiche(select(func.count(Pratica.id)).where(Pratica.stato_verifica == StatoVerifica.PROPOSTO.value), utente)) or 0,
        "errori": conta_messaggi(StatoMessaggio.ERRORE),
    }
    in_attesa_ia = conta_messaggi(StatoMessaggio.NUOVO)

    scadenze = session.scalars(
        filtra_pratiche(select(Pratica), utente)
        .options(selectinload(Pratica.cliente))
        .where(
            Pratica.data_scadenza.isnot(None),
            Pratica.data_scadenza <= oggi + timedelta(days=14),
            Pratica.stato.notin_([StatoPratica.CHIUSA.value, StatoPratica.ANNULLATA.value]),
        )
        .order_by(Pratica.data_scadenza)
        .limit(10)
    ).all()

    eventi = session.scalars(
        select(Evento)
        .options(selectinload(Evento.pratica))
        .where(Evento.fonte_tipo != TipoFonte.SISTEMA.value, Evento.pratica_id.in_(visibili))
        .order_by(Evento.created_at.desc())
        .limit(15)
    ).all()

    caselle = session.scalars(filtra_caselle(select(Account), utente).options(selectinload(Account.watched_folders)).order_by(Account.email)).all()
    run = select(SyncRun).order_by(SyncRun.started_at.desc()).limit(5)
    if not vede_tutto(utente):
        run = run.where(SyncRun.account_id.in_([a.id for a in caselle] or [0]))
    ultime_run = session.scalars(run).all()

    # Richieste dei clienti verso di noi ancora senza risposta: le più vecchie per prime, perché sono le più in ritardo.
    ids_visibili = ids_pratiche_visibili(session, utente)
    aperte = richieste_in_attesa(session, ids_visibili)
    pratiche_con_richieste = {
        p.id: p
        for p in session.scalars(select(Pratica).options(selectinload(Pratica.cliente)).where(Pratica.id.in_(list(aperte) or [0])))
    }
    richieste_cliente = sorted(
        ((r[0], pratiche_con_richieste[pid]) for pid, r in aperte.items() if pid in pratiche_con_richieste),
        key=lambda coppia: coppia[0].data,
    )
    # ... e quelle che aspettiamo noi dal cliente, sempre dalla più vecchia.
    attese = attese_cliente(session, ids_visibili)
    pratiche_in_attesa = {
        p.id: p
        for p in session.scalars(select(Pratica).options(selectinload(Pratica.cliente)).where(Pratica.id.in_(list(attese) or [0])))
    }
    attese_del_cliente = sorted(
        ((a[0], pratiche_in_attesa[pid]) for pid, a in attese.items() if pid in pratiche_in_attesa),
        key=lambda coppia: coppia[0].data,
    )

    return render(
        request,
        "dashboard.html",
        utente,
        per_stato=per_stato,
        totale_attive=totale_attive,
        solleciti=solleciti[:12],
        totale_solleciti=len(solleciti),
        per_gravita=per_gravita,
        da_rivedere=da_rivedere,
        in_attesa_ia=in_attesa_ia,
        scadenze=scadenze,
        eventi=eventi,
        caselle=caselle,
        ultime_run=ultime_run,
        richieste_cliente=richieste_cliente[:10],
        totale_richieste_cliente=len(richieste_cliente),
        attese_del_cliente=attese_del_cliente[:10],
        totale_attese_del_cliente=len(attese_del_cliente),
        senza_team=not vede_tutto(utente) and not team_ids(utente),
    )
