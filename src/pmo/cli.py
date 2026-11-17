"""Interfaccia a riga di comando: `pmo --help`."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import typer
from sqlalchemy import func, select

from pmo.config import get_settings

app = typer.Typer(help="PANificator 360: tracciabilità pratiche da Gmail e Google Drive", no_args_is_help=True)
utenti_app = typer.Typer(help="Gestione utenti")
caselle_app = typer.Typer(help="Gestione caselle collegate")
team_app = typer.Typer(help="Gestione dei team (gruppi di utenti che vedono le stesse pratiche)")
app.add_typer(utenti_app, name="utenti")
app.add_typer(caselle_app, name="caselle")
app.add_typer(team_app, name="team")


def _logging() -> None:
    logging.basicConfig(level=get_settings().log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _stampa(obj: object) -> None:
    typer.echo(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def _radice_progetto() -> Path:
    """Cartella con alembic.ini e config/: il sorgente (installazione editable) oppure la cartella corrente."""
    candidati = [Path(__file__).resolve().parents[2], Path.cwd()]
    for cand in candidati:
        if (cand / "alembic.ini").exists():
            return cand
    raise typer.BadParameter("alembic.ini non trovato: eseguire il comando dalla cartella del progetto")


@app.command("init-db")
def init_db() -> None:
    """Crea o aggiorna lo schema del database (alembic upgrade head) e le regole predefinite."""
    _logging()
    from alembic import command
    from alembic.config import Config

    radice = _radice_progetto()
    cfg = Config(str(radice / "alembic.ini"))
    cfg.set_main_option("script_location", str(radice / "alembic"))
    command.upgrade(cfg, "head")
    from pmo.db.session import session_scope
    from pmo.rules.definitions import assicura_regole

    with session_scope() as session:
        assicura_regole(session)
    typer.echo("Database pronto.")


@app.command()
def seed(file: Path | None = typer.Option(None, help="File YAML alternativo a config/seed.yaml")) -> None:
    """Carica clienti, prodotti, utenti e regole iniziali."""
    _logging()
    from pmo.db.session import session_scope
    from pmo.services.seed import carica_seed

    with session_scope() as session:
        _stampa(carica_seed(session, file))


@app.command("ricalcola-date")
def ricalcola_date() -> None:
    """Riporta apertura, fase corrente e ultima attività delle pratiche alle date delle mail e dei documenti."""
    _logging()
    from pmo.db.session import session_scope
    from pmo.services.pratiche import ricalcola_date as _ricalcola

    with session_scope() as session:
        _stampa(_ricalcola(session))


@app.command("gen-keys")
def gen_keys() -> None:
    """Genera PMO_SECRET_KEY e PMO_ENCRYPTION_KEY da incollare nel file .env."""
    import secrets

    from pmo.security import genera_chiave

    typer.echo(f"PMO_SECRET_KEY={secrets.token_urlsafe(48)}")
    typer.echo(f"PMO_ENCRYPTION_KEY={genera_chiave()}")


@app.command()
def serve(
    host: str = "0.0.0.0",  # noqa: S104
    port: int = 8000,
    reload: bool = typer.Option(False, help="Riparte da solo quando cambiano codice, template, CSS o JavaScript (sviluppo)"),
) -> None:
    """Avvia l'interfaccia web."""
    import uvicorn

    opzioni: dict = {}
    if reload:
        # Solo i sorgenti: guardare la cartella del progetto includerebbe .venv e il database. Anche template e file
        # statici fanno ripartire il server, così cambia la versione dei file statici e il browser non usa quelli vecchi.
        radice = _radice_progetto()
        opzioni = {
            "reload_dirs": [str(radice / "src"), str(radice / "config")],
            "reload_includes": ["*.py", "*.html", "*.css", "*.js", "*.yaml"],
        }
    uvicorn.run("pmo.web.app:create_app", host=host, port=port, reload=reload, factory=True, **opzioni)


@app.command("mcp")
def mcp_server(
    scrittura: bool = typer.Option(
        False, "--scrittura", help="Abilita anche le azioni che modificano i dati (conferma o scarta pratiche, note, stati, solleciti)"
    ),
    utente: str | None = typer.Option(None, help="Email a cui attribuire le modifiche (default: primo amministratore)"),
) -> None:
    """Avvia il server MCP (stdio) per collegare l'applicazione a Claude Desktop o Claude Code."""
    import os

    try:
        # avviato da Claude, il processo non parte dalla cartella del progetto: .env e il database SQLite sono relativi ad essa
        os.chdir(_radice_progetto())
    except typer.BadParameter:
        pass
    _logging()  # i log vanno su stderr: stdout è riservato al protocollo MCP
    from pmo.mcp_server import avvia, scrittura_abilitata

    avvia(scrittura=scrittura_abilitata(scrittura), utente_email=utente)


@app.command()
def sync(
    account: str | None = typer.Option(None, help="Email della casella (default: tutte)"),
    completa: bool = typer.Option(False, help="Ignora i segnalibri e rilegge l'intera finestra"),
    giorni: int | None = typer.Option(None, help="Finestra in giorni per l'importazione"),
    classifica: bool = typer.Option(True, help="Classifica con l'IA i messaggi nuovi"),
    regole: bool = typer.Option(True, help="Esegue le regole di sollecito al termine"),
) -> None:
    """Sincronizza Gmail e Drive, classifica e aggiorna i solleciti."""
    _logging()
    from pmo.db.models import Account
    from pmo.db.session import session_scope
    from pmo.services.sync import esegui_regole_tutto, sincronizza_account, sincronizza_tutto

    settings = get_settings()
    inizio = time.time()
    if account:
        with session_scope() as session:
            acc = session.scalar(select(Account).where(Account.email == account.lower()))
            if acc is None:
                raise typer.BadParameter(f"Casella {account} non trovata")
            account_id = acc.id
        esito = sincronizza_account(account_id, settings, completa=completa, giorni=giorni, classifica=classifica)
        if regole:
            esito["regole"] = esegui_regole_tutto()
    else:
        esito = sincronizza_tutto(settings, classifica=classifica, regole=regole, completa=completa, giorni=giorni)
    esito["durata_s"] = round(time.time() - inizio, 1)
    _stampa(esito)


@app.command()
def classifica(account: str | None = None, limite: int = 200) -> None:
    """Classifica con l'IA i messaggi e i documenti in attesa, senza scaricare nulla di nuovo."""
    _logging()
    from pmo.db.models import Account
    from pmo.db.session import session_scope
    from pmo.ingest.catalogo import carica_catalogo
    from pmo.ingest.classifier import crea_classificatore
    from pmo.ingest.drive_sync import classifica_documenti
    from pmo.ingest.gmail_sync import classifica_messaggi

    settings = get_settings()
    with session_scope() as session:
        q = select(Account).where(Account.attivo.is_(True))
        if account:
            q = q.where(Account.email == account.lower())
        classificatore = crea_classificatore(settings)
        catalogo = carica_catalogo(session, settings)
        esiti = {}
        for acc in session.scalars(q).all():
            run = classifica_messaggi(session, acc, settings, classificatore=classificatore, catalogo=catalogo, limite=limite)
            run2 = classifica_documenti(session, acc, settings, classificatore=classificatore, catalogo=catalogo, limite=limite)
            esiti[acc.email] = {"mail": run.contatori, "drive": run2.contatori, "errori": run.errori + run2.errori}
    _stampa(esiti)


@app.command("estrai-prova")
def estrai_prova(
    file: list[Path] = typer.Argument(..., help="PDF o Word di un ordine o di un'offerta"),
    offerta: bool = typer.Option(False, "--offerta", help="Il documento è una nostra offerta (altrimenti un ordine del cliente)"),
) -> None:
    """Fa leggere a Claude le righe di uno o più documenti e stampa il risultato, senza scrivere nel database.

    Serve a provare la lettura su documenti veri prima di lasciarla lavorare sulle caselle. Ogni lettura costa
    qualche centesimo: il listino del database viene passato come contesto, come nel giro automatico."""
    _logging()
    from sqlalchemy import select as sa_select

    from pmo.db.models import Articolo
    from pmo.db.session import session_scope
    from pmo.ingest.classifier import ErroreClassificazione
    from pmo.ingest.estrattore import EstrattoreClaude
    from pmo.ingest.estrazione_righe import MAX_ARTICOLI_CONTESTO, _da_byte
    from pmo.services.estrazioni import avvisi_risultato, leggibile

    settings = get_settings()
    letti = []
    for percorso in file:
        mime = "application/pdf" if percorso.suffix.lower() == ".pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        if not leggibile(percorso.name, mime):
            raise typer.BadParameter(f"{percorso.name}: servono PDF o Word (.docx)")
        letti.append(_da_byte(percorso.name, mime, percorso.read_bytes()))
    with session_scope() as session:
        articoli = session.scalars(sa_select(Articolo).where(Articolo.attivo.is_(True)).order_by(Articolo.codice).limit(MAX_ARTICOLI_CONTESTO)).all()
        contesto = "Listino di La Traccia (codice | descrizione | prezzo):\n" + "\n".join(
            f"{a.codice} | {a.descrizione} | {a.prezzo_listino if a.prezzo_listino is not None else ''}" for a in articoli
        ) if articoli else ""
    estrattore = EstrattoreClaude(settings)
    try:
        risultato = estrattore.estrai("offerta" if offerta else "ordine", letti, contesto).model_dump()
    except ErroreClassificazione as exc:
        typer.echo(f"Lettura non riuscita: {exc}", err=True)
        raise typer.Exit(1) from exc
    risultato["avvisi"] = [*avvisi_risultato(risultato), *risultato.get("avvisi", [])]
    _stampa({"modello": estrattore.modello, "token": estrattore.uso.to_dict(), "risultato": risultato})


@app.command()
def regole() -> None:
    """Valuta le regole di sollecito su tutte le pratiche."""
    _logging()
    from pmo.services.sync import esegui_regole_tutto

    _stampa(esegui_regole_tutto())


@app.command()
def digest(
    anteprima: bool = typer.Option(False, help="Mostra il digest senza inviarlo"),
    a: list[str] = typer.Option(None, "--a", help="Destinatario (ripetibile)"),
) -> None:
    """Invia (o mostra) il digest dei solleciti: uno per destinatario, con ciò che quel destinatario può vedere."""
    _logging()
    from pmo.db.session import session_scope
    from pmo.services.digest import invia_digest

    with session_scope() as session:
        esito = invia_digest(session, get_settings(), destinatari=list(a) if a else None, solo_anteprima=anteprima)
    for email, testo in (esito.pop("anteprime", None) or {}).items():
        typer.echo(f"===== Digest per {email} =====")
        typer.echo(testo + "\n")
    _stampa(esito)


@app.command()
def stato() -> None:
    """Riepilogo rapido: pratiche per stato, solleciti aperti, elementi da verificare, caselle."""
    from pmo.db.models import Account, Documento, Messaggio, Pratica, Sollecito
    from pmo.db.session import session_scope

    with session_scope() as session:
        per_stato = dict(session.execute(select(Pratica.stato, func.count()).group_by(Pratica.stato)).all())
        solleciti = dict(session.execute(select(Sollecito.stato, func.count()).group_by(Sollecito.stato)).all())
        messaggi = dict(session.execute(select(Messaggio.stato, func.count()).group_by(Messaggio.stato)).all())
        documenti = dict(session.execute(select(Documento.stato, func.count()).group_by(Documento.stato)).all())
        caselle = [
            {"email": a.email, "collegata": a.collegato, "ultima_sync": a.gmail_last_sync_at, "stato": a.last_sync_status}
            for a in session.scalars(select(Account)).all()
        ]
    _stampa({"pratiche": per_stato, "solleciti": solleciti, "messaggi": messaggi, "documenti": documenti, "caselle": caselle})


@app.command()
def scheduler() -> None:
    """Esegue lo scheduler in primo piano (sincronizzazione periodica e digest)."""
    _logging()
    from pmo.scheduler import crea_scheduler

    sched = crea_scheduler(get_settings())
    sched.start()
    typer.echo("Scheduler avviato (Ctrl+C per uscire)")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        sched.shutdown()


@utenti_app.command("aggiungi")
def utenti_aggiungi(
    email: str,
    nome: str = "",
    ruolo: str = typer.Option("", help="amministratore, direzione, amministrazione, responsabile, commerciale, operativo"),
    admin: bool = typer.Option(False, help="Equivale a --ruolo amministratore"),
) -> None:
    """Crea o aggiorna un utente abilitato all'accesso."""
    from pmo.db.models import User
    from pmo.db.session import session_scope
    from pmo.permessi import Ruolo, ruolo_valido

    scelto = Ruolo.AMMINISTRATORE if admin else ruolo_valido(ruolo) if ruolo else None
    if ruolo and scelto is None:
        raise typer.BadParameter(f"Ruolo '{ruolo}' non valido: usa uno tra {', '.join(r.value for r in Ruolo)}")
    with session_scope() as session:
        utente = session.scalar(select(User).where(User.email == email.lower()))
        if utente is None:
            utente = User(email=email.lower(), ruolo=Ruolo.OPERATIVO.value)
            session.add(utente)
        utente.nome = nome or utente.nome or email
        if scelto is not None:
            utente.ruolo = scelto.value
        utente.attivo = True
        ruolo_finale = utente.ruolo
    typer.echo(f"Utente {email} pronto ({ruolo_finale}).")


@team_app.command("crea")
def team_crea(nome: str, descrizione: str = "") -> None:
    """Crea un team (o lo riattiva, se esiste già)."""
    from pmo.db.models import Team
    from pmo.db.session import session_scope

    with session_scope() as session:
        team = session.scalar(select(Team).where(Team.nome == nome.strip()))
        if team is None:
            team = Team(nome=nome.strip())
            session.add(team)
        team.descrizione = descrizione or team.descrizione or ""
        team.attivo = True
    typer.echo(f"Team '{nome}' pronto.")


@team_app.command("aggiungi")
def team_aggiungi(nome: str, email: str) -> None:
    """Aggiunge un utente (già esistente) a un team."""
    from pmo.db.models import Team, User
    from pmo.db.session import session_scope

    with session_scope() as session:
        team = session.scalar(select(Team).where(Team.nome == nome.strip()))
        utente = session.scalar(select(User).where(User.email == email.strip().lower()))
        if team is None:
            raise typer.BadParameter(f"Team '{nome}' inesistente: crealo con `pmo team crea`")
        if utente is None:
            raise typer.BadParameter(f"Utente {email} inesistente: crealo con `pmo utenti aggiungi`")
        if utente not in team.membri:
            team.membri.append(utente)
    typer.echo(f"{email} aggiunto al team '{nome}'.")


@team_app.command("togli")
def team_togli(nome: str, email: str) -> None:
    """Toglie un utente da un team."""
    from pmo.db.models import Team, User
    from pmo.db.session import session_scope

    with session_scope() as session:
        team = session.scalar(select(Team).where(Team.nome == nome.strip()))
        utente = session.scalar(select(User).where(User.email == email.strip().lower()))
        if team is None or utente is None:
            raise typer.BadParameter("Team o utente inesistente")
        if utente in team.membri:
            team.membri.remove(utente)
    typer.echo(f"{email} tolto dal team '{nome}'.")


@team_app.command("elenco")
def team_elenco() -> None:
    """I team con i loro membri."""
    from pmo.db.models import Team
    from pmo.db.session import session_scope

    with session_scope() as session:
        righe = [
            {"nome": t.nome, "attivo": t.attivo, "descrizione": t.descrizione, "membri": [f"{u.email} ({u.ruolo})" for u in t.membri]}
            for t in session.scalars(select(Team).order_by(Team.nome)).all()
        ]
    _stampa(righe)


@caselle_app.command("elenco")
def caselle_elenco() -> None:
    from pmo.db.models import Account
    from pmo.db.session import session_scope

    with session_scope() as session:
        righe = [
            {
                "id": a.id,
                "email": a.email,
                "collegata": a.collegato,
                "scopes": a.scopes,
                "cartelle": [f.nome or f.drive_folder_id for f in a.watched_folders],
                "ultima_sync": a.gmail_last_sync_at,
                "stato": a.last_sync_status,
                "errore": a.last_error,
            }
            for a in session.scalars(select(Account)).all()
        ]
    _stampa(righe)


@caselle_app.command("collega")
def caselle_collega(porta: int = 8765) -> None:
    """Collega una casella dal terminale (apre il browser su questa macchina)."""
    _logging()
    from google_auth_oauthlib.flow import InstalledAppFlow

    from pmo.config import OPENID_SCOPES
    from pmo.connectors.gmail import GmailClient
    from pmo.connectors.google_oauth import credentials_to_dict, salva_token
    from pmo.db.models import Account
    from pmo.db.session import session_scope

    settings = get_settings()
    if not settings.google_configured:
        raise typer.BadParameter("Impostare PMO_GOOGLE_CLIENT_ID e PMO_GOOGLE_CLIENT_SECRET")
    config = {
        "installed": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(config, scopes=OPENID_SCOPES + settings.gmail_scopes)
    creds = flow.run_local_server(port=porta, access_type="offline", prompt="consent")
    profilo = GmailClient(creds).profile()
    email = profilo["emailAddress"].lower()
    with session_scope() as session:
        account = session.scalar(select(Account).where(Account.email == email))
        if account is None:
            account = Account(email=email, display_name=email)
            session.add(account)
        salva_token(account, credentials_to_dict(creds))
        account.attivo = True
    typer.echo(f"Casella {email} collegata.")


@caselle_app.command("cartella")
def caselle_cartella(email: str, cartella: str, ricorsivo: bool = True) -> None:
    """Aggiunge una cartella Drive (ID o URL) da monitorare per la casella indicata."""
    from pmo.connectors.drive import DriveClient, estrai_folder_id
    from pmo.connectors.google_oauth import credentials_per_account
    from pmo.db.models import Account, WatchedFolder
    from pmo.db.session import session_scope

    with session_scope() as session:
        account = session.scalar(select(Account).where(Account.email == email.lower()))
        if account is None:
            raise typer.BadParameter(f"Casella {email} non trovata")
        folder_id = estrai_folder_id(cartella)
        meta = DriveClient(credentials_per_account(account)).get_file(folder_id)
        wf = WatchedFolder(account_id=account.id, drive_folder_id=folder_id, nome=meta.get("name", ""), ricorsivo=ricorsivo)
        session.add(wf)
    typer.echo(f"Cartella '{meta.get('name')}' monitorata per {email}.")


if __name__ == "__main__":
    app()
