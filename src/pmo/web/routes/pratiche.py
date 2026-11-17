"""Pratiche: elenco, dettaglio con cronologia, modifica, eventi, conferme e collegamenti manuali."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from pydantic import BeforeValidator
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from pmo.config import get_settings
from pmo.db.models import (
    ETICHETTE_EVENTO,
    ETICHETTE_STATO,
    ETICHETTE_TIPO_PRATICA,
    Cliente,
    Documento,
    Evento,
    Messaggio,
    Pratica,
    Prodotto,
    Sollecito,
    StatoMessaggio,
    StatoPratica,
    StatoSollecito,
    StatoVerifica,
    Team,
    TipoEvento,
    TipoFonte,
    User,
)
from pmo.permessi import (
    Azione,
    carica_pratica_visibile,
    elemento_visibile,
    filtra_pratiche,
    ids_pratiche_visibili,
    pratica_visibile,
    puo,
    puo_passare_a,
    team_ids,
    team_predefinito,
    vede_tutto,
)
from pmo.rules.engine import ordina_gravita
from pmo.services import ordini as ordini_svc
from pmo.services import pratiche as svc
from pmo.services.richieste import (
    AZIONI_DA_FARE,
    attese_cliente,
    pratiche_con_azioni,
    richieste_in_attesa,
    scadenze_vicine,
)
from pmo.web.deps import (
    flash,
    get_db,
    pagina_per,
    redirect,
    render,
    richiede,
    ritorno_sicuro,
    utente_corrente,
    verifica_csrf,
)

router = APIRouter()

PER_PAGINA_PRATICHE = 24

TIPI_EVENTO_MANUALI = [t.value for t in TipoEvento if t not in (TipoEvento.CAMBIO_STATO,)]


def _decimal(valore: str | None) -> Decimal | None:
    if valore is None or not valore.strip():
        return None
    pulito = valore.strip().replace("€", "").replace(" ", "")
    if "," in pulito and "." in pulito:
        pulito = pulito.replace(".", "").replace(",", ".")
    elif "," in pulito:
        pulito = pulito.replace(",", ".")
    try:
        return Decimal(pulito)
    except InvalidOperation as exc:
        raise HTTPException(status_code=400, detail=f"Importo non valido: {valore}") from exc


def _date(valore: str | None) -> date | None:
    if not valore:
        return None
    try:
        return date.fromisoformat(valore)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Data non valida: {valore}") from exc


def _riferimenti(testo: str | None) -> dict:
    out: dict[str, str] = {}
    for riga in (testo or "").splitlines():
        if "=" in riga:
            k, v = riga.split("=", 1)
            if k.strip() and v.strip():
                out[k.strip().lower().replace(" ", "_")] = v.strip()
    return out


def _carica(session: Session, pratica_id: int, utente: User) -> Pratica:
    return carica_pratica_visibile(session, pratica_id, utente)


def _team_assegnabili(session: Session, utente: User) -> list[Team]:
    """I team che l'utente può dare a una pratica: tutti per chi vede tutto, altrimenti i propri."""
    query = select(Team).where(Team.attivo.is_(True)).order_by(Team.nome)
    if not vede_tutto(utente):
        query = query.where(Team.id.in_(team_ids(utente) or [0]))
    return list(session.scalars(query).all())


def _team_scelto(session: Session, utente: User, valore: str) -> int | None:
    """Il team indicato nel modulo, solo se l'utente può assegnarlo (un id inventato non passa)."""
    team_id = _intero_o(valore, None)
    if team_id is None:
        return None
    if team_id not in {t.id for t in _team_assegnabili(session, utente)}:
        raise HTTPException(status_code=400, detail="Team non valido")
    return team_id


def _id_utente(valore: str) -> int | None:
    return _intero_o(valore, None)


def _intero_o(valore: object, predefinito: int | None) -> int | None:
    """Il modulo dei filtri manda i menu lasciati su "Tutti" come stringa vuota: non è un errore, è "nessun filtro"."""
    try:
        return int(str(valore).strip())
    except (TypeError, ValueError):
        return predefinito


IdFiltro = Annotated[int | None, BeforeValidator(lambda v: _intero_o(v, None))]
InteroFiltro = Annotated[int, BeforeValidator(lambda v: _intero_o(v, 0))]


def _filtra_per_testo(query, testo: str):  # noqa: ANN001, ANN202
    """Ogni parola scritta deve comparire in almeno uno tra codice, titolo, descrizione, cliente e prodotto."""
    for parola in testo.split():
        pulita = parola.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{pulita}%"
        query = query.where(
            or_(
                Pratica.codice.ilike(like, escape="\\"),
                Pratica.titolo.ilike(like, escape="\\"),
                Pratica.descrizione.ilike(like, escape="\\"),
                Pratica.cliente_id.in_(select(Cliente.id).where(Cliente.nome.ilike(like, escape="\\"))),
                Pratica.prodotto_id.in_(select(Prodotto.id).where(Prodotto.nome.ilike(like, escape="\\"))),
            )
        )
    return query


@router.get("/pratiche")
def elenco(
    request: Request,
    q: str = "",
    stato: str = "",
    cliente_id: IdFiltro = None,
    prodotto_id: IdFiltro = None,
    tipo: str = "",
    responsabile_id: str = "",  # un identificativo, oppure "nessuno" per le pratiche senza responsabile
    team_id: str = "",  # un identificativo, oppure "nessuno" per le pratiche senza team
    verifica: str = "",
    tutte: InteroFiltro = 0,
    azione: str = "",
    richieste: InteroFiltro = 0,  # vecchio nome del filtro "in attesa di una nostra risposta", tenuto per i link già in uso
    session: Session = Depends(get_db, scope="function"),
    utente: User = Depends(utente_corrente),
):
    query = filtra_pratiche(select(Pratica), utente)
    senza_responsabile = responsabile_id == "nessuno"
    responsabile = "nessuno" if senza_responsabile else _intero_o(responsabile_id, None)
    senza_team = team_id == "nessuno"
    team = "nessuno" if senza_team else _intero_o(team_id, None)
    visibili = ids_pratiche_visibili(session, utente)
    richieste_aperte = richieste_in_attesa(session, visibili)
    attese_aperte = attese_cliente(session, visibili)
    azioni = pratiche_con_azioni(session, richieste_aperte, attese_aperte)
    if visibili is not None:
        azioni = {chiave: ids & visibili for chiave, ids in azioni.items()}
    azione = azione or ("risposta" if richieste else "")
    if azione in azioni:
        query = query.where(Pratica.id.in_(list(azioni[azione]) or [0]))
    else:
        azione = ""
    query = _filtra_per_testo(query, q)
    if stato:
        query = query.where(Pratica.stato == stato)
    elif not tutte:
        query = query.where(Pratica.stato.notin_([StatoPratica.CHIUSA.value, StatoPratica.ANNULLATA.value]))
    if cliente_id:
        query = query.where(Pratica.cliente_id == cliente_id)
    if prodotto_id:
        query = query.where(Pratica.prodotto_id == prodotto_id)
    if tipo in ETICHETTE_TIPO_PRATICA:
        query = query.where(Pratica.tipo == tipo)
    else:
        tipo = ""
    if senza_responsabile:
        query = query.where(Pratica.responsabile_user_id.is_(None))
    elif responsabile:
        query = query.where(Pratica.responsabile_user_id == responsabile)
    if senza_team:
        query = query.where(Pratica.team_id.is_(None))
    elif team:
        query = query.where(Pratica.team_id == team)
    if verifica == "proposte":
        query = query.where(Pratica.stato_verifica == StatoVerifica.PROPOSTO.value)
    elif verifica == "confermate":
        query = query.where(Pratica.stato_verifica == StatoVerifica.CONFERMATO.value)
    else:
        query = query.where(Pratica.stato_verifica != StatoVerifica.RIFIUTATO.value)
    # Il riepilogo vale per tutto l'elenco filtrato, non per la sola pagina mostrata.
    filtrate = query.subquery()
    totale = session.scalar(select(func.count()).select_from(filtrate)) or 0
    riepilogo = {
        "totale": totale,
        "proposte": session.scalar(select(func.count()).select_from(filtrate).where(filtrate.c.stato_verifica == StatoVerifica.PROPOSTO.value)) or 0,
        "ordinato": session.scalar(select(func.sum(filtrate.c.importo_ordine))) or 0,
        "con_solleciti": session.scalar(
            select(func.count(func.distinct(Sollecito.pratica_id))).where(
                Sollecito.pratica_id.in_(select(filtrate.c.id)),
                Sollecito.stato.in_([StatoSollecito.APERTO.value, StatoSollecito.IN_CARICO.value]),
            )
        )
        or 0,
        "con_richieste": len(set(session.scalars(select(filtrate.c.id))) & set(richieste_aperte)),
        "con_attese": len(set(session.scalars(select(filtrate.c.id))) & set(attese_aperte)),
        "con_azioni": len(set(session.scalars(select(filtrate.c.id))) & azioni["qualsiasi"]),
    }
    pagina = pagina_per(request, totale, PER_PAGINA_PRATICHE)
    pratiche = session.scalars(
        query.options(selectinload(Pratica.cliente), selectinload(Pratica.prodotto), selectinload(Pratica.responsabile), selectinload(Pratica.team), selectinload(Pratica.solleciti))
        .order_by(Pratica.ultimo_evento_at.desc().nullslast(), Pratica.id.desc())
        .offset(pagina.offset)
        .limit(pagina.per_pagina)
    ).all()
    clienti = session.scalars(select(Cliente).where(Cliente.attivo.is_(True)).order_by(Cliente.nome)).all()
    prodotti = session.scalars(select(Prodotto).where(Prodotto.attivo.is_(True)).order_by(Prodotto.nome)).all()
    utenti = session.scalars(select(User).where(User.attivo.is_(True)).order_by(User.nome)).all()
    return render(
        request,
        "pratiche/elenco.html",
        utente,
        pratiche=pratiche,
        pagina=pagina,
        riepilogo=riepilogo,
        clienti=clienti,
        prodotti=prodotti,
        utenti=utenti,
        teams=_team_assegnabili(session, utente),
        stati_consentiti={k: v for k, v in ETICHETTE_STATO.items() if puo_passare_a(utente, k)},
        richieste_aperte=richieste_aperte,
        attese_aperte=attese_aperte,
        # quante pratiche attive hanno un'attesa, indipendentemente dagli altri filtri: servono ai comandi rapidi
        totali_attese={chiave: len(azioni[chiave]) for chiave in ("risposta", "attesa_cliente", "attese")},
        azioni_da_fare=AZIONI_DA_FARE,
        scadenze=scadenze_vicine(session),
        filtri={"q": q, "stato": stato, "cliente_id": cliente_id, "prodotto_id": prodotto_id, "tipo": tipo, "responsabile_id": responsabile, "team_id": team, "verifica": verifica, "tutte": tutte, "azione": azione},
        stati=ETICHETTE_STATO,
        tipi=ETICHETTE_TIPO_PRATICA,
    )


@router.get("/pratiche/nuova")
def nuova(request: Request, session: Session = Depends(get_db, scope="function"), utente: User = Depends(richiede(Azione.CREARE_PRATICA))):
    clienti = session.scalars(select(Cliente).where(Cliente.attivo.is_(True)).order_by(Cliente.nome)).all()
    prodotti = session.scalars(select(Prodotto).where(Prodotto.attivo.is_(True)).order_by(Prodotto.nome)).all()
    utenti = session.scalars(select(User).where(User.attivo.is_(True)).order_by(User.nome)).all()
    return render(
        request,
        "pratiche/nuova.html",
        utente,
        clienti=clienti,
        prodotti=prodotti,
        utenti=utenti,
        teams=_team_assegnabili(session, utente),
        team_proposto=team_predefinito(utente),
        stati=ETICHETTE_STATO,
        tipi=ETICHETTE_TIPO_PRATICA,
    )


@router.post("/pratiche", dependencies=[Depends(verifica_csrf)])
def crea(
    request: Request,
    titolo: str = Form(...),
    descrizione: str = Form(""),
    cliente_id: str = Form(""),
    prodotto_id: str = Form(""),
    tipo: str = Form("fornitura"),
    stato: str = Form("bozza"),
    responsabile_id: str = Form(""),
    sostituto_id: str = Form(""),
    team_id: str = Form(""),
    data_scadenza: str = Form(""),
    importo_offerta: str = Form(""),
    importo_ordine: str = Form(""),
    priorita: str = Form("media"),
    session: Session = Depends(get_db, scope="function"),
    utente: User = Depends(richiede(Azione.CREARE_PRATICA)),
):
    if stato in ("chiusa", "annullata") and not puo(utente, Azione.CHIUDERE):
        raise HTTPException(status_code=403, detail="Il tuo ruolo non consente di creare una commessa chiusa")
    pratica = svc.crea_pratica(
        session,
        titolo,
        descrizione=descrizione,
        cliente=session.get(Cliente, int(cliente_id)) if cliente_id else None,
        prodotto=session.get(Prodotto, int(prodotto_id)) if prodotto_id else None,
        tipo=tipo,
        stato=stato,
        responsabile_user_id=_id_utente(responsabile_id) or utente.id,
        sostituto_user_id=_id_utente(sostituto_id),
        team_id=_team_scelto(session, utente, team_id),
        created_by_user_id=utente.id,
        data_scadenza=_date(data_scadenza),
        importo_offerta=_decimal(importo_offerta),
        importo_ordine=_decimal(importo_ordine),
        priorita=priorita,
    )
    flash(request, f"Commessa {pratica.codice} creata. Usa il tag {pratica.tag} nell'oggetto delle mail.")
    return redirect(f"/pratiche/{pratica.id}")


@router.get("/pratiche/{pratica_id}")
def dettaglio(request: Request, pratica_id: int, session: Session = Depends(get_db, scope="function"), utente: User = Depends(utente_corrente)):
    pratica = _carica(session, pratica_id, utente)
    eventi = sorted(pratica.eventi, key=lambda e: (e.data_evento, e.id), reverse=True)
    messaggi = session.scalars(
        select(Messaggio).where(Messaggio.pratica_id == pratica.id).order_by(Messaggio.data.desc()).limit(200)
    ).all()
    documenti = session.scalars(
        select(Documento).where(Documento.pratica_id == pratica.id).order_by(Documento.modified_time.desc()).limit(200)
    ).all()
    solleciti = sorted(pratica.solleciti, key=lambda s: (s.stato != "aperto" and s.stato != "in_carico", ordina_gravita(s.gravita)))
    clienti = session.scalars(select(Cliente).where(Cliente.attivo.is_(True)).order_by(Cliente.nome)).all()
    prodotti = session.scalars(select(Prodotto).where(Prodotto.attivo.is_(True)).order_by(Prodotto.nome)).all()
    utenti = session.scalars(select(User).where(User.attivo.is_(True)).order_by(User.nome)).all()
    proposti = [e for e in eventi if e.stato_verifica == StatoVerifica.PROPOSTO]
    return render(
        request,
        "pratiche/dettaglio.html",
        utente,
        pratica=pratica,
        richieste_cliente=richieste_in_attesa(session, [pratica.id]).get(pratica.id, []),
        attese_del_cliente=attese_cliente(session, [pratica.id]).get(pratica.id, []),
        eventi=eventi,
        proposti=proposti,
        messaggi=messaggi,
        documenti=documenti,
        solleciti=solleciti,
        clienti=clienti,
        prodotti=prodotti,
        utenti=utenti,
        teams=_team_assegnabili(session, utente),
        stati=ETICHETTE_STATO,
        tipi=ETICHETTE_TIPO_PRATICA,
        stati_consentiti={k: v for k, v in ETICHETTE_STATO.items() if puo_passare_a(utente, k)},
        tipi_evento=[(t, ETICHETTE_EVENTO[t]) for t in TIPI_EVENTO_MANUALI],
        riferimenti_testo="\n".join(f"{k}={v}" for k, v in (pratica.riferimenti or {}).items()),
        residui_offerte={r.id: ordini_svc.residuo_revisione(r) for r in ordini_svc.revisioni_accettate(pratica)},
        importi_calcolati=ordini_svc.importi_calcolati(pratica),
    )


@router.post("/pratiche/{pratica_id}/modifica", dependencies=[Depends(verifica_csrf)])
def modifica(
    request: Request,
    pratica_id: int,
    titolo: str = Form(...),
    descrizione: str = Form(""),
    cliente_id: str = Form(""),
    prodotto_id: str = Form(""),
    tipo: str = Form(""),
    responsabile_id: str = Form(""),
    sostituto_id: str = Form(""),
    team_id: str = Form(""),
    priorita: str = Form("media"),
    data_scadenza: str = Form(""),
    importo_stima: str = Form(""),
    importo_offerta: str = Form(""),
    importo_ordine: str = Form(""),
    importo_fatturato: str = Form(""),
    riferimenti: str = Form(""),
    drive_folder_url: str = Form(""),
    note: str = Form(""),
    session: Session = Depends(get_db, scope="function"),
    utente: User = Depends(richiede(Azione.MODIFICARE_PRATICA, Azione.MODIFICARE_IMPORTI)),
):
    pratica = _carica(session, pratica_id, utente)
    # Ognuno salva solo la parte che il suo ruolo gli consente: gli altri campi del modulo vengono ignorati.
    if puo(utente, Azione.MODIFICARE_PRATICA):
        pratica.titolo = titolo.strip()[:300]
        pratica.descrizione = descrizione
        pratica.cliente_id = int(cliente_id) if cliente_id else None
        pratica.prodotto_id = int(prodotto_id) if prodotto_id else None
        if tipo in ETICHETTE_TIPO_PRATICA:
            pratica.tipo = tipo
        pratica.priorita = priorita
        pratica.data_scadenza = _date(data_scadenza)
        pratica.riferimenti = _riferimenti(riferimenti)
        pratica.drive_folder_url = drive_folder_url.strip() or None
        pratica.note = note
    if puo(utente, Azione.ASSEGNARE):
        pratica.responsabile_user_id = _id_utente(responsabile_id)
        pratica.sostituto_user_id = _id_utente(sostituto_id)
        pratica.team_id = _team_scelto(session, utente, team_id)
    if puo(utente, Azione.MODIFICARE_IMPORTI):
        # Con offerte accettate e ordini a righe, offerto e ordinato si calcolano dalle righe: non si scrivono.
        calcolati = ordini_svc.importi_calcolati(pratica)
        pratica.importo_stima = _decimal(importo_stima)
        if not calcolati["importo_offerta"]:
            pratica.importo_offerta = _decimal(importo_offerta)
        if not calcolati["importo_ordine"]:
            pratica.importo_ordine = _decimal(importo_ordine)
        pratica.importo_fatturato = _decimal(importo_fatturato)
    flash(request, "Commessa aggiornata.")
    if not pratica_visibile(session, pratica, utente):
        flash(request, "La commessa ora appartiene a un team di cui non fai parte: non la vedi più.", "avviso")
        return redirect("/pratiche")
    return redirect(f"/pratiche/{pratica.id}")


@router.post("/pratiche/{pratica_id}/stato", dependencies=[Depends(verifica_csrf)])
def cambia_stato(request: Request, pratica_id: int, stato: str = Form(...), motivo: str = Form(""), ritorno: str = Form(""), session: Session = Depends(get_db, scope="function"), utente: User = Depends(utente_corrente)):
    pratica = _carica(session, pratica_id, utente)
    if not puo_passare_a(utente, stato):
        raise HTTPException(status_code=403, detail="Il tuo ruolo non consente questo cambio di fase")
    try:
        svc.cambia_stato(session, pratica, stato, motivo=motivo, user_id=utente.id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Stato non valido") from None
    flash(request, f"{pratica.codice}: fase aggiornata a '{pratica.etichetta_stato}'.")
    # Dall'elenco si torna all'elenco, con gli stessi filtri; solo dentro le pratiche, mai verso un altro sito.
    return redirect(ritorno_sicuro(ritorno, f"/pratiche/{pratica.id}", prefisso="/pratiche"))


@router.post("/pratiche/{pratica_id}/eventi", dependencies=[Depends(verifica_csrf)])
def aggiungi_evento(
    request: Request,
    pratica_id: int,
    tipo: str = Form(...),
    titolo: str = Form(...),
    descrizione: str = Form(""),
    data_evento: str = Form(""),
    importo: str = Form(""),
    scadenza: str = Form(""),
    session: Session = Depends(get_db, scope="function"),
    utente: User = Depends(richiede(Azione.ANNOTARE)),
):
    pratica = _carica(session, pratica_id, utente)
    giorno = _date(data_evento)
    quando = datetime.combine(giorno, time(12, 0), tzinfo=UTC) if giorno else None
    svc.aggiungi_evento(
        session,
        pratica,
        tipo=tipo,
        titolo=titolo,
        descrizione=descrizione,
        data_evento=quando,
        fonte_tipo=TipoFonte.MANUALE,
        importo=_decimal(importo),
        scadenza=_date(scadenza),
        created_by_user_id=utente.id,
    )
    flash(request, "Evento registrato.")
    return redirect(f"/pratiche/{pratica.id}")


@router.post("/pratiche/{pratica_id}/eventi/{evento_id}/{azione}", dependencies=[Depends(verifica_csrf)])
def evento_azione(request: Request, pratica_id: int, evento_id: int, azione: str, session: Session = Depends(get_db, scope="function"), utente: User = Depends(richiede(Azione.ANNOTARE))):
    _carica(session, pratica_id, utente)
    evento = session.get(Evento, evento_id)
    if evento is None or evento.pratica_id != pratica_id:
        raise HTTPException(status_code=404)
    if azione == "conferma":
        svc.conferma_evento(session, evento, utente.id)
        flash(request, "Evento confermato.")
    elif azione == "rifiuta":
        nota = svc.rifiuta_evento(session, evento, utente.id)
        flash(request, "Evento scartato." + (f" {nota}" if nota else ""))
    elif azione == "elimina":
        nota = ordini_svc.evento_rimosso(session, evento)
        session.delete(evento)
        flash(request, "Evento eliminato." + (f" {nota}" if nota else ""))
    else:
        raise HTTPException(status_code=404)
    return redirect(f"/pratiche/{pratica_id}")


@router.post("/pratiche/{pratica_id}/conferma", dependencies=[Depends(verifica_csrf)])
def conferma(request: Request, pratica_id: int, session: Session = Depends(get_db, scope="function"), utente: User = Depends(richiede(Azione.CONFERMARE))):
    pratica = _carica(session, pratica_id, utente)
    svc.conferma_pratica(session, pratica, utente.id, soglia=get_settings().event_confidence_threshold)
    flash(request, f"Commessa {pratica.codice} confermata.")
    return redirect(f"/pratiche/{pratica.id}")


@router.post("/pratiche/{pratica_id}/scarta", dependencies=[Depends(verifica_csrf)])
def scarta(request: Request, pratica_id: int, session: Session = Depends(get_db, scope="function"), utente: User = Depends(richiede(Azione.CONFERMARE))):
    pratica = _carica(session, pratica_id, utente)
    svc.rifiuta_pratica(session, pratica, utente.id)
    flash(request, f"Commessa proposta {pratica.codice} scartata: i messaggi tornano in revisione.")
    return redirect("/revisione")


@router.post("/pratiche/{pratica_id}/unisci", dependencies=[Depends(verifica_csrf)])
def unisci(request: Request, pratica_id: int, codice: str = Form(...), session: Session = Depends(get_db, scope="function"), utente: User = Depends(richiede(Azione.UNIRE))):
    pratica = _carica(session, pratica_id, utente)
    destinazione = svc.trova_per_codice(session, codice.strip())
    if destinazione is None or destinazione.id == pratica.id or not pratica_visibile(session, destinazione, utente):
        flash(request, "Codice commessa di destinazione non valido.", "errore")
        return redirect(f"/pratiche/{pratica.id}")
    svc.unisci_pratiche(session, pratica, destinazione, utente.id)
    flash(request, f"Commessa {pratica.codice} unita a {destinazione.codice}.")
    return redirect(f"/pratiche/{destinazione.id}")


# --- collegamenti manuali di messaggi e documenti -----------------------------------------------


def _collega_elemento(session: Session, elemento, codice: str, utente: User) -> Pratica | None:  # noqa: ANN001
    pratica = svc.trova_per_codice(session, codice.strip()) if codice.strip() else None
    if pratica is None or not pratica_visibile(session, pratica, utente):
        return None
    elemento.pratica_id = pratica.id
    elemento.link_origine = "manuale"
    elemento.stato = StatoMessaggio.CLASSIFICATO
    elemento.motivo = f"Collegato manualmente da {utente.email}"
    elemento.processed_at = datetime.now(UTC)
    for evento in session.scalars(select(Evento).where(Evento.messaggio_id == elemento.id if isinstance(elemento, Messaggio) else Evento.documento_id == elemento.id)).all():
        if evento.pratica_id != pratica.id:
            evento.pratica_id = pratica.id
        if evento.stato_verifica == StatoVerifica.PROPOSTO:
            svc.conferma_evento(session, evento, utente.id)
    return pratica


@router.post("/messaggi/{messaggio_id}/collega", dependencies=[Depends(verifica_csrf)])
def collega_messaggio(request: Request, messaggio_id: int, codice: str = Form(""), ritorno: str = Form("/revisione"), session: Session = Depends(get_db, scope="function"), utente: User = Depends(richiede(Azione.ANNOTARE))):
    messaggio = session.get(Messaggio, messaggio_id)
    if not elemento_visibile(session, messaggio, utente):
        raise HTTPException(status_code=404)
    pratica = _collega_elemento(session, messaggio, codice, utente)
    if pratica is None:
        flash(request, "Codice commessa non trovato.", "errore")
    else:
        flash(request, f"Messaggio collegato a {pratica.codice}.")
    return redirect(ritorno_sicuro(ritorno, "/revisione"))


@router.post("/messaggi/{messaggio_id}/{azione}", dependencies=[Depends(verifica_csrf)])
def messaggio_azione(request: Request, messaggio_id: int, azione: str, ritorno: str = Form("/revisione"), session: Session = Depends(get_db, scope="function"), utente: User = Depends(richiede(Azione.ANNOTARE))):
    messaggio = session.get(Messaggio, messaggio_id)
    if not elemento_visibile(session, messaggio, utente):
        raise HTTPException(status_code=404)
    if azione == "conferma" and messaggio.pratica_id:
        _collega_elemento(session, messaggio, messaggio.pratica.codice, utente)
        flash(request, "Collegamento confermato.")
    elif azione == "ignora":
        messaggio.stato = StatoMessaggio.IGNORATO
        messaggio.motivo = f"Ignorato da {utente.email}"
        for evento in session.scalars(select(Evento).where(Evento.messaggio_id == messaggio.id, Evento.stato_verifica == StatoVerifica.PROPOSTO.value)).all():
            svc.rifiuta_evento(session, evento, utente.id)
        flash(request, "Messaggio ignorato.")
    elif azione == "scollega":
        messaggio.pratica_id = None
        messaggio.link_origine = None
        messaggio.stato = StatoMessaggio.DA_RIVEDERE
        flash(request, "Messaggio scollegato.")
    elif azione == "riclassifica":
        messaggio.stato = StatoMessaggio.NUOVO
        messaggio.classificazione = {}
        flash(request, "Il messaggio verrà riclassificato alla prossima sincronizzazione.")
    else:
        raise HTTPException(status_code=404)
    return redirect(ritorno_sicuro(ritorno, "/revisione"))


@router.post("/documenti/{documento_id}/collega", dependencies=[Depends(verifica_csrf)])
def collega_documento(request: Request, documento_id: int, codice: str = Form(""), ritorno: str = Form("/revisione"), session: Session = Depends(get_db, scope="function"), utente: User = Depends(richiede(Azione.ANNOTARE))):
    documento = session.get(Documento, documento_id)
    if not elemento_visibile(session, documento, utente):
        raise HTTPException(status_code=404)
    pratica = _collega_elemento(session, documento, codice, utente)
    if pratica is None:
        flash(request, "Codice commessa non trovato.", "errore")
    else:
        flash(request, f"Documento collegato a {pratica.codice}.")
    return redirect(ritorno_sicuro(ritorno, "/revisione"))


@router.post("/documenti/{documento_id}/{azione}", dependencies=[Depends(verifica_csrf)])
def documento_azione(request: Request, documento_id: int, azione: str, ritorno: str = Form("/revisione"), session: Session = Depends(get_db, scope="function"), utente: User = Depends(richiede(Azione.ANNOTARE))):
    documento = session.get(Documento, documento_id)
    if not elemento_visibile(session, documento, utente):
        raise HTTPException(status_code=404)
    if azione == "conferma" and documento.pratica_id:
        _collega_elemento(session, documento, documento.pratica.codice, utente)
        flash(request, "Collegamento confermato.")
    elif azione == "ignora":
        documento.stato = StatoMessaggio.IGNORATO
        documento.motivo = f"Ignorato da {utente.email}"
        flash(request, "Documento ignorato.")
    elif azione == "riclassifica":
        documento.stato = StatoMessaggio.NUOVO
        documento.classificazione = {}
        flash(request, "Il documento verrà riclassificato alla prossima sincronizzazione.")
    else:
        raise HTTPException(status_code=404)
    return redirect(ritorno_sicuro(ritorno, "/revisione"))
