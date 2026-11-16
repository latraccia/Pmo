"""Ruoli e visibilità: la matrice dei permessi e il filtro delle pratiche per team."""

from __future__ import annotations

from sqlalchemy import select

from pmo.db.models import Account, Messaggio, Pratica, Team, User
from pmo.permessi import (
    Azione,
    Ruolo,
    casella_visibile,
    elemento_visibile,
    filtra_pratiche,
    ids_pratiche_visibili,
    puo,
    puo_passare_a,
    ruolo_valido,
)
from pmo.services import pratiche as svc


def _utente(db, email: str, ruolo: Ruolo, *teams: Team) -> User:
    u = User(email=email, nome=email.split("@")[0], ruolo=ruolo.value, teams=list(teams))
    db.add(u)
    db.flush()
    return u


def _visibili(db, utente: User) -> set[str]:
    return set(db.scalars(filtra_pratiche(select(Pratica.codice), utente)))


def test_matrice_dei_ruoli():
    def u(ruolo: str) -> User:
        return User(email=f"{ruolo}@latraccia.it", ruolo=ruolo, attivo=True)

    assert all(puo(u("amministratore"), a) for a in Azione)
    assert puo(u("direzione"), Azione.VEDERE_TUTTO) and not puo(u("direzione"), Azione.CONFIGURARE)
    assert puo(u("amministrazione"), Azione.MODIFICARE_IMPORTI) and not puo(u("amministrazione"), Azione.MODIFICARE_PRATICA)
    assert puo(u("responsabile"), Azione.CHIUDERE) and not puo(u("responsabile"), Azione.VEDERE_TUTTO)
    assert puo(u("commerciale"), Azione.CAMBIARE_STATO) and not puo(u("commerciale"), Azione.CHIUDERE)
    assert puo(u("operativo"), Azione.ANNOTARE) and not puo(u("operativo"), Azione.CONFERMARE)
    # chiudere o annullare richiede un permesso in più del semplice cambio di fase
    assert puo_passare_a(u("commerciale"), "sviluppo") and not puo_passare_a(u("commerciale"), "chiusa")
    assert puo_passare_a(u("responsabile"), "annullata")
    assert not puo_passare_a(u("operativo"), "sviluppo")
    # un utente disattivato non può nulla; i vecchi nomi dei ruoli sono ancora riconosciuti
    assert not puo(User(email="x@latraccia.it", ruolo="amministratore", attivo=False), Azione.ANNOTARE)
    assert ruolo_valido("admin") is Ruolo.AMMINISTRATORE and ruolo_valido("membro") is Ruolo.OPERATIVO
    assert ruolo_valido("ceo") is None


def test_visibilita_per_team(db):
    alfa, beta = Team(nome="Alfa"), Team(nome="Beta")
    db.add_all([alfa, beta])
    db.flush()
    op_alfa = _utente(db, "op@latraccia.it", Ruolo.OPERATIVO, alfa)
    doppio = _utente(db, "doppio@latraccia.it", Ruolo.RESPONSABILE, alfa, beta)
    ceo = _utente(db, "ceo@latraccia.it", Ruolo.DIREZIONE)
    solo = _utente(db, "solo@latraccia.it", Ruolo.COMMERCIALE)

    pa = svc.crea_pratica(db, "Pratica Alfa", team_id=alfa.id)
    pb = svc.crea_pratica(db, "Pratica Beta", team_id=beta.id)
    pr = svc.crea_pratica(db, "Di cui sono sostituto", team_id=beta.id, sostituto_user_id=op_alfa.id)
    pc = svc.crea_pratica(db, "Creata da solo", created_by_user_id=solo.id)

    assert _visibili(db, op_alfa) == {pa.codice, pr.codice}
    assert _visibili(db, doppio) == {pa.codice, pb.codice, pr.codice}
    assert _visibili(db, ceo) == {pa.codice, pb.codice, pr.codice, pc.codice}
    assert _visibili(db, solo) == {pc.codice}
    assert ids_pratiche_visibili(db, ceo) is None  # chi vede tutto non ha filtri


def test_pratica_senza_team_visibile_tramite_la_casella(db):
    alfa = Team(nome="Alfa")
    db.add(alfa)
    db.flush()
    proprietario = _utente(db, "prop@latraccia.it", Ruolo.OPERATIVO)
    collega = _utente(db, "collega@latraccia.it", Ruolo.OPERATIVO, alfa)
    estraneo = _utente(db, "estraneo@latraccia.it", Ruolo.OPERATIVO)
    personale = Account(email="prop@latraccia.it", owner_user_id=proprietario.id)
    di_team = Account(email="ufficio@latraccia.it", team_id=alfa.id)
    db.add_all([personale, di_team])
    db.flush()

    # il proprietario non ha team: la pratica nata dalla sua casella resta senza team, ma lui la vede
    p1 = svc.crea_pratica(db, "Dalla casella personale", account_id=personale.id)
    assert p1.team_id is None
    assert _visibili(db, proprietario) == {p1.codice}
    assert _visibili(db, estraneo) == set()

    # dalla casella di team la pratica prende il team della casella
    p2 = svc.crea_pratica(db, "Dalla casella di team", account_id=di_team.id)
    assert p2.team_id == alfa.id
    assert _visibili(db, collega) == {p2.codice}
    assert casella_visibile(di_team, collega) and not casella_visibile(di_team, estraneo)


def test_team_automatico_dal_proprietario_con_un_solo_team(db):
    alfa, beta = Team(nome="Alfa"), Team(nome="Beta")
    db.add_all([alfa, beta])
    db.flush()
    uno = _utente(db, "uno@latraccia.it", Ruolo.OPERATIVO, alfa)
    due = _utente(db, "due@latraccia.it", Ruolo.OPERATIVO, alfa, beta)
    a1 = Account(email="uno@latraccia.it", owner_user_id=uno.id)
    a2 = Account(email="due@latraccia.it", owner_user_id=due.id)
    db.add_all([a1, a2])
    db.flush()
    assert svc.crea_pratica(db, "x", account_id=a1.id).team_id == alfa.id
    assert svc.crea_pratica(db, "y", account_id=a2.id).team_id is None  # due team: da assegnare a mano


def test_mail_visibili_per_casella_o_per_pratica(db):
    from datetime import UTC, datetime

    alfa = Team(nome="Alfa")
    db.add(alfa)
    db.flush()
    op = _utente(db, "op@latraccia.it", Ruolo.OPERATIVO, alfa)
    altra = Account(email="altra@latraccia.it")
    db.add(altra)
    db.flush()
    pratica = svc.crea_pratica(db, "Alfa", team_id=alfa.id)

    def mail(n: int, pratica_id: int | None) -> Messaggio:
        m = Messaggio(account_id=altra.id, gmail_message_id=f"m{n}", gmail_thread_id=f"t{n}", data=datetime.now(UTC), pratica_id=pratica_id)
        db.add(m)
        db.flush()
        return m

    collegata, sciolta = mail(1, pratica.id), mail(2, None)
    assert elemento_visibile(db, collegata, op)
    assert not elemento_visibile(db, sciolta, op)
