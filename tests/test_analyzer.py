"""Tests d'intégration de l'analyseur : alertes + verdict écrits en base."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from analyzer.analyzer import analyze_open_matches
from common import db
from common.config import load_config
from common.db import get_connection, init_db
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
    """Tip-off a now + window_hours + 1s -> hors fenetre (strictement au-dela de la borne)."""
    tipoff = (NOW + timedelta(hours=2, seconds=1)).isoformat()
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
