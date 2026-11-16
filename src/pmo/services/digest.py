"""Notifica email: riepilogo giornaliero dei solleciti e degli elementi da verificare, via Gmail API o SMTP.

Nel codice si chiama ancora "digest". È personale: va all'indirizzo email di ogni utente attivo che ha la notifica email
attiva (User.riceve_digest), ed è l'unico modo di sceglierne i destinatari. Ogni utente riceve una mail con i soli solleciti ed elementi da verificare delle pratiche,
mail e documenti che può vedere nell'applicazione (ruolo e team, vedi pmo.permessi). Un indirizzo che non corrisponde a
un utente attivo del PMO non riceve nulla: non si saprebbe cosa può vedere.
"""

from __future__ import annotations

import html
import logging
import smtplib
from dataclasses import dataclass, field
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from pmo.config import GMAIL_SEND, Settings, get_settings
from pmo.connectors.gmail import GmailClient
from pmo.connectors.google_oauth import credentials_per_account, scope_presente
from pmo.db.base import utcnow
from pmo.db.models import (
    Account,
    Documento,
    Messaggio,
    Pratica,
    Sollecito,
    StatoMessaggio,
    StatoSollecito,
    StatoVerifica,
    SyncRun,
    User,
)
from pmo.permessi import filtra_elementi, filtra_pratiche
from pmo.rules.engine import ordina_gravita

log = logging.getLogger(__name__)

ETICHETTE_GRAVITA = {"critico": "Critici", "avviso": "Avvisi", "info": "Informativi"}


@dataclass
class ContenutoDigest:
    oggetto: str
    testo: str
    html: str
    solleciti: list[Sollecito] = field(default_factory=list)
    contatori: dict[str, int] = field(default_factory=dict)


def _solleciti_aperti(session: Session, oggi: date, utente: User | None) -> list[Sollecito]:
    query = (
        select(Sollecito)
        .options(selectinload(Sollecito.pratica).selectinload(Pratica.cliente))
        .where(Sollecito.stato.in_([StatoSollecito.APERTO.value, StatoSollecito.IN_CARICO.value]))
    )
    if utente is not None:
        query = query.where(Sollecito.pratica_id.in_(filtra_pratiche(select(Pratica.id), utente)))
    righe = session.scalars(query).all()
    righe = [s for s in righe if not (s.snooze_fino and s.snooze_fino > oggi)]
    righe.sort(key=lambda s: (ordina_gravita(s.gravita), s.notificato_il is not None, s.pratica.codice))
    return righe


def componi_digest(
    session: Session, settings: Settings | None = None, oggi: date | None = None, utente: User | None = None
) -> ContenutoDigest:
    """Il digest visto da `utente` (solo ciò che può vedere). Senza utente, il riepilogo completo (uso interno e test)."""
    settings = settings or get_settings()
    oggi = oggi or date.today()
    solleciti = _solleciti_aperti(session, oggi, utente)

    def conta(query, modello=None) -> int:  # noqa: ANN001
        if utente is not None:
            query = filtra_pratiche(query, utente) if modello is None else filtra_elementi(query, utente, modello)
        return int(session.scalar(query) or 0)

    da_rivedere_msg = conta(select(func.count(Messaggio.id)).where(Messaggio.stato == StatoMessaggio.DA_RIVEDERE.value), Messaggio)
    da_rivedere_doc = conta(select(func.count(Documento.id)).where(Documento.stato == StatoMessaggio.DA_RIVEDERE.value), Documento)
    proposte = conta(select(func.count(Pratica.id)).where(Pratica.stato_verifica == StatoVerifica.PROPOSTO.value))
    nuovi = [s for s in solleciti if s.notificato_il is None]
    contatori = {
        "solleciti": len(solleciti),
        "nuovi": len(nuovi),
        "critici": sum(1 for s in solleciti if s.gravita == "critico"),
        "da_rivedere": int(da_rivedere_msg) + int(da_rivedere_doc),
        "pratiche_proposte": int(proposte),
    }
    base = settings.base_url.rstrip("/")
    oggetto = f"[{settings.app_name}] {oggi.strftime('%d/%m/%Y')}: {contatori['solleciti']} solleciti aperti ({contatori['nuovi']} nuovi), {contatori['da_rivedere']} elementi da verificare"

    righe_txt: list[str] = [f"Notifiche {settings.app_name} del {oggi.strftime('%d/%m/%Y')}", ""]
    righe_html: list[str] = [f"<h2>Notifiche {html.escape(settings.app_name)} del {oggi.strftime('%d/%m/%Y')}</h2>"]
    righe_txt.append(
        f"Solleciti aperti: {contatori['solleciti']} (nuovi: {contatori['nuovi']}, critici: {contatori['critici']}). "
        f"Da verificare: {contatori['da_rivedere']} messaggi/documenti, {contatori['pratiche_proposte']} commesse proposte."
    )
    righe_html.append(
        f"<p>Solleciti aperti: <b>{contatori['solleciti']}</b> (nuovi: {contatori['nuovi']}, critici: {contatori['critici']}). "
        f"Da verificare: <b>{contatori['da_rivedere']}</b> messaggi/documenti, <b>{contatori['pratiche_proposte']}</b> commesse proposte "
        f"&rarr; <a href='{base}/revisione'>coda di revisione</a>.</p>"
    )
    gravita_corrente = None
    for s in solleciti:
        if s.gravita != gravita_corrente:
            gravita_corrente = s.gravita
            etichetta = ETICHETTE_GRAVITA.get(s.gravita, s.gravita)
            righe_txt += ["", f"== {etichetta} =="]
            righe_html.append(f"<h3>{html.escape(etichetta)}</h3><ul>")
        pr = s.pratica
        cliente = f" · {pr.cliente.nome}" if pr.cliente else ""
        nuovo = " [NUOVO]" if s.notificato_il is None else ""
        righe_txt.append(f"- {pr.codice} {pr.titolo}{cliente}: {s.titolo}{nuovo}\n  {s.messaggio}\n  {base}/pratiche/{pr.id}")
        righe_html.append(
            f"<li><a href='{base}/pratiche/{pr.id}'>{html.escape(pr.codice)} {html.escape(pr.titolo)}</a>{html.escape(cliente)}: "
            f"<b>{html.escape(s.titolo)}</b>{nuovo}<br><small>{html.escape(s.messaggio)}</small></li>"
        )
        if s is solleciti[-1] or solleciti[solleciti.index(s) + 1].gravita != s.gravita:
            righe_html.append("</ul>")
    if not solleciti:
        righe_txt.append("Nessun sollecito aperto.")
        righe_html.append("<p>Nessun sollecito aperto.</p>")
    righe_txt += ["", f"Cruscotto: {base}/"]
    righe_html.append(f"<p><a href='{base}/'>Apri il cruscotto di {html.escape(settings.app_name)}</a></p>")
    return ContenutoDigest(oggetto=oggetto, testo="\n".join(righe_txt), html="\n".join(righe_html), solleciti=solleciti, contatori=contatori)


def destinatari_digest(session: Session, settings: Settings | None = None) -> list[str]:
    """Gli indirizzi email degli utenti attivi con la notifica email attiva."""
    utenti = session.scalars(select(User).where(User.attivo.is_(True), User.riceve_digest.is_(True))).all()
    return [u.email for u in utenti]


def _invia_smtp(settings: Settings, destinatari: list[str], contenuto: ContenutoDigest) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = contenuto.oggetto
    msg["From"] = settings.smtp_from or settings.smtp_user or "pmo@localhost"
    msg["To"] = ", ".join(destinatari)
    msg.attach(MIMEText(contenuto.testo, "plain", "utf-8"))
    msg.attach(MIMEText(contenuto.html, "html", "utf-8"))
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
        if settings.smtp_starttls:
            smtp.starttls()
        if settings.smtp_user and settings.smtp_password:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.sendmail(msg["From"], destinatari, msg.as_string())


def _account_mittente(session: Session) -> Account | None:
    for account in session.scalars(select(Account).where(Account.attivo.is_(True))).all():
        if account.collegato and scope_presente(account, GMAIL_SEND):
            return account
    return None


def _personali(session: Session, indirizzi: list[str]) -> tuple[list[User], list[str]]:
    """Gli utenti attivi corrispondenti agli indirizzi, e gli indirizzi che non sono utenti del PMO (esclusi)."""
    puliti = list(dict.fromkeys(i.strip().lower() for i in indirizzi if i and i.strip()))
    utenti = {u.email: u for u in session.scalars(select(User).where(User.email.in_(puliti or [""]), User.attivo.is_(True))).all()}
    return [utenti[i] for i in puliti if i in utenti], [i for i in puliti if i not in utenti]


def invia_digest(
    session: Session,
    settings: Settings | None = None,
    *,
    destinatari: list[str] | None = None,
    solo_anteprima: bool = False,
) -> dict:
    """Compone e invia un digest per destinatario, ciascuno con ciò che quel destinatario può vedere."""
    settings = settings or get_settings()
    run = SyncRun(tipo="digest")
    session.add(run)
    session.flush()
    utenti, esclusi = _personali(session, destinatari or destinatari_digest(session, settings))
    contenuti = {u.email: componi_digest(session, settings, utente=u) for u in utenti}
    esito: dict = {
        "destinatari": list(contenuti),
        "esclusi": esclusi,
        "contatori": {email: c.contatori for email, c in contenuti.items()},
        "inviato": False,
    }
    if esclusi:
        esito["nota"] = "Gli indirizzi esclusi non sono utenti attivi dell'applicazione: la notifica email va solo agli utenti."
    if solo_anteprima:
        esito["anteprime"] = {email: c.testo for email, c in contenuti.items()}
        run.stato = "ok"
        run.contatori = {"destinatari": len(contenuti), "esclusi": len(esclusi), "anteprima": True}
        run.finished_at = utcnow()
        session.commit()
        return esito
    errori: list[str] = []
    inviati: list[str] = []
    try:
        if not contenuti:
            raise RuntimeError("Nessun destinatario: nessun utente attivo ha la notifica email attiva")
        account = _account_mittente(session)
        if account is None and not settings.smtp_host:
            raise RuntimeError(
                "Nessun canale di invio: collegare una casella con scope gmail.send (PMO_GMAIL_SEND_SCOPE=true) oppure configurare SMTP"
            )
        client = GmailClient(credentials_per_account(account, settings)) if account is not None else None
        esito["canale"] = f"gmail:{account.email}" if account is not None else "smtp"
        for email, contenuto in contenuti.items():
            try:
                if client is not None:
                    client.send_email([email], contenuto.oggetto, contenuto.testo, contenuto.html)
                else:
                    _invia_smtp(settings, [email], contenuto)
                inviati.append(email)
            except Exception as exc:  # noqa: BLE001
                log.exception("Invio del digest a %s fallito", email)
                errori.append(f"{email}: {exc}")
    except Exception as exc:  # noqa: BLE001
        log.exception("Invio digest fallito")
        errori.append(str(exc))
    # Un sollecito è "notificato" quando è arrivato ad almeno un destinatario: i "nuovi" sono gli stessi per tutti.
    adesso = utcnow()
    for email in inviati:
        for s in contenuti[email].solleciti:
            s.notificato_il = adesso
    esito["inviato"] = bool(inviati)
    esito["inviati"] = inviati
    if errori:
        esito["errore"] = "; ".join(errori)
        run.errori = errori
    run.stato = "ok" if not errori else "errore"
    run.contatori = {"destinatari": len(contenuti), "inviati": len(inviati), "esclusi": len(esclusi)}
    run.finished_at = utcnow()
    session.commit()
    return esito
