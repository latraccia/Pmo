"""Lettura con Claude delle righe di un ordine o di un'offerta da un documento (PDF o Word).

I PDF vanno a Claude così come sono (blocchi `document`): legge anche tabelle e scansioni. Un documento Word va
come testo, tabelle comprese. Nel system prompt, in cache, ci sono le istruzioni e il listino; per un ordine anche
le righe dell'offerta di riferimento, che aiutano a riconoscere le voci. La risposta è uno schema Pydantic.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Literal, Protocol

import anthropic
from pydantic import BaseModel, Field

from pmo.config import Settings, get_settings
from pmo.ingest.classifier import ErroreClassificazione, UsoToken, chiama_claude

SYSTEM_PROMPT = """Leggi documenti commerciali di una software house italiana (La Traccia) che vende software sanitario:
ordini dei clienti (ordini d'acquisto, buoni d'ordine, determine, contratti esecutivi, ordini MEPA) e offerte
inviate ai clienti. Il tuo compito è trascrivere fedelmente la testata e le righe del documento richiesto.

Regole:
- Tra i file allegati, usa quello che è davvero l'ordine o l'offerta richiesti (indicalo in `documento_usato`).
  Capitolati, condizioni generali, firme, lettere di accompagnamento non sono righe.
- Se il documento non è un ordine né un'offerta, rispondi con tipo "altro" e nessuna riga.
- Una riga per ogni voce con quantità o importo. Non inventare righe; non unire voci diverse; ignora righe di
  solo testo, subtotali, totali, IVA, spese di bollo.
- Numeri in formato JSON (punto decimale): 12.000,50 nel documento diventa 12000.5. Prezzo unitario e importo
  della riga senza IVA. Se c'è solo l'importo, metti quantità 1 e prezzo uguale all'importo.
- Lo sconto è in percentuale; se il documento indica uno sconto in euro, calcola il prezzo unitario netto e
  lascia lo sconto vuoto.
- Codice: il codice articolo del documento (o del listino di La Traccia, se lo riconosci con certezza dalla
  descrizione); altrimenti vuoto. Descrizione: quella del documento, ripulita da a capo.
- Numero e data del documento; per un ordine anche il numero dell'offerta citata, se c'è. Se l'offerta indica
  una revisione ("rev. 2", "Revisione 3"), riportane il numero.
- Condizioni: pagamento, consegna, durata, penali, in una o due frasi.
- Quando un dato non c'è, lascialo vuoto: meglio vuoto che sbagliato. Segnala in `avvisi` i dubbi di lettura
  (cifre poco leggibili, righe tagliate, importi incoerenti)."""


class RigaLetta(BaseModel):
    codice: str = ""
    descrizione: str
    quantita: float | None = None
    unita: str = ""
    prezzo_unitario: float | None = None
    sconto_pct: float | None = None
    aliquota_iva: float | None = None
    importo: float | None = Field(default=None, description="Importo della riga senza IVA, come scritto nel documento")


class DocumentoLetto(BaseModel):
    documento_usato: str = Field(default="", description="Nome del file che contiene l'ordine o l'offerta")
    tipo: Literal["ordine", "offerta", "altro"]
    numero: str = ""
    data: str = Field(default="", description="Data del documento, AAAA-MM-GG")
    numero_offerta_citata: str = ""
    revisione: int | None = None
    condizioni: str = ""
    righe: list[RigaLetta] = Field(default_factory=list)
    totale_imponibile: float | None = None
    note: str = ""
    avvisi: list[str] = Field(default_factory=list)
    confidenza: float = Field(default=0.5, ge=0, le=1)


@dataclass
class FileDaLeggere:
    nome: str
    mime: str
    dati: bytes = b""
    testo: str = ""  # per i documenti Word: il testo con le tabelle, al posto dei byte


class Estrattore(Protocol):
    modello: str
    uso: UsoToken

    def estrai(self, destinazione: Literal["ordine", "offerta"], file: list[FileDaLeggere], contesto: str) -> DocumentoLetto: ...


def contenuto(destinazione: str, file: list[FileDaLeggere]) -> list[dict]:
    """Il messaggio per Claude: i PDF come documenti, i Word come testo, poi la richiesta."""
    blocchi: list[dict] = []
    for f in file:
        if f.dati and (f.mime == "application/pdf" or f.nome.lower().endswith(".pdf")):
            blocchi.append({
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": base64.standard_b64encode(f.dati).decode()},
                "title": f.nome[:200],
            })
        else:
            blocchi.append({"type": "text", "text": f"=== File: {f.nome} ===\n{f.testo}"})
    richiesta = "Trascrivi testata e righe dell'ordine del cliente." if destinazione == "ordine" else "Trascrivi testata e righe della nostra offerta."
    blocchi.append({"type": "text", "text": richiesta})
    return blocchi


class EstrattoreClaude:
    def __init__(self, settings: Settings | None = None, client: anthropic.Anthropic | None = None):
        self.settings = settings or get_settings()
        if client is not None:
            self.client = client
        elif self.settings.anthropic_api_key:
            self.client = anthropic.Anthropic(api_key=self.settings.anthropic_api_key)
        else:
            self.client = anthropic.Anthropic()
        self.modello = self.settings.claude_model_estrazione or self.settings.claude_model
        self.uso = UsoToken()

    def estrai(self, destinazione: Literal["ordine", "offerta"], file: list[FileDaLeggere], contesto: str) -> DocumentoLetto:
        if not file:
            raise ErroreClassificazione("Nessun file da leggere", temporaneo=False)
        system = [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]
        if contesto:
            system.append({"type": "text", "text": contesto, "cache_control": {"type": "ephemeral"}})
        risultato = chiama_claude(
            self.client, self.settings, system=system, content=contenuto(destinazione, file), schema=DocumentoLetto, uso=self.uso,
            model=self.modello, effort=self.settings.estrazione_effort, max_tokens=self.settings.estrazione_max_tokens,
        )
        return risultato  # type: ignore[return-value]


class EstrattoreNullo:
    modello = ""

    def __init__(self) -> None:
        self.uso = UsoToken()

    def estrai(self, destinazione: Literal["ordine", "offerta"], file: list[FileDaLeggere], contesto: str) -> DocumentoLetto:
        raise ErroreClassificazione("Lettura delle righe disattivata (PMO_AI_ENABLED o PMO_ESTRAZIONE_RIGHE)", temporaneo=False)


def crea_estrattore(settings: Settings | None = None) -> Estrattore:
    settings = settings or get_settings()
    if not settings.ai_enabled or not settings.estrazione_righe:
        return EstrattoreNullo()
    return EstrattoreClaude(settings)
