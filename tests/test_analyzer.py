"""Tests d'intégration de l'analyseur : alertes + verdict écrits en base."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from analyzer.analyzer import analyze_open_matches
from common import db
from common.config import load_config
from common.db import DECISION_LOGIC_VERSION, get_connection, init_db
from tests import fixtures as fx

CFG = load_config()
NOW = datetime(2026, 1, 10, 20, 0, tzinfo=timezone.utc)


def _setup(tmp_path, tipoff_iso):
    """Base avec un match SUIVI et deux relevés (mouvement fort sur 4 books)."""
    db_path = tmp_path / "test.db"
    init_db(db_path)
    conn = get_connection(db_path)
    db.insert_match(
        conn, match_id="m1", sport="basketball_wnba", home_team="Home",
        away_team="Away", tipoff_utc=tipoff_iso, status="SUIVI", created_at=fx.T[0],
    )
    rows = []
    for book in ("a", "b", "c", "d"):
        rows += fx.spreads(book, "Home", "Away", -2.0, 1.91, 1.91, fx.T[0])
        rows += fx.spreads(book, "Home", "Away", -5.0, 1.91, 1.91, fx.T[1])
        rows += fx.h2h(book, "Home", "Away", 1.90, 1.90, fx.T[0])
        rows += fx.h2h(book, "Home", "Away", 1.70, 2.15, fx.T[1])
    for row in rows:
        db.insert_snapshot(conn, match_id="m1", **row)
    conn.commit()
    return conn


def test_verdict_written_in_decision_window(tmp_path):
    """Tip-off dans la fenêtre → verdict SIGNAL écrit, match passé en DECIDE."""
    tipoff = (NOW + timedelta(hours=1)).isoformat()
    conn = _setup(tmp_path, tipoff)

    summary = analyze_open_matches(conn, CFG, NOW)

    assert summary["verdicts"] == 1
    assert conn.execute("SELECT status FROM matches WHERE match_id='m1'").fetchone()["status"] == "DECIDE"
    verdict = conn.execute("SELECT verdict, selection FROM verdicts WHERE match_id='m1'").fetchone()
    assert verdict["verdict"] == "SIGNAL"
    assert verdict["selection"] == "Home"
    alerts = {a["rule"] for a in conn.execute("SELECT rule FROM alerts WHERE match_id='m1'")}
    assert {"R1", "R4"} <= alerts
    conn.close()


def test_no_verdict_outside_decision_window(tmp_path):
    """Tip-off trop lointain → pas de verdict (reste SUIVI), mais alertes émises."""
    tipoff = (NOW + timedelta(hours=5)).isoformat()
    conn = _setup(tmp_path, tipoff)

    summary = analyze_open_matches(conn, CFG, NOW)

    assert summary["verdicts"] == 0
    assert conn.execute("SELECT status FROM matches WHERE match_id='m1'").fetchone()["status"] == "SUIVI"
    assert summary["alerts"] >= 1
    conn.close()


def test_verdict_at_exact_window_boundary(tmp_path):
    """Tip-off exactement a now + window_hours (2.0h) -> dans la fenetre (borne inclusive <=)."""
    tipoff = (NOW + timedelta(hours=2, seconds=0)).isoformat()
    conn = _setup(tmp_path, tipoff)

    summary = analyze_open_matches(conn, CFG, NOW)

    assert summary["verdicts"] == 1
    assert conn.execute("SELECT status FROM matches WHERE match_id='m1'").fetchone()["status"] == "DECIDE"
    conn.close()


def test_no_verdict_just_beyond_window(tmp_path):
    """Tip-off a now + window_hours + 1s -> hors fenetre (strictement au-dela de la borne).
    
    Lot 2 : window_hours passé de 2.0 à 2.5, donc le test utilise 2.5 + 1s.
    """
    tipoff = (NOW + timedelta(hours=2.5, seconds=1)).isoformat()
    conn = _setup(tmp_path, tipoff)

    summary = analyze_open_matches(conn, CFG, NOW)

    assert summary["verdicts"] == 0
    assert conn.execute("SELECT status FROM matches WHERE match_id='m1'").fetchone()["status"] == "SUIVI"
    conn.close()


# ─────────────────── Déduplication des alertes par état ───────────────────

def test_alert_dedup_same_state_not_reemitted(tmp_path):
    """Deux collectes avec le même état (sélection + direction) → une seule alerte.

    La déduplication compare le `state_key` : si la règle persiste sans changement
    de direction, la seconde collecte ne réémet pas (fini le spam Telegram).
    """
    tipoff = (NOW + timedelta(hours=5)).isoformat()
    conn = _setup(tmp_path, tipoff)

    # 1re analyse : R1 + R4 déclenchées (mouvement Home -2 → -5 sur 4 books).
    analyze_open_matches(conn, CFG, NOW)
    alerts_1 = conn.execute("SELECT COUNT(*) AS n FROM alerts WHERE match_id='m1'").fetchone()["n"]
    assert alerts_1 >= 1

    # 2e analyse : mêmes données (aucun nouveau snapshot) → même état → pas de nouvelle alerte.
    analyze_open_matches(conn, CFG, NOW)
    alerts_2 = conn.execute("SELECT COUNT(*) AS n FROM alerts WHERE match_id='m1'").fetchone()["n"]
    assert alerts_2 == alerts_1  # aucune alerte supplémentaire
    conn.close()


def test_alert_reemitted_when_state_changes(tmp_path):
    """Changement de direction → nouvelle alerte émise (l'état a changé)."""
    tipoff = (NOW + timedelta(hours=5)).isoformat()
    db_path = tmp_path / "test.db"
    init_db(db_path)
    conn = get_connection(db_path)
    db.insert_match(
        conn, match_id="m1", sport="basketball_wnba", home_team="Home",
        away_team="Away", tipoff_utc=tipoff, status="SUIVI", created_at=fx.T[0],
    )
    # T0 : Home -2.0 ; T1 : Home -5.0 (baisse → R1 déclenchée, state = spreads/Home|-1)
    for book in ("a", "b", "c", "d"):
        db.insert_snapshot(conn, match_id="m1", **fx.snap(book, "spreads", "Home", 1.91, -2.0, fx.T[0]))
        db.insert_snapshot(conn, match_id="m1", **fx.snap(book, "spreads", "Away", 1.91, 2.0, fx.T[0]))
        db.insert_snapshot(conn, match_id="m1", **fx.snap(book, "spreads", "Home", 1.91, -5.0, fx.T[1]))
        db.insert_snapshot(conn, match_id="m1", **fx.snap(book, "spreads", "Away", 1.91, 5.0, fx.T[1]))
    conn.commit()

    analyze_open_matches(conn, CFG, NOW)
    alerts_1 = conn.execute("SELECT COUNT(*) AS n FROM alerts WHERE match_id='m1' AND rule='R1'").fetchone()["n"]
    assert alerts_1 == 1

    # Ajout d'un snapshot T2 : Home passe à +1.0 (Away à -1.0). Le mouvement Away
    # depuis l ouverture est |(-1.0) - (+2.0)| = 3.0 >= seuil -> R1 toujours déclenchée,
    # mais la direction s inverse : Away +2.0 -> -1.0 = baisse (state passe de +1 a -1).
    for book in ("a", "b", "c", "d"):
        db.insert_snapshot(conn, match_id="m1", **fx.snap(book, "spreads", "Home", 1.91, 1.0, fx.T[2]))
        db.insert_snapshot(conn, match_id="m1", **fx.snap(book, "spreads", "Away", 1.91, -1.0, fx.T[2]))
    conn.commit()

    analyze_open_matches(conn, CFG, NOW)
    alerts_2 = conn.execute("SELECT COUNT(*) AS n FROM alerts WHERE match_id='m1' AND rule='R1'").fetchone()["n"]
    assert alerts_2 == 2  # nouvelle alerte car l'état a changé (hausse → baisse)
    conn.close()


# ─────────────────── Déduplication R4 : évolution d'ampleur ───────────────────

def test_alert_r4_evolution_8_to_9_books(tmp_path):
    """R4 : 8 books → 9 books (même direction) → nouvelle alerte avec évolution.

    Le state_key change (amplitude 8 → 9) donc la déduplication laisse passer.
    Le détail de la 2e alerte contient "8 → 9 bookmakers".
    """
    tipoff = (NOW + timedelta(hours=5)).isoformat()
    db_path = tmp_path / "test.db"
    init_db(db_path)
    conn = get_connection(db_path)
    db.insert_match(
        conn, match_id="m1", sport="basketball_wnba", home_team="Home",
        away_team="Away", tipoff_utc=tipoff, status="SUIVI", created_at=fx.T[0],
    )
    # T0 : 8 books baissent (Home -2.0 → -5.0)
    for book in ("a", "b", "c", "d", "e", "f", "g", "h"):
        db.insert_snapshot(conn, match_id="m1", **fx.snap(book, "spreads", "Home", 1.91, -2.0, fx.T[0]))
        db.insert_snapshot(conn, match_id="m1", **fx.snap(book, "spreads", "Away", 1.91, 2.0, fx.T[0]))
        db.insert_snapshot(conn, match_id="m1", **fx.snap(book, "spreads", "Home", 1.91, -5.0, fx.T[1]))
        db.insert_snapshot(conn, match_id="m1", **fx.snap(book, "spreads", "Away", 1.91, 5.0, fx.T[1]))
    conn.commit()

    analyze_open_matches(conn, CFG, NOW)
    alerts_1 = conn.execute("SELECT COUNT(*) AS n FROM alerts WHERE match_id='m1' AND rule='R4'").fetchone()["n"]
    assert alerts_1 == 1

    # Ajout d'un 9e book (i) qui baisse aussi → R4 toujours déclenchée, amplitude 8 → 9
    db.insert_snapshot(conn, match_id="m1", **fx.snap("i", "spreads", "Home", 1.91, -2.0, fx.T[0]))
    db.insert_snapshot(conn, match_id="m1", **fx.snap("i", "spreads", "Away", 1.91, 2.0, fx.T[0]))
    db.insert_snapshot(conn, match_id="m1", **fx.snap("i", "spreads", "Home", 1.91, -5.0, fx.T[1]))
    db.insert_snapshot(conn, match_id="m1", **fx.snap("i", "spreads", "Away", 1.91, 5.0, fx.T[1]))
    conn.commit()

    analyze_open_matches(conn, CFG, NOW)
    alerts_2 = conn.execute("SELECT COUNT(*) AS n FROM alerts WHERE match_id='m1' AND rule='R4'").fetchone()["n"]
    assert alerts_2 == 2  # nouvelle alerte car amplitude a changé (8 → 9)

    # Le détail de la 2e alerte contient l'évolution "8 → 9"
    last_alert = conn.execute(
        "SELECT details FROM alerts WHERE match_id='m1' AND rule='R4' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert "8 → 9" in last_alert["details"]
    conn.close()


def test_alert_r4_same_8_books_silent(tmp_path):
    """R4 : 2 collectes à 8 books (même direction, même amplitude) → 1 seule alerte."""
    tipoff = (NOW + timedelta(hours=5)).isoformat()
    db_path = tmp_path / "test.db"
    init_db(db_path)
    conn = get_connection(db_path)
    db.insert_match(
        conn, match_id="m1", sport="basketball_wnba", home_team="Home",
        away_team="Away", tipoff_utc=tipoff, status="SUIVI", created_at=fx.T[0],
    )
    for book in ("a", "b", "c", "d", "e", "f", "g", "h"):
        db.insert_snapshot(conn, match_id="m1", **fx.snap(book, "spreads", "Home", 1.91, -2.0, fx.T[0]))
        db.insert_snapshot(conn, match_id="m1", **fx.snap(book, "spreads", "Away", 1.91, 2.0, fx.T[0]))
        db.insert_snapshot(conn, match_id="m1", **fx.snap(book, "spreads", "Home", 1.91, -5.0, fx.T[1]))
        db.insert_snapshot(conn, match_id="m1", **fx.snap(book, "spreads", "Away", 1.91, 5.0, fx.T[1]))
    conn.commit()

    analyze_open_matches(conn, CFG, NOW)
    alerts_1 = conn.execute("SELECT COUNT(*) AS n FROM alerts WHERE match_id='m1' AND rule='R4'").fetchone()["n"]
    assert alerts_1 == 1

    # 2e analyse : mêmes données → même state_key → pas de nouvelle alerte
    analyze_open_matches(conn, CFG, NOW)
    alerts_2 = conn.execute("SELECT COUNT(*) AS n FROM alerts WHERE match_id='m1' AND rule='R4'").fetchone()["n"]
    assert alerts_2 == 1  # silencieux
    conn.close()


# ─────────────────── Garde tip-off : aucune analyse post-tip-off ───────────────────

# ─────────────────── Re-décision des matchs DECIDE via analyze_open_matches ───────────────────

def test_decide_match_in_window_is_redecided_and_superseded(tmp_path):
    """Un match DECIDE en fenêtre est re-décidé via analyze_open_matches (C1).

    Correctif C1 (revue externe) : `analyze_open_matches` ne sélectionnait que
    DECOUVERT/SUIVI — les matchs DECIDE n'étaient jamais réanalysés, la branche
    `elif status == "DECIDE"` de `analyze_match` était du code mort en production.
    Les tests passaient car `test_redecision.py` appelle `_redecide` directement.

    Ce test d'intégration prouve le chemin complet : un match DECIDE en fenêtre,
    passé par `analyze_open_matches`, est re-décidé. Un changement matériel
    (sélection Home → Away) déclenche la supersession (ancien message mémorisé,
    verdict remis en file). Vérifie aussi que `logic_version` est estampillé.
    """
    tipoff = (NOW + timedelta(hours=1)).isoformat()
    conn = _setup(tmp_path, tipoff)

    # 1re analyse : SIGNAL Home, match → DECIDE.
    analyze_open_matches(conn, CFG, NOW)
    verdict = conn.execute(
        "SELECT id, verdict, selection, logic_version FROM verdicts WHERE match_id='m1'"
    ).fetchone()
    assert verdict["verdict"] == "SIGNAL"
    assert verdict["selection"] == "Home"
    assert verdict["logic_version"] == DECISION_LOGIC_VERSION
    assert conn.execute("SELECT status FROM matches WHERE match_id='m1'").fetchone()["status"] == "DECIDE"

    # Simule l'envoi Telegram du 1er verdict (message 100).
    db.set_verdict_notified(conn, verdict["id"], 100, NOW.isoformat())
    conn.commit()

    # Ajout d'un snapshot T2 : Home passe à +1.0 (Away à -1.0). Le mouvement Away
    # depuis l'ouverture est |(-1.0) - (+2.0)| = 3.0 >= seuil → R1 déclenchée,
    # mais la direction s'inverse : la sélection devient Away (changement matériel).
    for book in ("a", "b", "c", "d"):
        db.insert_snapshot(conn, match_id="m1", **fx.snap(book, "spreads", "Home", 1.91, 1.0, fx.T[2]))
        db.insert_snapshot(conn, match_id="m1", **fx.snap(book, "spreads", "Away", 1.91, -1.0, fx.T[2]))
    conn.commit()

    # 2e analyse : le match est DECIDE mais actif → re-décision via analyze_open_matches.
    summary = analyze_open_matches(conn, CFG, NOW)
    assert summary["analyzed"] == 1  # le match DECIDE est bien sélectionné

    redecided = conn.execute(
        "SELECT verdict, selection, logic_version, superseded_message_id, "
        "telegram_message_id, notified_at FROM verdicts WHERE match_id='m1'"
    ).fetchone()
    assert redecided["selection"] == "Away"                      # changement matériel
    assert redecided["logic_version"] == DECISION_LOGIC_VERSION  # estampillé
    assert redecided["superseded_message_id"] == 100             # ancien message mémorisé
    assert redecided["telegram_message_id"] is None              # message courant effacé
    assert redecided["notified_at"] is None                      # remis en file d'envoi
    conn.close()


# ─────────────────── Garde decision_min_hours (correctif 2026-07-20, CLV None) ───────────────────

def _setup_stable(tmp_path, tipoff_iso):
    """Base avec un match SUIVI et des relevés STABLES (aucun mouvement -> NO_BET)."""
    db_path = tmp_path / "test.db"
    init_db(db_path)
    conn = get_connection(db_path)
    db.insert_match(
        conn, match_id="m1", sport="basketball_wnba", home_team="Home",
        away_team="Away", tipoff_utc=tipoff_iso, status="SUIVI", created_at=fx.T[0],
    )
    rows = []
    for book in ("a", "b", "c", "d"):
        rows += fx.spreads(book, "Home", "Away", -3.0, 1.91, 1.91, fx.T[0])
        rows += fx.spreads(book, "Home", "Away", -3.0, 1.91, 1.91, fx.T[1])  # stable
        rows += fx.h2h(book, "Home", "Away", 1.90, 1.90, fx.T[0])
        rows += fx.h2h(book, "Home", "Away", 1.90, 1.90, fx.T[1])  # stable
    for row in rows:
        db.insert_snapshot(conn, match_id="m1", **row)
    conn.commit()
    return conn


def test_no_analysis_within_decision_min_hours(tmp_path):
    """Tip-off à H-0,4 (< decision_min_hours=0.55) → analyse ENTIÈRE sautée.

    Même un mouvement fort (R1+R4, cf. `_setup`) ne produit plus rien : zéro alerte,
    zéro verdict. Le snapshot de clôture reste stocké par le collecteur (hors de ce
    test) ; seule l'ANALYSE est sautée.
    """
    tipoff = (NOW + timedelta(hours=0.4)).isoformat()
    conn = _setup(tmp_path, tipoff)

    summary = analyze_open_matches(conn, CFG, NOW)

    assert summary["alerts"] == 0
    assert summary["verdicts"] == 0
    assert conn.execute("SELECT status FROM matches WHERE match_id='m1'").fetchone()["status"] == "SUIVI"
    conn.close()


def test_verdict_produced_just_above_decision_min_hours(tmp_path):
    """Tip-off à H-0,6 (> decision_min_hours=0.55, dans la fenêtre 2.5h) → verdict rendu."""
    tipoff = (NOW + timedelta(hours=0.6)).isoformat()
    conn = _setup(tmp_path, tipoff)

    summary = analyze_open_matches(conn, CFG, NOW)

    assert summary["verdicts"] == 1
    assert summary["alerts"] >= 1
    conn.close()


def test_decided_at_frozen_across_nobet_reconfirmations(tmp_path):
    """NO_BET rendu à H-2, reconfirmé à H-1,5 puis H-0,6 (aucun mouvement) : decided_at inchangé.

    Sans la distinction matériel/non matériel (correctif 2026-07-20), decided_at
    aurait avancé à chaque passage — faussant systématiquement le CLV, mesuré contre
    un instant qui n'est pas celui de la vraie décision.
    """
    tipoff_dt = NOW + timedelta(hours=2)
    conn = _setup_stable(tmp_path, tipoff_dt.isoformat())

    analyze_open_matches(conn, CFG, NOW)  # H-2 : entrée en fenêtre, verdict NO_BET créé
    v1 = conn.execute("SELECT decided_at, verdict FROM verdicts WHERE match_id='m1'").fetchone()
    assert v1["verdict"] == "NO_BET"
    decided_at_1 = v1["decided_at"]

    analyze_open_matches(conn, CFG, tipoff_dt - timedelta(hours=1.5))  # re-décision non matérielle
    v2 = conn.execute("SELECT decided_at, verdict FROM verdicts WHERE match_id='m1'").fetchone()
    assert v2["verdict"] == "NO_BET"
    assert v2["decided_at"] == decided_at_1

    analyze_open_matches(conn, CFG, tipoff_dt - timedelta(hours=0.6))  # juste au-dessus du seuil
    v3 = conn.execute("SELECT decided_at, verdict FROM verdicts WHERE match_id='m1'").fetchone()
    assert v3["verdict"] == "NO_BET"
    assert v3["decided_at"] == decided_at_1  # toujours inchangé, 3 confirmations plus tard
    conn.close()


def test_decided_at_updated_on_material_redecision(tmp_path):
    """NO_BET à H-2 puis SIGNAL matériel à H-1 (mouvement fort) : decided_at avance.

    Contraste avec le test précédent : un changement matériel DOIT ré-ancrer
    decided_at/odds_at_verdict sur le nouveau prix (nouvelle décision, nouvelle
    référence de CLV).
    """
    tipoff_dt = NOW + timedelta(hours=2)
    conn = _setup_stable(tmp_path, tipoff_dt.isoformat())

    analyze_open_matches(conn, CFG, NOW)
    v1 = conn.execute("SELECT decided_at, verdict FROM verdicts WHERE match_id='m1'").fetchone()
    assert v1["verdict"] == "NO_BET"
    decided_at_1 = v1["decided_at"]

    # Mouvement fort ajouté avant la 2e analyse (R1 + R4 -> score 6 -> SIGNAL).
    for book in ("a", "b", "c", "d"):
        db.insert_snapshot(conn, match_id="m1", **fx.snap(book, "spreads", "Home", 1.70, -7.0, fx.T[2]))
        db.insert_snapshot(conn, match_id="m1", **fx.snap(book, "spreads", "Away", 2.15, 7.0, fx.T[2]))
    conn.commit()

    analyze_open_matches(conn, CFG, tipoff_dt - timedelta(hours=1))
    v2 = conn.execute("SELECT decided_at, verdict FROM verdicts WHERE match_id='m1'").fetchone()
    assert v2["verdict"] == "SIGNAL"          # changement matériel
    assert v2["decided_at"] != decided_at_1   # ré-ancré sur la nouvelle décision
    conn.close()


def test_too_close_to_decide_applies_per_match_not_globally(tmp_path):
    """Deux matchs dans la MÊME exécution : l'un à H-0,3 (sauté), l'autre à H-2 (verdict rendu).

    Preuve que `_too_close_to_decide` s'évalue sur `match["tipoff_utc"]` (par match),
    pas sur une borne globale de l'exécution : `analyze_open_matches` passe le même
    `now` aux deux matchs, mais seul celui dont le tip-off est proche est ignoré —
    l'autre est analysé normalement dans la même passe.
    """
    db_path = tmp_path / "test.db"
    init_db(db_path)
    conn = get_connection(db_path)

    tipoff_close = (NOW + timedelta(hours=0.3)).isoformat()  # sous decision_min_hours=0.55
    tipoff_far = (NOW + timedelta(hours=2)).isoformat()      # dans la fenêtre, au-dessus du seuil

    for match_id, tipoff in (("close", tipoff_close), ("far", tipoff_far)):
        db.insert_match(
            conn, match_id=match_id, sport="basketball_wnba", home_team="Home",
            away_team="Away", tipoff_utc=tipoff, status="SUIVI", created_at=fx.T[0],
        )
        # Même mouvement fort (R1 + R4 -> score 6) sur les deux matchs.
        for book in ("a", "b", "c", "d"):
            for row in fx.spreads(book, "Home", "Away", -2.0, 1.91, 1.91, fx.T[0]):
                db.insert_snapshot(conn, match_id=match_id, **row)
            for row in fx.spreads(book, "Home", "Away", -5.0, 1.91, 1.91, fx.T[1]):
                db.insert_snapshot(conn, match_id=match_id, **row)
    conn.commit()

    summary = analyze_open_matches(conn, CFG, NOW)  # UNE SEULE exécution, même `now` pour les deux

    # "close" (H-0.3) : analyse entièrement sautée -> zéro alerte, zéro verdict, reste SUIVI.
    assert conn.execute("SELECT COUNT(*) n FROM alerts WHERE match_id='close'").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) n FROM verdicts WHERE match_id='close'").fetchone()["n"] == 0
    assert conn.execute("SELECT status FROM matches WHERE match_id='close'").fetchone()["status"] == "SUIVI"

    # "far" (H-2) : analysé normalement dans la MÊME passe -> alertes + verdict SIGNAL, DECIDE.
    assert conn.execute("SELECT COUNT(*) n FROM alerts WHERE match_id='far'").fetchone()["n"] >= 1
    far_verdict = conn.execute("SELECT verdict FROM verdicts WHERE match_id='far'").fetchone()
    assert far_verdict is not None and far_verdict["verdict"] == "SIGNAL"
    assert conn.execute("SELECT status FROM matches WHERE match_id='far'").fetchone()["status"] == "DECIDE"

    # Le résumé global reflète le mélange dans une seule exécution.
    assert summary["analyzed"] == 2
    assert summary["verdicts"] == 1
    conn.close()


def test_no_alerts_after_tipoff(tmp_path):
    """Un match dont le tip-off est passé → zéro alerte, zéro verdict.

    Garde délibérée (bug 17/07) : sans elle, un match resté SUIVI au tip-off
    aurait alerté en live. Couvre aussi le chemin de re-décision DECIDE.
    """
    tipoff = (NOW - timedelta(hours=1)).isoformat()  # tip-off il y a 1h
    db_path = tmp_path / "test.db"
    init_db(db_path)
    conn = get_connection(db_path)
    db.insert_match(
        conn, match_id="m1", sport="basketball_wnba", home_team="Home",
        away_team="Away", tipoff_utc=tipoff, status="SUIVI", created_at=fx.T[0],
    )
    # Snapshots qui déclencheraient R4 (8 books baissent)
    for book in ("a", "b", "c", "d", "e", "f", "g", "h"):
        db.insert_snapshot(conn, match_id="m1", **fx.snap(book, "spreads", "Home", 1.91, -2.0, fx.T[0]))
        db.insert_snapshot(conn, match_id="m1", **fx.snap(book, "spreads", "Away", 1.91, 2.0, fx.T[0]))
        db.insert_snapshot(conn, match_id="m1", **fx.snap(book, "spreads", "Home", 1.91, -5.0, fx.T[1]))
        db.insert_snapshot(conn, match_id="m1", **fx.snap(book, "spreads", "Away", 1.91, 5.0, fx.T[1]))
    conn.commit()

    analyze_open_matches(conn, CFG, NOW)
    alerts = conn.execute("SELECT COUNT(*) AS n FROM alerts WHERE match_id='m1'").fetchone()["n"]
    verdicts = conn.execute("SELECT COUNT(*) AS n FROM verdicts WHERE match_id='m1'").fetchone()["n"]
    assert alerts == 0   # zéro alerte : tip-off passé
    assert verdicts == 0  # zéro verdict
    conn.close()
