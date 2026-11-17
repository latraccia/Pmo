"""Giro delle estrazioni: per le letture in coda scarica i file (allegati Gmail, file Drive), li fa leggere a Claude e
mette le righe sull'ordine o sull'offerta (vedi pmo.services.estrazioni).

Lo scaricamento dipende dalla fonte: un dizionario fonte → funzione. Il programma di protocollo (via API) aggiungerà
la sua; l'estrazione resta la stessa.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from typing import Any

from googleapiclient.errors import HttpError
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from pmo.config import Settings, get_settings
from pmo.db.base import utcnow
from pmo.db.models import (
    Account,
    Articolo,
    Documento,
    Estrazione,
    FonteEstrazione,
    Messaggio,
    StatoEstrazione,
    SyncRun,
)
from pmo.ingest.classifier import ErroreClassificazione
from pmo.ingest.estrattore import Estrattore, EstrattoreNullo, FileDaLeggere, crea_estrattore
from pmo.ingest.serie_errori import MOTIVO_INTERRUZIONE, SERIE_ERRORI_MAX
from pmo.ingest.text_extract import estrai_testo
from pmo.services import estrazioni as svc
from pmo.services.verifica_ordini import revisione_di_riferimento

log = logging.getLogger(__name__)

MAX_TENTATIVI = 3
MAX_CARATTERI_WORD = 60_000
MAX_ARTICOLI_CONTESTO = 500


class Clienti:
    """I client Google dell'account, creati solo se servono (e una volta sola per giro)."""

    def __init__(self, account: Account, settings: Settings, *, gmail: Callable | None = None, drive: Callable | None = None):
        self.account, self.settings = account, settings
        self._fabbriche = {"gmail": gmail, "drive": drive}
        self._creati: dict[str, Any] = {}

    def _crea(self, tipo: str) -> Any:
        if tipo not in self._creati:
            fabbrica = self._fabbriche[tipo]
            if fabbrica is None:
                from pmo.connectors.google_oauth import credentials_per_account

                creds = credentials_per_account(self.account, self.settings)
                if tipo == "gmail":
                    from pmo.connectors.gmail import GmailClient

                    self._creati[tipo] = GmailClient(creds)
                else:
                    from pmo.connectors.drive import DriveClient

                    self._creati[tipo] = DriveClient(creds)
            else:
                self._creati[tipo] = fabbrica(self.account)
        return self._creati[tipo]

    @property
    def gmail(self) -> Any:
        return self._crea("gmail")

    @property
    def drive(self) -> Any:
        return self._crea("drive")


def _errore_google(exc: HttpError, cosa: str) -> ErroreClassificazione:
    stato = getattr(getattr(exc, "resp", None), "status", 0) or 0
    return ErroreClassificazione(f"{cosa}: errore Google {stato}", temporaneo=stato == 429 or stato >= 500)


def _da_byte(nome: str, mime: str, dati: bytes) -> FileDaLeggere:
    if mime == "application/pdf" or nome.lower().endswith(".pdf"):
        return FileDaLeggere(nome=nome, mime="application/pdf", dati=dati)
    return FileDaLeggere(nome=nome, mime=mime, testo=estrai_testo(dati, mime, nome, MAX_CARATTERI_WORD))


def _file_mail(estrazione: Estrazione, clienti: Clienti, settings: Settings) -> list[FileDaLeggere]:
    messaggio = estrazione.messaggio
    if messaggio is None:
        raise ErroreClassificazione("La mail d'origine non c'è più", temporaneo=False)
    file = []
    limite = settings.estrazione_max_mb * 1024 * 1024
    for allegato in svc.allegati_leggibili(messaggio)[: settings.estrazione_max_file]:
        if (allegato.get("size") or 0) > limite:
            continue
        try:
            dati = clienti.gmail.get_attachment(messaggio.gmail_message_id, allegato["attachment_id"])
        except HttpError as exc:
            raise _errore_google(exc, f"Allegato {allegato.get('filename')}") from exc
        file.append(_da_byte(allegato.get("filename") or "allegato", allegato.get("mime_type") or "", dati))
    return file


def _file_drive(estrazione: Estrazione, clienti: Clienti, settings: Settings) -> list[FileDaLeggere]:
    documento = estrazione.documento
    if documento is None:
        raise ErroreClassificazione("Il documento Drive d'origine non c'è più", temporaneo=False)
    if (documento.dimensione or 0) > settings.estrazione_max_mb * 1024 * 1024:
        raise ErroreClassificazione(f"{documento.nome}: file oltre {settings.estrazione_max_mb} MB", temporaneo=False)
    google = documento.mime_type.startswith("application/vnd.google-apps.")
    if not google and not svc.leggibile(documento.nome, documento.mime_type):
        return []
    try:
        dati = clienti.drive.scarica(documento.drive_file_id, documento.mime_type)
    except HttpError as exc:
        raise _errore_google(exc, documento.nome) from exc
    return [_da_byte(documento.nome, "application/pdf" if google else documento.mime_type, dati)]


SCARICA: dict[str, Callable[[Estrazione, Clienti, Settings], list[FileDaLeggere]]] = {
    FonteEstrazione.MAIL: _file_mail,
    FonteEstrazione.DRIVE: _file_drive,
}


def contesto(session: Session, estrazione: Estrazione) -> str:
    """Listino e, per un ordine, le righe dell'offerta di riferimento: aiutano Claude a riconoscere codici e voci."""
    parti = []
    articoli = session.scalars(select(Articolo).where(Articolo.attivo.is_(True)).order_by(Articolo.codice).limit(MAX_ARTICOLI_CONTESTO)).all()
    if articoli:
        righe = [f"{a.codice} | {a.descrizione} | {a.prezzo_listino if a.prezzo_listino is not None else ''}" for a in articoli]
        parti.append("Listino di La Traccia (codice | descrizione | prezzo):\n" + "\n".join(righe))
    revisione = revisione_di_riferimento(estrazione.ordine) if estrazione.ordine is not None else None
    if revisione is not None and revisione.righe:
        righe = [
            f"{r.modulo or '-'} | {r.descrizione} | q.tà {r.quantita if r.quantita is not None else ''} | prezzo {r.prezzo_unitario if r.prezzo_unitario is not None else ''}"
            for r in revisione.righe
        ]
        parti.append(f"Offerta di riferimento {revisione.nome}:\n" + "\n".join(righe))
    return "\n\n".join(parti)


def elabora_estrazione(session: Session, estrazione: Estrazione, estrattore: Estrattore, clienti: Clienti, settings: Settings) -> None:
    """Una lettura: scarica, controlla che il file non sia già stato letto, chiede a Claude, applica."""
    scarica = SCARICA.get(estrazione.fonte)
    if scarica is None:
        raise ErroreClassificazione(f"Fonte {estrazione.fonte}: connettore non ancora disponibile", temporaneo=False)
    file = scarica(estrazione, clienti, settings)
    if not file:
        svc._saltata(estrazione, "Nessun file leggibile (PDF o Word) entro i limiti di dimensione.")
        return
    estrazione.file = [
        {"nome": f.nome, "mime": f.mime, "sha256": hashlib.sha256(f.dati or f.testo.encode()).hexdigest(), "byte": len(f.dati or f.testo)}
        for f in file
    ]
    altra = svc.gia_letto(session, estrazione, {f["sha256"] for f in estrazione.file})
    if altra is not None:
        svc._saltata(estrazione, f"Stesso file già letto per {altra.destinazione.nome if altra.destinazione else 'un altro documento'}: non riletto.")
        return
    destinazione = "ordine" if estrazione.ordine_id else "offerta"
    prima = estrattore.uso.to_dict()
    letto = estrattore.estrai(destinazione, file, contesto(session, estrazione))
    dopo = estrattore.uso.to_dict()
    estrazione.modello = estrattore.modello
    estrazione.token = {k: dopo[k] - prima.get(k, 0) for k in dopo}
    usato = (letto.documento_usato or "").strip().lower()
    if usato:
        estrazione.file = [{**f, "usato": f["nome"].lower() == usato or usato in f["nome"].lower()} for f in estrazione.file]
        if not any(f["usato"] for f in estrazione.file):
            estrazione.file = [{**f, "usato": True} for f in estrazione.file]
    svc.applica_risultato(session, estrazione, letto.model_dump())


def elabora_estrazioni(
    session: Session,
    account: Account,
    settings: Settings | None = None,
    *,
    estrattore: Estrattore | None = None,
    clienti: Clienti | None = None,
    limite: int | None = None,
) -> SyncRun | None:
    """Le letture in coda che vengono da mail o documenti di questa casella. Senza IA (o con la lettura delle righe
    spenta) non fa niente: le letture restano in coda per quando si riaccende."""
    settings = settings or get_settings()
    estrattore = estrattore or crea_estrattore(settings)
    if isinstance(estrattore, EstrattoreNullo):
        return None
    in_attesa = session.scalars(
        select(Estrazione)
        .outerjoin(Messaggio, Estrazione.messaggio_id == Messaggio.id)
        .outerjoin(Documento, Estrazione.documento_id == Documento.id)
        .where(Estrazione.stato == StatoEstrazione.IN_ATTESA, or_(Messaggio.account_id == account.id, Documento.account_id == account.id))
        .order_by(Estrazione.id)
        .limit(limite or settings.estrazione_per_giro)
    ).all()
    if not in_attesa:
        return None
    clienti = clienti or Clienti(account, settings)
    run = SyncRun(account_id=account.id, tipo="estrazione")
    session.add(run)
    session.commit()  # un errore su un documento annulla solo il suo lavoro, non quello fatto prima del giro
    contatori = {"estrazioni": 0, "fatte": 0, "saltate": 0, "errori": 0}
    serie: list[Estrazione] = []  # errori temporanei consecutivi: rete o servizio non raggiungibili
    for estrazione in in_attesa:
        contatori["estrazioni"] += 1
        try:
            elabora_estrazione(session, estrazione, estrattore, clienti, settings)
        except Exception as exc:  # noqa: BLE001 - un documento che non si legge non ferma gli altri
            if not isinstance(exc, ErroreClassificazione):
                log.exception("Estrazione %s non riuscita", estrazione.id)
                exc = ErroreClassificazione(f"Errore inatteso: {exc}", temporaneo=False)
            session.rollback()
            estrazione = session.get(Estrazione, estrazione.id)
            contatori["errori"] += 1
            run = session.get(SyncRun, run.id)
            run.errori = [*run.errori, str(exc)]
            estrazione.tentativi += 1
            estrazione.errore = str(exc)[:1000]
            if not exc.temporaneo or estrazione.tentativi >= MAX_TENTATIVI:
                estrazione.stato = StatoEstrazione.ERRORE
                estrazione.elaborata_at = utcnow()
            serie = [*serie, estrazione] if exc.temporaneo else []
            session.commit()
            if len(serie) >= SERIE_ERRORI_MAX:
                for e in serie:  # non era colpa dei documenti: il tentativo non conta
                    e.tentativi = max(0, e.tentativi - 1)
                    e.stato = StatoEstrazione.IN_ATTESA
                run.errori = [*run.errori, MOTIVO_INTERRUZIONE]
                session.commit()
                break
            continue
        serie = []
        contatori["fatte" if estrazione.stato == StatoEstrazione.FATTA else "saltate"] += 1
        session.commit()
    contatori["token"] = estrattore.uso.to_dict()
    run.contatori = contatori
    run.stato = "ok" if contatori["errori"] == 0 else "errore"
    run.finished_at = utcnow()
    session.commit()
    return run
