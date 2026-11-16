"""Ruoli, permessi e visibilità: chi può fare cosa e chi vede quali pratiche, caselle, mail e documenti.

Ogni utente ha un solo ruolo. I ruoli "globali" (amministratore, direzione, amministrazione) vedono tutto; gli altri
vedono le pratiche dei team a cui appartengono, quelle di cui sono responsabili o sostituti e quelle che hanno creato.
Una pratica senza team si vede anche attraverso la casella da cui è nata. Le regole stanno tutte qui, così
interfaccia web, server MCP e riga di comando applicano gli stessi controlli.
"""

from __future__ import annotations

import enum

from fastapi import HTTPException
from sqlalchemy import ColumnElement, and_, false, or_, select
from sqlalchemy.orm import Session

from pmo.db.models import Account, Documento, Messaggio, Pratica, User


class Ruolo(enum.StrEnum):
    AMMINISTRATORE = "amministratore"
    DIREZIONE = "direzione"
    AMMINISTRAZIONE = "amministrazione"
    RESPONSABILE = "responsabile"
    COMMERCIALE = "commerciale"
    OPERATIVO = "operativo"


ETICHETTE_RUOLO: dict[str, str] = {
    Ruolo.AMMINISTRATORE: "Amministratore",
    Ruolo.DIREZIONE: "Direzione",
    Ruolo.AMMINISTRAZIONE: "Amministrazione",
    Ruolo.RESPONSABILE: "Responsabile di commessa",
    Ruolo.COMMERCIALE: "Commerciale",
    Ruolo.OPERATIVO: "Operativo",
}

# Valori in uso prima della migrazione 0002 (e ancora accettati, ad esempio in un seed.yaml non aggiornato).
RUOLI_PRECEDENTI: dict[str, Ruolo] = {"admin": Ruolo.AMMINISTRATORE, "membro": Ruolo.OPERATIVO}


class Azione(enum.StrEnum):
    VEDERE_TUTTO = "vedere_tutto"  # tutte le pratiche, caselle, mail e documenti, senza filtro per team
    CONFIGURARE = "configurare"  # regole, filtri, digest, prodotti, collegamento a Claude
    GESTIRE_UTENTI = "gestire_utenti"  # utenti, ruoli e team
    GESTIRE_CASELLE = "gestire_caselle"  # caselle di altri utenti, assegnazione ai team, eliminazione
    GESTIRE_CLIENTI = "gestire_clienti"  # anagrafica clienti e contatti
    CREARE_PRATICA = "creare_pratica"
    MODIFICARE_PRATICA = "modificare_pratica"  # titolo, descrizione, cliente, scadenze, riferimenti, note
    MODIFICARE_IMPORTI = "modificare_importi"
    ASSEGNARE = "assegnare"  # team, responsabile e sostituto di una pratica
    CONFERMARE = "confermare"  # conferma o scarto delle pratiche proposte dall'IA
    UNIRE = "unire"
    CAMBIARE_STATO = "cambiare_stato"
    CHIUDERE = "chiudere"  # passaggio a "chiusa" o "annullata"
    ANNOTARE = "annotare"  # note ed eventi, collegamento di mail e documenti, revisione degli eventi proposti
    GESTIRE_SOLLECITI = "gestire_solleciti"
    GESTIRE_ORDINI = "gestire_ordini"  # offerte, ordini e righe; verifica dell'ordine contro l'offerta
    APPROVARE_DEROGHE = "approvare_deroghe"  # accettare un ordine con scostamenti, con motivo


_LAVORO_PRATICHE = frozenset(
    {
        Azione.GESTIRE_CLIENTI,
        Azione.CREARE_PRATICA,
        Azione.MODIFICARE_PRATICA,
        Azione.MODIFICARE_IMPORTI,
        Azione.CONFERMARE,
        Azione.CAMBIARE_STATO,
        Azione.ANNOTARE,
        Azione.GESTIRE_SOLLECITI,
        Azione.GESTIRE_ORDINI,
        Azione.APPROVARE_DEROGHE,
    }
)

PERMESSI: dict[Ruolo, frozenset[Azione]] = {
    Ruolo.AMMINISTRATORE: frozenset(Azione),
    Ruolo.DIREZIONE: frozenset(Azione) - {Azione.CONFIGURARE, Azione.GESTIRE_UTENTI, Azione.GESTIRE_CASELLE},
    Ruolo.AMMINISTRAZIONE: frozenset(
        {Azione.VEDERE_TUTTO, Azione.GESTIRE_CLIENTI, Azione.MODIFICARE_IMPORTI, Azione.ANNOTARE, Azione.GESTIRE_SOLLECITI}
    ),
    Ruolo.RESPONSABILE: _LAVORO_PRATICHE | {Azione.ASSEGNARE, Azione.UNIRE, Azione.CHIUDERE},
    Ruolo.COMMERCIALE: _LAVORO_PRATICHE,
    Ruolo.OPERATIVO: frozenset({Azione.ANNOTARE, Azione.GESTIRE_SOLLECITI}),
}

STATI_DI_CHIUSURA = {"chiusa", "annullata"}


def ruolo_valido(valore: str | None) -> Ruolo | None:
    """Il ruolo scritto in un modulo, in un file o sulla riga di comando; None se non è riconosciuto."""
    testo = (valore or "").strip().lower()
    if testo in RUOLI_PRECEDENTI:
        return RUOLI_PRECEDENTI[testo]
    try:
        return Ruolo(testo)
    except ValueError:
        return None


def ruolo_di(utente: User | None) -> Ruolo | None:
    if utente is None or not utente.attivo:
        return None
    return ruolo_valido(utente.ruolo) or Ruolo.OPERATIVO


def puo(utente: User | None, azione: Azione | str) -> bool:
    ruolo = ruolo_di(utente)
    return ruolo is not None and Azione(azione) in PERMESSI[ruolo]


def puo_passare_a(utente: User | None, stato: str) -> bool:
    """Cambiare fase richiede CAMBIARE_STATO; chiudere o annullare una pratica richiede anche CHIUDERE."""
    if not puo(utente, Azione.CAMBIARE_STATO):
        return False
    return stato not in STATI_DI_CHIUSURA or puo(utente, Azione.CHIUDERE)


def vede_tutto(utente: User | None) -> bool:
    return puo(utente, Azione.VEDERE_TUTTO)


def team_ids(utente: User | None) -> list[int]:
    """I team attivi dell'utente."""
    if utente is None:
        return []
    return [t.id for t in utente.teams if t.attivo]


# --- visibilità ----------------------------------------------------------------------------------


def condizione_caselle(utente: User | None) -> ColumnElement[bool] | None:
    """Caselle visibili: le proprie e quelle dei propri team. None = nessun filtro."""
    if utente is None:
        return false()
    if vede_tutto(utente):
        return None
    ids = team_ids(utente)
    condizioni = [Account.owner_user_id == utente.id]
    if ids:
        condizioni.append(Account.team_id.in_(ids))
    return or_(*condizioni)


def _caselle_select(utente: User):  # noqa: ANN202
    condizione = condizione_caselle(utente)
    query = select(Account.id)
    return query if condizione is None else query.where(condizione)


def condizione_pratiche(utente: User | None) -> ColumnElement[bool] | None:
    """Pratiche visibili all'utente. None = nessun filtro (ruoli che vedono tutto)."""
    if utente is None:
        return false()
    if vede_tutto(utente):
        return None
    ids = team_ids(utente)
    condizioni = [
        Pratica.responsabile_user_id == utente.id,
        Pratica.sostituto_user_id == utente.id,
        Pratica.created_by_user_id == utente.id,
        and_(Pratica.team_id.is_(None), Pratica.account_id.in_(_caselle_select(utente))),
    ]
    if ids:
        condizioni.insert(0, Pratica.team_id.in_(ids))
    return or_(*condizioni)


def filtra_pratiche(query, utente: User | None):  # noqa: ANN001, ANN201
    """Aggiunge a una select su Pratica il filtro di visibilità dell'utente."""
    condizione = condizione_pratiche(utente)
    return query if condizione is None else query.where(condizione)


def filtra_caselle(query, utente: User | None):  # noqa: ANN001, ANN201
    condizione = condizione_caselle(utente)
    return query if condizione is None else query.where(condizione)


def condizione_elementi(utente: User | None, modello: type[Messaggio] | type[Documento]) -> ColumnElement[bool] | None:
    """Mail e documenti visibili: arrivati da una casella visibile oppure collegati a una pratica visibile."""
    if utente is None:
        return false()
    if vede_tutto(utente):
        return None
    pratiche = filtra_pratiche(select(Pratica.id), utente)
    return or_(modello.account_id.in_(_caselle_select(utente)), modello.pratica_id.in_(pratiche))


def filtra_elementi(query, utente: User | None, modello: type[Messaggio] | type[Documento]):  # noqa: ANN001, ANN201
    condizione = condizione_elementi(utente, modello)
    return query if condizione is None else query.where(condizione)


def ids_pratiche_visibili(session: Session, utente: User | None) -> set[int] | None:
    """Gli identificativi delle pratiche visibili; None se l'utente le vede tutte."""
    if vede_tutto(utente):
        return None
    return set(session.scalars(filtra_pratiche(select(Pratica.id), utente)))


def pratica_visibile(session: Session, pratica: Pratica | None, utente: User | None) -> bool:
    if pratica is None or utente is None:
        return False
    if vede_tutto(utente):
        return True
    return session.scalar(filtra_pratiche(select(Pratica.id).where(Pratica.id == pratica.id), utente)) is not None


def carica_pratica_visibile(session: Session, pratica_id: int, utente: User) -> Pratica:
    """La pratica, se l'utente può vederla; altrimenti 404 (non si rivela che esiste)."""
    pratica = session.get(Pratica, pratica_id)
    if not pratica_visibile(session, pratica, utente):
        raise HTTPException(status_code=404, detail="Commessa non trovata")
    return pratica


def casella_visibile(account: Account | None, utente: User | None) -> bool:
    if account is None or utente is None:
        return False
    if vede_tutto(utente):
        return True
    return account.owner_user_id == utente.id or (account.team_id is not None and account.team_id in team_ids(utente))


def elemento_visibile(session: Session, elemento: Messaggio | Documento | None, utente: User | None) -> bool:
    if elemento is None or utente is None:
        return False
    if vede_tutto(utente) or casella_visibile(elemento.account, utente):
        return True
    return elemento.pratica is not None and pratica_visibile(session, elemento.pratica, utente)


def team_predefinito(utente: User | None) -> int | None:
    """Il team da proporre per una pratica nuova: quello dell'utente, se ne ha uno solo."""
    ids = team_ids(utente)
    return ids[0] if len(ids) == 1 else None
