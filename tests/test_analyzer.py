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
