"""Caricamento delle anagrafiche iniziali da config/seed.yaml (idempotente)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from pmo.db.models import Articolo, Cliente, ContattoCliente, Prodotto, Team, User
from pmo.permessi import ruolo_valido
from pmo.rules.definitions import assicura_regole


def percorso_seed_default() -> Path:
    """config/seed.yaml nel sorgente (installazione editable) oppure nella cartella corrente."""
    for base in (Path(__file__).resolve().parents[3], Path.cwd()):
        candidato = base / "config" / "seed.yaml"
        if candidato.exists():
            return candidato
    raise FileNotFoundError("config/seed.yaml non trovato: indicare il file con --file")


def carica_seed(session: Session, percorso: Path | None = None) -> dict[str, int]:
    percorso = percorso or percorso_seed_default()
    dati = yaml.safe_load(percorso.read_text(encoding="utf-8")) or {}
    contatori = {"utenti": 0, "team": 0, "prodotti": 0, "articoli": 0, "clienti": 0, "regole": 0}

    for spec in dati.get("utenti", []) or []:
        email = spec["email"].strip().lower()
        utente = session.scalar(select(User).where(User.email == email))
        if utente is None:
            utente = User(email=email)
            session.add(utente)
            contatori["utenti"] += 1
        utente.nome = spec.get("nome") or utente.nome or email
        if spec.get("ruolo"):
            ruolo = ruolo_valido(spec["ruolo"])
            if ruolo is None:
                raise ValueError(f"Ruolo '{spec['ruolo']}' non valido per {email}")
            utente.ruolo = ruolo.value
    session.flush()

    for spec in dati.get("team", []) or []:
        nome = spec["nome"].strip()
        team = session.scalar(select(Team).where(Team.nome == nome))
        if team is None:
            team = Team(nome=nome)
            session.add(team)
            contatori["team"] += 1
        team.descrizione = spec.get("descrizione") or team.descrizione or ""
        membri = [m.strip().lower() for m in spec.get("membri") or []]
        if membri:
            persone = session.scalars(select(User).where(User.email.in_(membri))).all()
            mancanti = set(membri) - {u.email for u in persone}
            if mancanti:
                raise ValueError(f"Team '{nome}': utenti non presenti in seed.yaml: {', '.join(sorted(mancanti))}")
            team.membri = list(persone)

    for spec in dati.get("prodotti", []) or []:
        nome = spec["nome"].strip()
        prodotto = session.scalar(select(Prodotto).where(Prodotto.nome == nome))
        if prodotto is None:
            prodotto = Prodotto(nome=nome)
            session.add(prodotto)
            contatori["prodotti"] += 1
        prodotto.alias = list(spec.get("alias") or [])
        prodotto.descrizione = spec.get("descrizione") or ""

    for spec in dati.get("articoli", []) or []:
        codice = str(spec["codice"]).strip()
        articolo = session.scalar(select(Articolo).where(Articolo.codice == codice))
        if articolo is None:
            articolo = Articolo(codice=codice)
            session.add(articolo)
            contatori["articoli"] += 1
        articolo.descrizione = spec.get("descrizione") or ""
        articolo.unita = str(spec.get("unita") or "")
        articolo.prezzo_listino = Decimal(str(spec["prezzo"])) if spec.get("prezzo") is not None else None
        articolo.aliquota_iva = Decimal(str(spec["iva"])) if spec.get("iva") is not None else None
        articolo.attivo = bool(spec.get("attivo", True))
        if spec.get("prodotto"):
            session.flush()
            prodotto = session.scalar(select(Prodotto).where(Prodotto.nome == str(spec["prodotto"]).strip()))
            if prodotto is None:
                raise ValueError(f"Articolo {codice}: prodotto '{spec['prodotto']}' non presente in seed.yaml")
            articolo.prodotto_id = prodotto.id

    for spec in dati.get("clienti", []) or []:
        nome = spec["nome"].strip()
        cliente = session.scalar(select(Cliente).where(Cliente.nome == nome))
        if cliente is None:
            cliente = Cliente(nome=nome)
            session.add(cliente)
            contatori["clienti"] += 1
        cliente.alias = list(spec.get("alias") or [])
        cliente.domini = [d.lower() for d in spec.get("domini") or []]
        cliente.tipo = spec.get("tipo") or "altro"
        for campo in ("ragione_sociale", "partita_iva", "codice_fiscale", "indirizzo", "citta", "telefono", "email", "pec", "codice_oracle", "codice_vte"):
            if spec.get(campo):
                setattr(cliente, campo, str(spec[campo]).strip())
        if spec.get("team"):
            cliente.team = session.scalar(select(Team).where(Team.nome == spec["team"].strip()))
        for contatto in spec.get("contatti") or []:
            email = (contatto.get("email") or "").strip().lower()
            esistente = next((k for k in cliente.contatti if (email and k.email == email) or k.nome == contatto["nome"]), None)
            if esistente is None:
                esistente = ContattoCliente(nome=contatto["nome"])
                cliente.contatti.append(esistente)
            esistente.nome = contatto["nome"]
            esistente.email = email
            esistente.ruolo = contatto.get("ruolo") or ""
            esistente.telefono = str(contatto.get("telefono") or "")

    contatori["regole"] = len(assicura_regole(session))
    session.commit()
    return contatori
