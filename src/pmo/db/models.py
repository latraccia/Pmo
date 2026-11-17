"""Modello dati del PMO.

Entità principali:
- User, Team: le persone, con un ruolo ciascuna, raggruppate in team; i team decidono chi vede quali pratiche.
- Account: casella Gmail/Drive collegata (token cifrati, segnalibri di sincronizzazione), personale o di team.
- Pratica: la "commessa" seguita dalla stima alla fattura, con codice univoco PMO-AAAA-NNNN.
- Evento: cronologia della pratica (ogni evento punta alla fonte: mail, documento, utente, sistema).
- Messaggio / Documento: elementi importati da Gmail e Drive, classificati e collegati alle pratiche.
- Sollecito: segnalazione generata dalle regole (ordine senza specifiche, scadenze, ecc.).
- Offerta (con revisioni e righe) e Ordine (con righe): il contenuto economico della commessa, riga per riga;
  le allocazioni legano le righe d'ordine alle righe dell'offerta e ne ricavano il residuo.
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pmo.db.base import Base, JSONType, TZDateTime, utcnow


class StatoPratica(enum.StrEnum):
    BOZZA = "bozza"
    STIMA = "stima"
    OFFERTA = "offerta"
    ORDINE = "ordine"
    SPECIFICHE = "specifiche"
    SVILUPPO = "sviluppo"
    CONSEGNA = "consegna"
    FATTURAZIONE = "fatturazione"
    CHIUSA = "chiusa"
    SOSPESA = "sospesa"
    ANNULLATA = "annullata"


# Ordine delle fasi "attive": una pratica avanza solo in avanti in automatico.
FASI_ORDINATE: list[StatoPratica] = [
    StatoPratica.BOZZA,
    StatoPratica.STIMA,
    StatoPratica.OFFERTA,
    StatoPratica.ORDINE,
    StatoPratica.SPECIFICHE,
    StatoPratica.SVILUPPO,
    StatoPratica.CONSEGNA,
    StatoPratica.FATTURAZIONE,
    StatoPratica.CHIUSA,
]
STATI_ATTIVI: set[StatoPratica] = set(FASI_ORDINATE) - {StatoPratica.CHIUSA}
STATI_TERMINALI: set[StatoPratica] = {StatoPratica.CHIUSA, StatoPratica.ANNULLATA}

class TipoPratica(enum.StrEnum):
    """Il tipo di lavoro della commessa: una fornitura (licenze, sviluppo, hardware, avviamento) o una manutenzione
    (assistenza e canoni, di solito una commessa per periodo)."""

    FORNITURA = "fornitura"
    MANUTENZIONE = "manutenzione"


ETICHETTE_TIPO_PRATICA: dict[str, str] = {"fornitura": "Fornitura", "manutenzione": "Manutenzione"}

ETICHETTE_STATO: dict[str, str] = {
    "bozza": "Bozza",
    "stima": "Stima",
    "offerta": "Offerta",
    "ordine": "Ordine",
    "specifiche": "Specifiche",
    "sviluppo": "Sviluppo",
    "consegna": "Consegna",
    "fatturazione": "Fatturazione",
    "chiusa": "Chiusa",
    "sospesa": "Sospesa",
    "annullata": "Annullata",
}


class TipoEvento(enum.StrEnum):
    STIMA_RICHIESTA = "stima_richiesta"
    STIMA_INVIATA = "stima_inviata"
    OFFERTA_INVIATA = "offerta_inviata"
    OFFERTA_ACCETTATA = "offerta_accettata"
    OFFERTA_RIFIUTATA = "offerta_rifiutata"
    ORDINE_RICEVUTO = "ordine_ricevuto"
    SPECIFICHE_RICHIESTE = "specifiche_richieste"
    SPECIFICHE_RICEVUTE = "specifiche_ricevute"
    SPECIFICHE_INVIATE = "specifiche_inviate"
    SVILUPPO_AVVIATO = "sviluppo_avviato"
    SAL = "sal"
    CONSEGNA_EFFETTUATA = "consegna_effettuata"
    COLLAUDO = "collaudo"
    SAL_FIRMATO = "sal_firmato"
    FATTURA_EMESSA = "fattura_emessa"
    PAGAMENTO_RICEVUTO = "pagamento_ricevuto"
    SCADENZA = "scadenza"
    RIUNIONE = "riunione"
    RICHIESTA_INFORMAZIONI = "richiesta_informazioni"
    SOLLECITO_RICEVUTO = "sollecito_ricevuto"
    SOLLECITO_INVIATO = "sollecito_inviato"
    PROMEMORIA = "promemoria"
    DOCUMENTO = "documento"
    NOTA = "nota"
    CAMBIO_STATO = "cambio_stato"
    ANNULLAMENTO = "annullamento"
    ALTRO = "altro"


ETICHETTE_EVENTO: dict[str, str] = {
    "stima_richiesta": "Richiesta di stima",
    "stima_inviata": "Stima inviata",
    "offerta_inviata": "Offerta inviata",
    "offerta_accettata": "Offerta accettata",
    "offerta_rifiutata": "Offerta rifiutata",
    "ordine_ricevuto": "Ordine ricevuto",
    "specifiche_richieste": "Specifiche richieste",
    "specifiche_ricevute": "Specifiche ricevute",
    "specifiche_inviate": "Specifiche inviate",
    "sviluppo_avviato": "Sviluppo avviato",
    "sal": "Stato avanzamento lavori",
    "consegna_effettuata": "Consegna / rilascio",
    "collaudo": "Collaudo",
    "sal_firmato": "SAL firmato / accettazione",
    "fattura_emessa": "Fattura emessa",
    "pagamento_ricevuto": "Pagamento ricevuto",
    "scadenza": "Scadenza",
    "riunione": "Riunione",
    "richiesta_informazioni": "Richiesta informazioni",
    "sollecito_ricevuto": "Sollecito ricevuto",
    "sollecito_inviato": "Sollecito inviato",
    "promemoria": "Promemoria",
    "documento": "Documento",
    "nota": "Nota",
    "cambio_stato": "Cambio di stato",
    "annullamento": "Annullamento",
    "altro": "Altro",
}

# Fase implicata da ciascun tipo di evento (None = nessun avanzamento automatico).
FASE_PER_EVENTO: dict[str, StatoPratica | None] = {
    TipoEvento.STIMA_RICHIESTA: StatoPratica.STIMA,
    TipoEvento.STIMA_INVIATA: StatoPratica.STIMA,
    TipoEvento.OFFERTA_INVIATA: StatoPratica.OFFERTA,
    TipoEvento.OFFERTA_ACCETTATA: StatoPratica.OFFERTA,
    TipoEvento.ORDINE_RICEVUTO: StatoPratica.ORDINE,
    TipoEvento.SPECIFICHE_RICHIESTE: StatoPratica.ORDINE,
    TipoEvento.SPECIFICHE_RICEVUTE: StatoPratica.SPECIFICHE,
    TipoEvento.SPECIFICHE_INVIATE: StatoPratica.SPECIFICHE,
    TipoEvento.SVILUPPO_AVVIATO: StatoPratica.SVILUPPO,
    TipoEvento.SAL: StatoPratica.SVILUPPO,
    TipoEvento.CONSEGNA_EFFETTUATA: StatoPratica.CONSEGNA,
    TipoEvento.COLLAUDO: StatoPratica.CONSEGNA,
    TipoEvento.SAL_FIRMATO: StatoPratica.CONSEGNA,
    TipoEvento.FATTURA_EMESSA: StatoPratica.FATTURAZIONE,
    TipoEvento.PAGAMENTO_RICEVUTO: StatoPratica.FATTURAZIONE,
}


class TipoFonte(enum.StrEnum):
    EMAIL = "email"
    DRIVE = "drive"
    MANUALE = "manuale"
    SISTEMA = "sistema"


class StatoVerifica(enum.StrEnum):
    PROPOSTO = "proposto"
    CONFERMATO = "confermato"
    RIFIUTATO = "rifiutato"


class StatoMessaggio(enum.StrEnum):
    NUOVO = "nuovo"
    CLASSIFICATO = "classificato"
    DA_RIVEDERE = "da_rivedere"
    IGNORATO = "ignorato"
    ERRORE = "errore"


class StatoSollecito(enum.StrEnum):
    APERTO = "aperto"
    IN_CARICO = "in_carico"
    RISOLTO = "risolto"
    SILENZIATO = "silenziato"


class Gravita(enum.StrEnum):
    INFO = "info"
    AVVISO = "avviso"
    CRITICO = "critico"


class TipoDocumento(enum.StrEnum):
    STIMA = "stima"
    OFFERTA = "offerta"
    ORDINE = "ordine"
    SPECIFICA = "specifica"
    CONTRATTO = "contratto"
    SAL = "sal"
    VERBALE = "verbale"
    FATTURA = "fattura"
    ALTRO = "altro"


# ---------------------------------------------------------------------------------------------
# Utenti e caselle
# ---------------------------------------------------------------------------------------------


team_membri = Table(
    "team_membri",
    Base.metadata,
    Column("team_id", ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True),
    Column("user_id", ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
)


class Team(Base):
    """Gruppo di persone che seguono insieme un insieme di pratiche (e, se serve, una casella condivisa)."""

    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(120), unique=True)
    descrizione: Mapped[str] = mapped_column(Text, default="")
    attivo: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)

    membri: Mapped[list[User]] = relationship(secondary=team_membri, back_populates="teams", order_by="User.nome")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    nome: Mapped[str] = mapped_column(String(255), default="")
    # amministratore | direzione | amministrazione | responsabile | commerciale | operativo (vedi pmo.permessi)
    ruolo: Mapped[str] = mapped_column(String(32), default="operativo")
    attivo: Mapped[bool] = mapped_column(Boolean, default=True)
    google_sub: Mapped[str | None] = mapped_column(String(64))
    riceve_digest: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    accounts: Mapped[list[Account]] = relationship(back_populates="owner")
    teams: Mapped[list[Team]] = relationship(secondary=team_membri, back_populates="membri", order_by="Team.nome")

    @property
    def is_admin(self) -> bool:
        return self.ruolo in ("amministratore", "admin")  # "admin": valore precedente alla migrazione 0002


class Account(Base):
    """Casella Gmail (e Drive) collegata al sistema."""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(255), default="")
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"), index=True)  # casella di team
    attivo: Mapped[bool] = mapped_column(Boolean, default=True)
    token_cifrato: Mapped[str | None] = mapped_column(Text)  # JSON cifrato con Fernet
    scopes: Mapped[list] = mapped_column(JSONType, default=list)
    query_filter: Mapped[str] = mapped_column(String(500), default="")  # filtro Gmail aggiuntivo
    sync_gmail: Mapped[bool] = mapped_column(Boolean, default=True)
    sync_drive: Mapped[bool] = mapped_column(Boolean, default=True)
    gmail_history_id: Mapped[str | None] = mapped_column(String(32))
    gmail_last_sync_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    gmail_sync_from: Mapped[date | None] = mapped_column(Date)
    drive_last_sync_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    last_sync_status: Mapped[str] = mapped_column(String(32), default="mai")
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow, onupdate=utcnow)

    owner: Mapped[User | None] = relationship(back_populates="accounts")
    team: Mapped[Team | None] = relationship()
    watched_folders: Mapped[list[WatchedFolder]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )

    @property
    def collegato(self) -> bool:
        return bool(self.token_cifrato)


class WatchedFolder(Base):
    """Cartella Drive monitorata per una casella."""

    __tablename__ = "watched_folders"
    __table_args__ = (UniqueConstraint("account_id", "drive_folder_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    drive_folder_id: Mapped[str] = mapped_column(String(128))
    nome: Mapped[str] = mapped_column(String(255), default="")
    ricorsivo: Mapped[bool] = mapped_column(Boolean, default=True)
    attivo: Mapped[bool] = mapped_column(Boolean, default=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)

    account: Mapped[Account] = relationship(back_populates="watched_folders")


# ---------------------------------------------------------------------------------------------
# Anagrafiche
# ---------------------------------------------------------------------------------------------


class Cliente(Base):
    __tablename__ = "clienti"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(255), unique=True)
    alias: Mapped[list] = mapped_column(JSONType, default=list)
    domini: Mapped[list] = mapped_column(JSONType, default=list)
    tipo: Mapped[str] = mapped_column(String(32), default="altro")  # sanita | pa | partner | altro
    ragione_sociale: Mapped[str] = mapped_column(String(255), default="")
    partita_iva: Mapped[str] = mapped_column(String(20), default="")
    codice_fiscale: Mapped[str] = mapped_column(String(20), default="")
    indirizzo: Mapped[str] = mapped_column(String(255), default="")
    citta: Mapped[str] = mapped_column(String(120), default="")
    telefono: Mapped[str] = mapped_column(String(50), default="")
    email: Mapped[str] = mapped_column(String(255), default="")
    pec: Mapped[str] = mapped_column(String(255), default="")
    codice_oracle: Mapped[str] = mapped_column(String(50), default="")  # codice nel gestionale aziendale
    codice_vte: Mapped[str] = mapped_column(String(50), default="")  # identificativo nel CRM VTE
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"))  # team di riferimento
    note: Mapped[str] = mapped_column(Text, default="")
    attivo: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)

    pratiche: Mapped[list[Pratica]] = relationship(back_populates="cliente")
    team: Mapped[Team | None] = relationship()
    contatti: Mapped[list[ContattoCliente]] = relationship(
        back_populates="cliente", cascade="all, delete-orphan", order_by="ContattoCliente.nome"
    )


class ContattoCliente(Base):
    """Referente di un cliente: il suo indirizzo aiuta anche a riconoscere le mail del cliente."""

    __tablename__ = "contatti_cliente"

    id: Mapped[int] = mapped_column(primary_key=True)
    cliente_id: Mapped[int] = mapped_column(ForeignKey("clienti.id", ondelete="CASCADE"), index=True)
    nome: Mapped[str] = mapped_column(String(255))
    ruolo: Mapped[str] = mapped_column(String(120), default="")
    email: Mapped[str] = mapped_column(String(255), default="", index=True)
    telefono: Mapped[str] = mapped_column(String(50), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    attivo: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)

    cliente: Mapped[Cliente] = relationship(back_populates="contatti")


class Prodotto(Base):
    """Prodotto o area (LURTO, Gepadial, T-MED, portali regionali, ...)."""

    __tablename__ = "prodotti"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(255), unique=True)
    alias: Mapped[list] = mapped_column(JSONType, default=list)
    descrizione: Mapped[str] = mapped_column(Text, default="")
    attivo: Mapped[bool] = mapped_column(Boolean, default=True)

    pratiche: Mapped[list[Pratica]] = relationship(back_populates="prodotto")
    articoli: Mapped[list[Articolo]] = relationship(back_populates="prodotto", order_by="Articolo.codice")


class Articolo(Base):
    """Voce del listino di vendita: un codice con la sua descrizione, il prezzo di listino e l'IVA.

    Le righe di offerte e ordini possono partire da un articolo: codice, descrizione, prezzo e IVA vengono copiati
    nella riga (che resta modificabile e non cambia se poi cambia il listino). Le righe libere restano possibili.
    """

    __tablename__ = "articoli"

    id: Mapped[int] = mapped_column(primary_key=True)
    codice: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    descrizione: Mapped[str] = mapped_column(Text, default="")
    prodotto_id: Mapped[int | None] = mapped_column(ForeignKey("prodotti.id", ondelete="SET NULL"), index=True)
    unita: Mapped[str] = mapped_column(String(20), default="")
    prezzo_listino: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    aliquota_iva: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    note: Mapped[str] = mapped_column(Text, default="")
    attivo: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)

    prodotto: Mapped[Prodotto | None] = relationship(back_populates="articoli")

    @property
    def etichetta(self) -> str:
        return f"{self.codice} · {self.descrizione}" if self.descrizione else self.codice


# ---------------------------------------------------------------------------------------------
# Pratiche ed eventi
# ---------------------------------------------------------------------------------------------


class Pratica(Base):
    __tablename__ = "pratiche"

    id: Mapped[int] = mapped_column(primary_key=True)
    codice: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    titolo: Mapped[str] = mapped_column(String(300))
    descrizione: Mapped[str] = mapped_column(Text, default="")
    cliente_id: Mapped[int | None] = mapped_column(ForeignKey("clienti.id"), index=True)
    prodotto_id: Mapped[int | None] = mapped_column(ForeignKey("prodotti.id"), index=True)
    tipo: Mapped[str] = mapped_column(String(16), default=TipoPratica.FORNITURA, server_default="fornitura", index=True)
    stato: Mapped[str] = mapped_column(String(32), default=StatoPratica.BOZZA, index=True)
    stato_dal: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    stato_verifica: Mapped[str] = mapped_column(String(16), default=StatoVerifica.CONFERMATO, index=True)
    origine: Mapped[str] = mapped_column(String(16), default="manuale")  # manuale | ai | tag
    priorita: Mapped[str] = mapped_column(String(16), default="media")  # alta | media | bassa
    responsabile_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    sostituto_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"), index=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    importo_stima: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    importo_offerta: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    importo_ordine: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    importo_fatturato: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    data_apertura: Mapped[date] = mapped_column(Date, default=date.today)
    data_scadenza: Mapped[date | None] = mapped_column(Date, index=True)
    data_chiusura: Mapped[date | None] = mapped_column(Date)
    riferimenti: Mapped[dict] = mapped_column(JSONType, default=dict)  # numero ordine, CIG, protocollo, SIA, VTE...
    drive_folder_id: Mapped[str | None] = mapped_column(String(128))
    drive_folder_url: Mapped[str | None] = mapped_column(String(500))
    note: Mapped[str] = mapped_column(Text, default="")
    ultimo_evento_at: Mapped[datetime | None] = mapped_column(TZDateTime, index=True)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow, onupdate=utcnow)

    cliente: Mapped[Cliente | None] = relationship(back_populates="pratiche")
    prodotto: Mapped[Prodotto | None] = relationship(back_populates="pratiche")
    responsabile: Mapped[User | None] = relationship(foreign_keys=[responsabile_user_id])
    sostituto: Mapped[User | None] = relationship(foreign_keys=[sostituto_user_id])
    team: Mapped[Team | None] = relationship()
    eventi: Mapped[list[Evento]] = relationship(
        back_populates="pratica", cascade="all, delete-orphan", order_by="Evento.data_evento"
    )
    messaggi: Mapped[list[Messaggio]] = relationship(back_populates="pratica")
    documenti: Mapped[list[Documento]] = relationship(back_populates="pratica")
    solleciti: Mapped[list[Sollecito]] = relationship(back_populates="pratica", cascade="all, delete-orphan")
    offerte: Mapped[list[Offerta]] = relationship(back_populates="pratica", cascade="all, delete-orphan", order_by="Offerta.id")
    ordini: Mapped[list[Ordine]] = relationship(back_populates="pratica", cascade="all, delete-orphan", order_by="Ordine.id")

    @property
    def etichetta_stato(self) -> str:
        return ETICHETTE_STATO.get(self.stato, self.stato)

    @property
    def etichetta_tipo(self) -> str:
        return ETICHETTE_TIPO_PRATICA.get(self.tipo, self.tipo)

    @property
    def ordine_principale(self) -> Ordine | None:
        """Di regola una commessa ha un ordine solo: il primo registrato e non annullato. Gli altri sono integrazioni
        (un ordine aggiuntivo o una tranche della stessa offerta): ammesse, ma segnalate."""
        return next((o for o in sorted(self.ordini, key=lambda o: o.id or 0) if o.avanzamento != "annullato"), None)

    @property
    def attiva(self) -> bool:
        return self.stato in {s.value for s in STATI_ATTIVI}

    @property
    def tag(self) -> str:
        return f"[{self.codice}]"


class Evento(Base):
    __tablename__ = "eventi"
    __table_args__ = (Index("ix_eventi_pratica_data", "pratica_id", "data_evento"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    pratica_id: Mapped[int] = mapped_column(ForeignKey("pratiche.id", ondelete="CASCADE"), index=True)
    tipo: Mapped[str] = mapped_column(String(40), index=True)
    data_evento: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    titolo: Mapped[str] = mapped_column(String(300))
    descrizione: Mapped[str] = mapped_column(Text, default="")
    fonte_tipo: Mapped[str] = mapped_column(String(16), default=TipoFonte.MANUALE)
    fonte_id: Mapped[str | None] = mapped_column(String(255))
    fonte_url: Mapped[str | None] = mapped_column(String(1000))
    chiave: Mapped[str | None] = mapped_column(String(400), unique=True)  # idempotenza (fonte:tipo)
    messaggio_id: Mapped[int | None] = mapped_column(ForeignKey("messaggi.id", ondelete="SET NULL"))
    documento_id: Mapped[int | None] = mapped_column(ForeignKey("documenti.id", ondelete="SET NULL"))
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    importo: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    scadenza: Mapped[date | None] = mapped_column(Date)
    confidenza: Mapped[float | None] = mapped_column()
    stato_verifica: Mapped[str] = mapped_column(String(16), default=StatoVerifica.CONFERMATO, index=True)
    dati: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)

    pratica: Mapped[Pratica] = relationship(back_populates="eventi")
    messaggio: Mapped[Messaggio | None] = relationship(foreign_keys=[messaggio_id])
    documento: Mapped[Documento | None] = relationship(foreign_keys=[documento_id])

    @property
    def etichetta_tipo(self) -> str:
        return ETICHETTE_EVENTO.get(self.tipo, self.tipo)


# ---------------------------------------------------------------------------------------------
# Messaggi e documenti importati
# ---------------------------------------------------------------------------------------------


class Messaggio(Base):
    __tablename__ = "messaggi"
    __table_args__ = (
        UniqueConstraint("account_id", "gmail_message_id"),
        Index("ix_messaggi_thread", "account_id", "gmail_thread_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    gmail_message_id: Mapped[str] = mapped_column(String(32))
    gmail_thread_id: Mapped[str] = mapped_column(String(32))
    message_id_header: Mapped[str | None] = mapped_column(String(500))
    subject: Mapped[str] = mapped_column(String(1000), default="")
    from_email: Mapped[str] = mapped_column(String(255), default="", index=True)
    from_name: Mapped[str] = mapped_column(String(255), default="")
    to_emails: Mapped[list] = mapped_column(JSONType, default=list)
    cc_emails: Mapped[list] = mapped_column(JSONType, default=list)
    data: Mapped[datetime] = mapped_column(TZDateTime, index=True)
    snippet: Mapped[str] = mapped_column(Text, default="")
    body_text: Mapped[str] = mapped_column(Text, default="")
    labels: Mapped[list] = mapped_column(JSONType, default=list)
    has_attachments: Mapped[bool] = mapped_column(Boolean, default=False)
    attachments: Mapped[list] = mapped_column(JSONType, default=list)
    attachments_text: Mapped[str] = mapped_column(Text, default="")
    inviato_da_noi: Mapped[bool] = mapped_column(Boolean, default=False)
    tag_codice: Mapped[str | None] = mapped_column(String(20), index=True)
    stato: Mapped[str] = mapped_column(String(16), default=StatoMessaggio.NUOVO, index=True)
    motivo: Mapped[str] = mapped_column(String(500), default="")
    classificazione: Mapped[dict] = mapped_column(JSONType, default=dict)
    pratica_id: Mapped[int | None] = mapped_column(ForeignKey("pratiche.id", ondelete="SET NULL"), index=True)
    link_origine: Mapped[str | None] = mapped_column(String(16))  # tag | thread | ai | manuale
    confidenza: Mapped[float | None] = mapped_column()
    processed_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)

    account: Mapped[Account] = relationship()
    pratica: Mapped[Pratica | None] = relationship(back_populates="messaggi")

    @property
    def gmail_url(self) -> str:
        return f"https://mail.google.com/mail/u/0/#all/{self.gmail_message_id}"


class Documento(Base):
    __tablename__ = "documenti"
    __table_args__ = (UniqueConstraint("account_id", "drive_file_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    watched_folder_id: Mapped[int | None] = mapped_column(ForeignKey("watched_folders.id", ondelete="SET NULL"))
    drive_file_id: Mapped[str] = mapped_column(String(128))
    nome: Mapped[str] = mapped_column(String(500))
    mime_type: Mapped[str] = mapped_column(String(255), default="")
    percorso: Mapped[str] = mapped_column(String(1000), default="")
    parent_folder_id: Mapped[str | None] = mapped_column(String(128))
    web_view_link: Mapped[str | None] = mapped_column(String(1000))
    dimensione: Mapped[int | None] = mapped_column(Integer)
    modified_time: Mapped[datetime | None] = mapped_column(TZDateTime, index=True)
    created_time: Mapped[datetime | None] = mapped_column(TZDateTime)
    ultimo_autore: Mapped[str] = mapped_column(String(255), default="")
    estratto: Mapped[str] = mapped_column(Text, default="")
    tag_codice: Mapped[str | None] = mapped_column(String(20), index=True)
    tipo_documento: Mapped[str | None] = mapped_column(String(32))
    stato: Mapped[str] = mapped_column(String(16), default=StatoMessaggio.NUOVO, index=True)
    motivo: Mapped[str] = mapped_column(String(500), default="")
    classificazione: Mapped[dict] = mapped_column(JSONType, default=dict)
    pratica_id: Mapped[int | None] = mapped_column(ForeignKey("pratiche.id", ondelete="SET NULL"), index=True)
    link_origine: Mapped[str | None] = mapped_column(String(16))
    confidenza: Mapped[float | None] = mapped_column()
    processed_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)

    account: Mapped[Account] = relationship()
    pratica: Mapped[Pratica | None] = relationship(back_populates="documenti")


# ---------------------------------------------------------------------------------------------
# Offerte, ordini e righe
# ---------------------------------------------------------------------------------------------


class StatoRevisione(enum.StrEnum):
    BOZZA = "bozza"
    INVIATA = "inviata"
    ACCETTATA = "accettata"
    RIFIUTATA = "rifiutata"
    SUPERATA = "superata"


class Conformita(enum.StrEnum):
    DA_VERIFICARE = "da_verificare"
    CONFORME = "conforme"
    SCOSTAMENTI = "scostamenti"
    NON_DETERMINABILE = "non_determinabile"


class Avanzamento(enum.StrEnum):
    APERTO = "aperto"
    IN_LAVORAZIONE = "in_lavorazione"
    CONSEGNATO = "consegnato"
    CHIUSO = "chiuso"
    ANNULLATO = "annullato"


class Attesa(enum.StrEnum):
    NESSUNA = "nessuna"
    CLIENTE = "cliente"  # il cliente aspetta noi
    NOI = "noi"  # noi aspettiamo il cliente


ETICHETTE_REVISIONE = {"bozza": "Bozza", "inviata": "Inviata", "accettata": "Accettata", "rifiutata": "Rifiutata", "superata": "Superata"}
ETICHETTE_CONFORMITA = {
    "da_verificare": "Da verificare",
    "conforme": "Conforme",
    "scostamenti": "Scostamenti",
    "non_determinabile": "Non determinabile",
}
ETICHETTE_AVANZAMENTO = {
    "aperto": "Aperto",
    "in_lavorazione": "In lavorazione",
    "consegnato": "Consegnato",
    "chiuso": "Chiuso",
    "annullato": "Annullato",
}
ETICHETTE_ATTESA = {"nessuna": "Nessuna", "cliente": "Il cliente aspetta noi", "noi": "Aspettiamo il cliente"}

CENTESIMO = Decimal("0.01")


class _CampiRiga:
    """Campi comuni alle righe d'offerta e d'ordine. L'importo è il netto: quantità × prezzo − sconto, senza IVA."""

    posizione: Mapped[int] = mapped_column(Integer, default=0)
    articolo_id: Mapped[int | None] = mapped_column(ForeignKey("articoli.id", ondelete="SET NULL"))
    prodotto_id: Mapped[int | None] = mapped_column(ForeignKey("prodotti.id", ondelete="SET NULL"))
    modulo: Mapped[str] = mapped_column(String(120), default="")
    descrizione: Mapped[str] = mapped_column(Text, default="")
    quantita: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    prezzo_unitario: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    sconto_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    aliquota_iva: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    stato_verifica: Mapped[str] = mapped_column(String(16), default=StatoVerifica.CONFERMATO)
    # La riga è stata letta da un documento (estrazione con Claude): resta "proposto" finché non si conferma.
    estrazione_id: Mapped[int | None] = mapped_column(ForeignKey("estrazioni.id", ondelete="SET NULL"))

    @property
    def proposta(self) -> bool:
        return self.stato_verifica == StatoVerifica.PROPOSTO

    @property
    def importo(self) -> Decimal | None:
        if self.quantita is None or self.prezzo_unitario is None:
            return None
        lordo = Decimal(self.quantita) * Decimal(self.prezzo_unitario)
        sconto = Decimal(self.sconto_pct or 0)
        return (lordo * (Decimal(100) - sconto) / Decimal(100)).quantize(CENTESIMO)

    @property
    def prezzo_netto(self) -> Decimal | None:
        if self.prezzo_unitario is None:
            return None
        return (Decimal(self.prezzo_unitario) * (Decimal(100) - Decimal(self.sconto_pct or 0)) / Decimal(100)).quantize(CENTESIMO)


def totale_righe(righe) -> Decimal:  # noqa: ANN001
    """Somma degli importi delle righe (quelle senza quantità o prezzo non contano)."""
    return sum((r.importo for r in righe if r.importo is not None), Decimal("0.00"))


class Offerta(Base):
    __tablename__ = "offerte"

    id: Mapped[int] = mapped_column(primary_key=True)
    pratica_id: Mapped[int] = mapped_column(ForeignKey("pratiche.id", ondelete="CASCADE"), index=True)
    # Il cliente a cui si offre: obbligatorio (lo impone il servizio), di norma quello della commessa.
    cliente_id: Mapped[int | None] = mapped_column(ForeignKey("clienti.id"), index=True)
    numero: Mapped[str] = mapped_column(String(80), default="")
    titolo: Mapped[str] = mapped_column(String(300), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    # L'evento (offerta inviata o accettata, da mail o da documento) che l'ha creata, se non è stata scritta a mano.
    evento_id: Mapped[int | None] = mapped_column(ForeignKey("eventi.id", ondelete="SET NULL"))
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)

    pratica: Mapped[Pratica] = relationship(back_populates="offerte")
    cliente: Mapped[Cliente | None] = relationship()
    revisioni: Mapped[list[RevisioneOfferta]] = relationship(
        back_populates="offerta", cascade="all, delete-orphan", order_by="RevisioneOfferta.numero_revisione"
    )

    @property
    def revisione_accettata(self) -> RevisioneOfferta | None:
        return next((r for r in self.revisioni if r.stato == StatoRevisione.ACCETTATA), None)

    @property
    def ultima_revisione(self) -> RevisioneOfferta | None:
        return self.revisioni[-1] if self.revisioni else None

    @property
    def nome(self) -> str:
        return self.numero or self.titolo or f"Offerta {self.id}"


class RevisioneOfferta(Base):
    __tablename__ = "revisioni_offerta"
    __table_args__ = (UniqueConstraint("offerta_id", "numero_revisione"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    offerta_id: Mapped[int] = mapped_column(ForeignKey("offerte.id", ondelete="CASCADE"), index=True)
    numero_revisione: Mapped[int] = mapped_column(Integer, default=1)
    data: Mapped[date | None] = mapped_column(Date)
    stato: Mapped[str] = mapped_column(String(16), default=StatoRevisione.BOZZA)
    condizioni: Mapped[str] = mapped_column(Text, default="")
    messaggio_id: Mapped[int | None] = mapped_column(ForeignKey("messaggi.id", ondelete="SET NULL"))
    documento_id: Mapped[int | None] = mapped_column(ForeignKey("documenti.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)

    offerta: Mapped[Offerta] = relationship(back_populates="revisioni")
    righe: Mapped[list[RigaOfferta]] = relationship(
        back_populates="revisione", cascade="all, delete-orphan", order_by="[RigaOfferta.posizione, RigaOfferta.id]"
    )

    @property
    def totale(self) -> Decimal:
        return totale_righe(self.righe)

    @property
    def etichetta_stato(self) -> str:
        return ETICHETTE_REVISIONE.get(self.stato, self.stato)

    @property
    def nome(self) -> str:
        return f"{self.offerta.nome} rev. {self.numero_revisione}"


class RigaOfferta(_CampiRiga, Base):
    __tablename__ = "righe_offerta"

    id: Mapped[int] = mapped_column(primary_key=True)
    revisione_id: Mapped[int] = mapped_column(ForeignKey("revisioni_offerta.id", ondelete="CASCADE"), index=True)

    revisione: Mapped[RevisioneOfferta] = relationship(back_populates="righe")
    prodotto: Mapped[Prodotto | None] = relationship()
    articolo: Mapped[Articolo | None] = relationship()
    allocazioni: Mapped[list[AllocazioneRiga]] = relationship(back_populates="riga_offerta", cascade="all, delete-orphan")


ordini_messaggi = Table(
    "ordini_messaggi",
    Base.metadata,
    Column("ordine_id", ForeignKey("ordini.id", ondelete="CASCADE"), primary_key=True),
    Column("messaggio_id", ForeignKey("messaggi.id", ondelete="CASCADE"), primary_key=True),
)


class Ordine(Base):
    __tablename__ = "ordini"

    id: Mapped[int] = mapped_column(primary_key=True)
    pratica_id: Mapped[int] = mapped_column(ForeignKey("pratiche.id", ondelete="CASCADE"), index=True)
    numero: Mapped[str] = mapped_column(String(80), default="")  # il numero d'ordine del cliente
    data_ordine: Mapped[date | None] = mapped_column(Date)
    revisione_offerta_id: Mapped[int | None] = mapped_column(ForeignKey("revisioni_offerta.id", ondelete="SET NULL"))
    riferimento_offerta: Mapped[str] = mapped_column(String(80), default="")  # il numero d'offerta citato nell'ordine
    condizioni: Mapped[str] = mapped_column(Text, default="")
    note: Mapped[str] = mapped_column(Text, default="")
    stato_verifica: Mapped[str] = mapped_column(String(16), default=StatoVerifica.CONFERMATO, index=True)
    conformita: Mapped[str] = mapped_column(String(20), default=Conformita.DA_VERIFICARE, index=True)
    parziale: Mapped[bool] = mapped_column(Boolean, default=False)
    esito_verifica: Mapped[dict] = mapped_column(JSONType, default=dict)
    verificato_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    deroga_motivo: Mapped[str] = mapped_column(Text, default="")
    deroga_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    deroga_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    avanzamento: Mapped[str] = mapped_column(String(20), default=Avanzamento.APERTO, index=True)
    attesa: Mapped[str] = mapped_column(String(10), default=Attesa.NESSUNA, index=True)
    attesa_motivo: Mapped[str] = mapped_column(String(500), default="")
    attesa_dal: Mapped[date | None] = mapped_column(Date)
    chiave: Mapped[str | None] = mapped_column(String(300), unique=True)  # idempotenza per gli ordini nati dagli eventi
    messaggio_id: Mapped[int | None] = mapped_column(ForeignKey("messaggi.id", ondelete="SET NULL"))
    documento_id: Mapped[int | None] = mapped_column(ForeignKey("documenti.id", ondelete="SET NULL"))
    evento_id: Mapped[int | None] = mapped_column(ForeignKey("eventi.id", ondelete="SET NULL"))
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow, onupdate=utcnow)

    pratica: Mapped[Pratica] = relationship(back_populates="ordini")
    revisione_offerta: Mapped[RevisioneOfferta | None] = relationship()
    righe: Mapped[list[RigaOrdine]] = relationship(
        back_populates="ordine", cascade="all, delete-orphan", order_by="[RigaOrdine.posizione, RigaOrdine.id]"
    )
    messaggi: Mapped[list[Messaggio]] = relationship(secondary=ordini_messaggi, order_by="Messaggio.data")
    deroga_utente: Mapped[User | None] = relationship(foreign_keys=[deroga_user_id])

    @property
    def totale(self) -> Decimal:
        return totale_righe(self.righe)

    @property
    def integrazione(self) -> bool:
        """Un ordine che si aggiunge all'ordine principale della commessa."""
        principale = self.pratica.ordine_principale if self.pratica is not None else None
        return principale is not None and principale is not self and self.avanzamento != "annullato"

    @property
    def nome(self) -> str:
        return f"Ordine {self.numero}" if self.numero else f"Ordine del {self.data_ordine:%d/%m/%Y}" if self.data_ordine else f"Ordine {self.id}"

    @property
    def derogato(self) -> bool:
        return self.conformita == Conformita.SCOSTAMENTI and self.deroga_at is not None

    @property
    def etichetta_conformita(self) -> str:
        if self.derogato:
            return "Conforme con deroga"
        etichetta = ETICHETTE_CONFORMITA.get(self.conformita, self.conformita)
        return f"{etichetta}, parziale" if self.parziale and self.conformita == Conformita.CONFORME else etichetta

    @property
    def etichetta_avanzamento(self) -> str:
        return ETICHETTE_AVANZAMENTO.get(self.avanzamento, self.avanzamento)

    @property
    def etichetta_attesa(self) -> str:
        return ETICHETTE_ATTESA.get(self.attesa, self.attesa)

    @property
    def conta(self) -> bool:
        """Entra nei totali della commessa: confermato e non annullato."""
        return self.stato_verifica == StatoVerifica.CONFERMATO and self.avanzamento != Avanzamento.ANNULLATO


class RigaOrdine(_CampiRiga, Base):
    __tablename__ = "righe_ordine"

    id: Mapped[int] = mapped_column(primary_key=True)
    ordine_id: Mapped[int] = mapped_column(ForeignKey("ordini.id", ondelete="CASCADE"), index=True)

    ordine: Mapped[Ordine] = relationship(back_populates="righe")
    prodotto: Mapped[Prodotto | None] = relationship()
    articolo: Mapped[Articolo | None] = relationship()
    allocazioni: Mapped[list[AllocazioneRiga]] = relationship(back_populates="riga_ordine", cascade="all, delete-orphan")


class AllocazioneRiga(Base):
    """Quanto di una riga d'offerta copre una riga d'ordine. Il residuo della riga d'offerta è ciò che non è allocato."""

    __tablename__ = "allocazioni_righe"
    __table_args__ = (UniqueConstraint("riga_offerta_id", "riga_ordine_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    riga_offerta_id: Mapped[int] = mapped_column(ForeignKey("righe_offerta.id", ondelete="CASCADE"), index=True)
    riga_ordine_id: Mapped[int] = mapped_column(ForeignKey("righe_ordine.id", ondelete="CASCADE"), index=True)
    quantita: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    importo: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    origine: Mapped[str] = mapped_column(String(12), default="automatica")  # automatica | manuale

    riga_offerta: Mapped[RigaOfferta] = relationship(back_populates="allocazioni")
    riga_ordine: Mapped[RigaOrdine] = relationship(back_populates="allocazioni")


class FonteEstrazione(enum.StrEnum):
    MAIL = "mail"
    DRIVE = "drive"
    PROTOCOLLO = "protocollo"  # il programma di protocollo, via API: fonte prevista, connettore da fare


class StatoEstrazione(enum.StrEnum):
    IN_ATTESA = "in_attesa"
    FATTA = "fatta"
    SALTATA = "saltata"  # file già letto, documento che non è un ordine/offerta, righe già presenti
    ERRORE = "errore"


ETICHETTE_ESTRAZIONE = {
    StatoEstrazione.IN_ATTESA: "In attesa",
    StatoEstrazione.FATTA: "Righe estratte",
    StatoEstrazione.SALTATA: "Non riletta",
    StatoEstrazione.ERRORE: "Errore",
}


class Estrazione(Base):
    """La lettura delle righe di un ordine o di una revisione d'offerta da un documento (allegato di una mail, file
    su Drive, in futuro protocollo), fatta da Claude. È insieme la coda del lavoro e il registro di cosa è stato
    letto: gli hash dei file evitano di rileggere lo stesso documento arrivato da più fonti."""

    __tablename__ = "estrazioni"

    id: Mapped[int] = mapped_column(primary_key=True)
    pratica_id: Mapped[int] = mapped_column(ForeignKey("pratiche.id", ondelete="CASCADE"), index=True)
    fonte: Mapped[str] = mapped_column(String(16))
    messaggio_id: Mapped[int | None] = mapped_column(ForeignKey("messaggi.id", ondelete="SET NULL"), index=True)
    documento_id: Mapped[int | None] = mapped_column(ForeignKey("documenti.id", ondelete="SET NULL"), index=True)
    riferimento_esterno: Mapped[str] = mapped_column(String(300), default="")
    ordine_id: Mapped[int | None] = mapped_column(ForeignKey("ordini.id", ondelete="CASCADE"), index=True)
    revisione_offerta_id: Mapped[int | None] = mapped_column(ForeignKey("revisioni_offerta.id", ondelete="CASCADE"), index=True)
    stato: Mapped[str] = mapped_column(String(16), default=StatoEstrazione.IN_ATTESA, index=True)
    tentativi: Mapped[int] = mapped_column(Integer, default=0)
    errore: Mapped[str] = mapped_column(Text, default="")
    file: Mapped[list] = mapped_column(JSONType, default=list)  # [{nome, mime, sha256, byte}]
    risultato: Mapped[dict] = mapped_column(JSONType, default=dict)  # testata, righe, avvisi
    modello: Mapped[str] = mapped_column(String(80), default="")
    token: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    elaborata_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    pratica: Mapped[Pratica] = relationship()
    messaggio: Mapped[Messaggio | None] = relationship()
    documento: Mapped[Documento | None] = relationship()
    ordine: Mapped[Ordine | None] = relationship()
    revisione_offerta: Mapped[RevisioneOfferta | None] = relationship()

    @property
    def destinazione(self) -> Ordine | RevisioneOfferta | None:
        return self.ordine or self.revisione_offerta

    @property
    def etichetta_stato(self) -> str:
        return ETICHETTE_ESTRAZIONE.get(self.stato, self.stato)

    @property
    def avvisi(self) -> list[str]:
        return list((self.risultato or {}).get("avvisi") or [])

    @property
    def nome_file(self) -> str:
        nomi = [f.get("nome", "") for f in self.file or [] if f.get("usato", True)]
        return ", ".join(n for n in nomi if n) or (self.documento.nome if self.documento else "")


# ---------------------------------------------------------------------------------------------
# Solleciti e regole
# ---------------------------------------------------------------------------------------------


class Regola(Base):
    __tablename__ = "regole"

    id: Mapped[int] = mapped_column(primary_key=True)
    codice: Mapped[str] = mapped_column(String(64), unique=True)
    nome: Mapped[str] = mapped_column(String(255))
    descrizione: Mapped[str] = mapped_column(Text, default="")
    soglia_giorni: Mapped[int | None] = mapped_column(Integer)
    gravita: Mapped[str] = mapped_column(String(16), default=Gravita.AVVISO)
    attiva: Mapped[bool] = mapped_column(Boolean, default=True)
    parametri: Mapped[dict] = mapped_column(JSONType, default=dict)


class Sollecito(Base):
    __tablename__ = "solleciti"

    id: Mapped[int] = mapped_column(primary_key=True)
    pratica_id: Mapped[int] = mapped_column(ForeignKey("pratiche.id", ondelete="CASCADE"), index=True)
    regola: Mapped[str] = mapped_column(String(64), index=True)
    chiave: Mapped[str] = mapped_column(String(100), unique=True)  # "<pratica_id>:<regola>"
    gravita: Mapped[str] = mapped_column(String(16), default=Gravita.AVVISO)
    titolo: Mapped[str] = mapped_column(String(300))
    messaggio: Mapped[str] = mapped_column(Text, default="")
    dati: Mapped[dict] = mapped_column(JSONType, default=dict)
    stato: Mapped[str] = mapped_column(String(16), default=StatoSollecito.APERTO, index=True)
    assegnato_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    snooze_fino: Mapped[date | None] = mapped_column(Date)
    aperto_il: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    aggiornato_il: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow, onupdate=utcnow)
    risolto_il: Mapped[datetime | None] = mapped_column(TZDateTime)
    risolto_da: Mapped[str | None] = mapped_column(String(16))  # sistema | utente
    notificato_il: Mapped[datetime | None] = mapped_column(TZDateTime)
    occorrenze: Mapped[int] = mapped_column(Integer, default=1)

    pratica: Mapped[Pratica] = relationship(back_populates="solleciti")
    assegnato: Mapped[User | None] = relationship(foreign_keys=[assegnato_user_id])


# ---------------------------------------------------------------------------------------------
# Supporto: sequenze codici, esecuzioni, impostazioni
# ---------------------------------------------------------------------------------------------


class SequenzaCodici(Base):
    __tablename__ = "sequenze_codici"

    anno: Mapped[int] = mapped_column(Integer, primary_key=True)
    ultimo: Mapped[int] = mapped_column(Integer, default=0)


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    tipo: Mapped[str] = mapped_column(String(16))  # gmail | drive | classify | regole | digest
    stato: Mapped[str] = mapped_column(String(16), default="in_corso")  # in_corso | ok | errore
    started_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    contatori: Mapped[dict] = mapped_column(JSONType, default=dict)
    errori: Mapped[list] = mapped_column(JSONType, default=list)
    note: Mapped[str] = mapped_column(Text, default="")


class Impostazione(Base):
    """Impostazioni modificabili dall'interfaccia (chiave → valore JSON)."""

    __tablename__ = "impostazioni"

    chiave: Mapped[str] = mapped_column(String(100), primary_key=True)
    valore: Mapped[dict] = mapped_column(JSONType, default=dict)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow, onupdate=utcnow)
