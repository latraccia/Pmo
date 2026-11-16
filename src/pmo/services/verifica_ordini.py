"""Verifica dell'ordine contro l'offerta: regole fisse, senza IA.

L'ordine si confronta con la revisione d'offerta indicata sull'ordine o, se manca, con l'ultima revisione accettata
della commessa. Ogni riga d'ordine si abbina a una riga d'offerta: prima l'abbinamento scelto a mano, poi lo stesso
articolo del listino, poi stesso prodotto e modulo, poi stessa descrizione. Si controllano:
1. cliente e riferimenti: il cliente dell'offerta e quello della commessa, il numero d'offerta citato nell'ordine;
2. perimetro: righe non previste, quantità oltre il residuo (contando gli altri ordini);
3. importi: il totale della riga contro il prezzo netto offerto, con una tolleranza (il maggiore tra euro e percentuale,
   parametri della regola "ordine_con_scostamenti");
4. condizioni: se offerta e ordine le indicano entrambe e sono diverse.
Esito: conforme, scostamenti, non determinabile (manca l'offerta, mancano righe, prezzi o quantità). Un ordine che non
copre tutta l'offerta è "parziale": il resto rimane residuo.

Una deroga accetta un ordine con scostamenti, con motivo, autore e data. Se una verifica successiva dà un esito
diverso (le righe sono cambiate), la deroga decade e la cronologia lo registra.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from pmo.db.base import utcnow
from pmo.db.models import (
    AllocazioneRiga,
    Conformita,
    Ordine,
    Regola,
    RevisioneOfferta,
    RigaOfferta,
    RigaOrdine,
    TipoEvento,
    TipoFonte,
    User,
)
from pmo.services.ordini import (
    allocato,
    normalizza,
    normalizza_numero,
    residuo_revisione,
    revisioni_accettate,
)

REGOLA = "ordine_con_scostamenti"
TOLLERANZA_PREDEFINITA = (Decimal("1.0"), Decimal("1.0"))  # euro, percento


@dataclass
class Scostamento:
    tipo: str  # riferimenti | perimetro | quantita | importo | condizioni | dati
    messaggio: str
    riga_ordine_id: int | None = None
    riga_offerta_id: int | None = None


def tolleranza(session: Session) -> tuple[Decimal, Decimal]:
    regola = session.query(Regola).filter(Regola.codice == REGOLA).one_or_none()
    parametri = (regola.parametri if regola else None) or {}
    return (
        Decimal(str(parametri.get("tolleranza_euro", TOLLERANZA_PREDEFINITA[0]))),
        Decimal(str(parametri.get("tolleranza_percento", TOLLERANZA_PREDEFINITA[1]))),
    )


def revisione_di_riferimento(ordine: Ordine) -> RevisioneOfferta | None:
    if ordine.revisione_offerta is not None:
        return ordine.revisione_offerta
    accettate = revisioni_accettate(ordine.pratica)
    return max(accettate, key=lambda r: (r.data or r.created_at.date(), r.id)) if accettate else None


def _euro(valore: Decimal) -> str:
    testo = f"{valore:,.2f}".replace(",", "§").replace(".", ",").replace("§", ".")
    return f"{testo} €"


def _nome_riga(riga: RigaOrdine | RigaOfferta) -> str:
    return " · ".join(x for x in (riga.modulo, (riga.descrizione or "")[:60]) if x) or f"riga {riga.posizione}"


def _abbina(riga: RigaOrdine, candidate: list[RigaOfferta]) -> RigaOfferta | None:
    if riga.articolo_id:  # stesso articolo del listino: l'abbinamento più sicuro
        for c in candidate:
            if c.articolo_id == riga.articolo_id:
                return c
    modulo = normalizza(riga.modulo)
    if riga.prodotto_id or modulo:
        for c in candidate:
            if (riga.prodotto_id or None) == (c.prodotto_id or None) and modulo == normalizza(c.modulo):
                return c
    descrizione = normalizza(riga.descrizione)
    if descrizione:
        for c in candidate:
            if descrizione == normalizza(c.descrizione):
                return c
    return None


def _togli_allocazione(allocazione: AllocazioneRiga) -> None:
    """Toglie l'abbinamento da entrambe le righe, così i residui calcolati prima del salvataggio sono già giusti."""
    if allocazione.riga_ordine is not None and allocazione in allocazione.riga_ordine.allocazioni:
        allocazione.riga_ordine.allocazioni.remove(allocazione)
    if allocazione.riga_offerta is not None and allocazione in allocazione.riga_offerta.allocazioni:
        allocazione.riga_offerta.allocazioni.remove(allocazione)


def _firma(conformita: str, scostamenti: list[Scostamento]) -> str:
    dati = json.dumps([conformita, sorted(s.messaggio for s in scostamenti)], ensure_ascii=False)
    return hashlib.sha256(dati.encode()).hexdigest()[:16]


def verifica_ordine(session: Session, ordine: Ordine, *, user_id: int | None = None) -> dict:
    """Confronta l'ordine con l'offerta, aggiorna abbinamenti, conformità e residuo, e restituisce l'esito."""
    toll_eur, toll_pct = tolleranza(session)
    revisione = revisione_di_riferimento(ordine)
    scostamenti: list[Scostamento] = []
    non_determinabile: list[str] = []

    # Gli abbinamenti automatici si rifanno a ogni verifica; quelli scelti a mano restano.
    for riga in ordine.righe:
        for allocazione in list(riga.allocazioni):
            if allocazione.origine != "manuale" or revisione is None or allocazione.riga_offerta.revisione_id != revisione.id:
                _togli_allocazione(allocazione)
    session.flush()

    if not ordine.righe:
        non_determinabile.append("L'ordine non ha righe.")
    if revisione is None:
        non_determinabile.append("Nessuna offerta di riferimento: indicare la revisione o accettarne una nella commessa.")
    else:
        if revisione.offerta.pratica_id != ordine.pratica_id:
            scostamenti.append(Scostamento("riferimenti", "L'offerta di riferimento appartiene a un'altra commessa."))
        cliente_offerta, cliente_ordine = revisione.offerta.cliente, ordine.pratica.cliente
        if cliente_offerta is not None and cliente_ordine is not None and cliente_offerta.id != cliente_ordine.id:
            scostamenti.append(
                Scostamento("cliente", f"L'offerta è per {cliente_offerta.nome}, l'ordine è della commessa di {cliente_ordine.nome}.")
            )
        numero_offerta = revisione.offerta.numero
        if ordine.riferimento_offerta and numero_offerta and normalizza_numero(ordine.riferimento_offerta) != normalizza_numero(numero_offerta):
            scostamenti.append(
                Scostamento("riferimenti", f"L'ordine cita l'offerta {ordine.riferimento_offerta}, il riferimento è {numero_offerta}.")
            )
        if ordine.condizioni.strip() and revisione.condizioni.strip() and normalizza(ordine.condizioni) != normalizza(revisione.condizioni):
            scostamenti.append(Scostamento("condizioni", "Le condizioni dell'ordine sono diverse da quelle dell'offerta: da verificare."))
        if not revisione.righe:
            non_determinabile.append("L'offerta di riferimento non ha righe.")

        gia_qui: dict[int, Decimal] = {}  # quantità già abbinata da questo ordine, per riga d'offerta
        for riga in ordine.righe:
            if riga.quantita is None or riga.prezzo_unitario is None:
                non_determinabile.append(f"Alla riga «{_nome_riga(riga)}» mancano quantità o prezzo.")
                continue
            manuale = next((a for a in riga.allocazioni if a.origine == "manuale"), None)
            offerta_riga = manuale.riga_offerta if manuale else _abbina(riga, list(revisione.righe))
            if offerta_riga is None:
                scostamenti.append(
                    Scostamento("perimetro", f"La riga «{_nome_riga(riga)}» non è prevista dall'offerta.", riga_ordine_id=riga.id)
                )
                continue
            if manuale is None:
                manuale = AllocazioneRiga(riga_offerta=offerta_riga, origine="automatica")
                riga.allocazioni.append(manuale)
            manuale.quantita = riga.quantita
            manuale.importo = riga.importo
            if offerta_riga.quantita is None or offerta_riga.prezzo_unitario is None:
                non_determinabile.append(f"Alla riga d'offerta «{_nome_riga(offerta_riga)}» mancano quantità o prezzo.")
                continue
            # perimetro: quantità oltre quanto resta dell'offerta, contando gli altri ordini e le righe precedenti
            gia_altri, _ = allocato(offerta_riga, escludi_ordine_id=ordine.id)
            disponibile = Decimal(offerta_riga.quantita) - gia_altri - gia_qui.get(offerta_riga.id, Decimal(0))
            gia_qui[offerta_riga.id] = gia_qui.get(offerta_riga.id, Decimal(0)) + Decimal(riga.quantita)
            if Decimal(riga.quantita) > disponibile:
                scostamenti.append(
                    Scostamento(
                        "quantita",
                        f"«{_nome_riga(riga)}»: ordinate {riga.quantita:g}, ne restano {max(disponibile, Decimal(0)):g} dell'offerta.",
                        riga.id,
                        offerta_riga.id,
                    )
                )
            # importi: totale della riga contro il prezzo netto offerto per la stessa quantità
            atteso = (offerta_riga.prezzo_netto * Decimal(riga.quantita)).quantize(Decimal("0.01"))
            margine = max(toll_eur, abs(atteso) * toll_pct / Decimal(100))
            if abs(riga.importo - atteso) > margine:
                scostamenti.append(
                    Scostamento(
                        "importo",
                        f"«{_nome_riga(riga)}»: {_euro(riga.importo)} contro {_euro(atteso)} dell'offerta.",
                        riga.id,
                        offerta_riga.id,
                    )
                )
    session.flush()

    if non_determinabile:
        conformita = Conformita.NON_DETERMINABILE
    elif scostamenti:
        conformita = Conformita.SCOSTAMENTI
    else:
        conformita = Conformita.CONFORME
    # Un ordine annullato o non confermato non consuma l'offerta: residuo_revisione conta solo gli ordini che contano.
    residuo = residuo_revisione(revisione) if revisione is not None else Decimal(0)
    firma = _firma(conformita, scostamenti)
    precedente = (ordine.esito_verifica or {}).get("firma")
    if ordine.deroga_at is not None and (conformita != Conformita.SCOSTAMENTI or (precedente and precedente != firma)):
        _deroga_decaduta(session, ordine)

    ordine.conformita = conformita.value
    ordine.parziale = revisione is not None and conformita != Conformita.NON_DETERMINABILE and residuo > toll_eur
    ordine.verificato_at = utcnow()
    ordine.esito_verifica = {
        "firma": firma,
        "revisione_id": revisione.id if revisione else None,
        "revisione": revisione.nome if revisione else None,
        "scostamenti": [asdict(s) for s in scostamenti],
        "non_determinabile": non_determinabile,
        "totale_offerta": str(revisione.totale) if revisione else None,
        "totale_ordine": str(ordine.totale),
        "residuo": str(residuo),
        "verificato_da": user_id,
    }
    session.flush()
    return ordine.esito_verifica


def _deroga_decaduta(session: Session, ordine: Ordine) -> None:
    from pmo.services.pratiche import aggiungi_evento

    aggiungi_evento(
        session,
        ordine.pratica,
        tipo=TipoEvento.NOTA,
        titolo=f"Deroga decaduta: {ordine.nome}",
        descrizione=f"L'esito della verifica è cambiato dopo la deroga («{ordine.deroga_motivo}»): l'ordine va rivisto.",
        fonte_tipo=TipoFonte.SISTEMA,
        aggiorna_stato=False,
    )
    ordine.deroga_motivo = ""
    ordine.deroga_user_id = None
    ordine.deroga_at = None


def registra_deroga(session: Session, ordine: Ordine, motivo: str, utente: User) -> None:
    """Accetta l'ordine con i suoi scostamenti: serve un motivo, restano autore e data."""
    motivo = (motivo or "").strip()
    if ordine.conformita != Conformita.SCOSTAMENTI:
        raise ValueError("Si può derogare solo un ordine con scostamenti verificati.")
    if not motivo:
        raise ValueError("Il motivo della deroga è obbligatorio.")
    from pmo.services.pratiche import aggiungi_evento

    ordine.deroga_motivo = motivo
    ordine.deroga_user_id = utente.id
    ordine.deroga_at = utcnow()
    aggiungi_evento(
        session,
        ordine.pratica,
        tipo=TipoEvento.NOTA,
        titolo=f"Deroga: {ordine.nome}",
        descrizione=f"Ordine accettato con scostamenti da {utente.nome or utente.email}: {motivo}",
        fonte_tipo=TipoFonte.MANUALE,
        created_by_user_id=utente.id,
        aggiorna_stato=False,
    )
    session.flush()


def confronto(ordine: Ordine) -> dict:
    """Le righe affiancate per la pagina: ogni riga d'offerta con le righe di questo ordine che la coprono e il residuo,
    poi le righe d'ordine senza corrispondenza. Gli scostamenti sono indicizzati per riga."""
    revisione = revisione_di_riferimento(ordine)
    esito = ordine.esito_verifica or {}
    per_riga: dict[int, list[str]] = {}
    for s in esito.get("scostamenti", []):
        if s.get("riga_ordine_id"):
            per_riga.setdefault(s["riga_ordine_id"], []).append(s["messaggio"])
    abbinate: set[int] = set()
    righe = []
    if revisione is not None:
        for riga_offerta in revisione.righe:
            proprie = [a.riga_ordine for a in riga_offerta.allocazioni if a.riga_ordine.ordine_id == ordine.id]
            abbinate.update(r.id for r in proprie)
            gia_altri = allocato(riga_offerta, escludi_ordine_id=ordine.id)[1]
            residuo = None
            if riga_offerta.importo is not None:
                residuo = riga_offerta.importo - gia_altri - (sum((r.importo or 0 for r in proprie), Decimal(0)) if ordine.conta else 0)
            righe.append({"offerta": riga_offerta, "ordine": proprie, "altri_ordini": gia_altri, "residuo": residuo})
    senza = [r for r in ordine.righe if r.id not in abbinate]
    return {"revisione": revisione, "righe": righe, "senza_offerta": senza, "per_riga": per_riga}
