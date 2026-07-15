"""Tests du bot d'écoute (étape 1.5) — logique métier, sans Telegram réel.

On teste les modules purs (`callbacks`, `odds`, `positions`) : décodage du
callback, autorisation, cote médiane au clic (nominal + repli), et surtout
l'**idempotence croisée** (premier clic gagnant, quelles que soient les actions).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from common import db
from common.db import get_connection, init_db
from listener.callbacks import is_authorized, parse_callback
from listener.odds import current_median_odds
from listener.positions import PASS, TAKE, record_click


# ─────────────────────────── parse_callback ───────────────────────────

def test_parse_callback_decodes_take_and_pass():
    assert parse_callback("pos:42") == (TAKE, 42)
    assert parse_callback("skip:7") == (PASS, 7)


@pytest.mark.parametrize("data", [None, "", "pos", "pos:", "pos:abc", "other:1", "42"])
def test_parse_callback_rejects_invalid(data):
    assert parse_callback(data) is None


# ─────────────────────────── autorisation ───────────────────────────

def test_is_authorized_matches_configured_chat():
    assert is_authorized(6771567165, "6771567165") is True   # int vs str
    assert is_authorized("6771567165", "6771567165") is True


def test_is_authorized_rejects_others_and_empty_config():
    assert is_authorized(999, "6771567165") is False
    assert is_authorized(123, "") is False  # config vide → tout est rejeté


# ─────────────────────── fixtures base ───────────────────────

@pytest.fixture
def conn(tmp_path: Path):
    """Base temporaire avec un match, un verdict SIGNAL, et deux relevés spread."""
    db_path = tmp_path / "test.db"
    init_db(db_path)
    connection = get_connection(db_path)
    connection.execute(
        "INSERT INTO matches VALUES "
        "('m1','basketball_nba','Boston Celtics','Miami Heat','2026-07-17T00:20:00Z',"
        "'DECIDE','2026-07-16T09:00:00Z')"
    )
    # Verdict spread sur Boston, cote figée 1.91 au moment du verdict.
    db.insert_verdict(
        connection, match_id="m1", verdict="SIGNAL", selection="Boston Celtics",
        market="spreads", line=-5.0, odds_at_verdict=1.91, signal_score=6,
        rules_triggered=json.dumps(["R1"]), rationale="…", decided_at="2026-07-16T23:20:00Z",
    )
    connection.commit()
    yield connection
    connection.close()


def _add_spread_snapshot(conn, *, book, odds, at, selection="Boston Celtics", line=-5.0):
    db.insert_snapshot(
        conn, match_id="m1", bookmaker=book, market="spreads",
        selection=selection, line=line, odds=odds, snapshot_at=at,
    )


# ─────────────────────── current_median_odds ───────────────────────

def test_current_median_odds_uses_latest_snapshot_median(conn):
    """La cote au clic = médiane des books du dernier relevé (pas odds_at_verdict)."""
    # Dernier relevé : trois books à 1.80 / 1.85 / 2.00 → médiane 1.85.
    _add_spread_snapshot(conn, book="pinnacle", odds=1.80, at="2026-07-16T23:50:00Z")
    _add_spread_snapshot(conn, book="fanduel", odds=1.85, at="2026-07-16T23:50:00Z")
    _add_spread_snapshot(conn, book="draftkings", odds=2.00, at="2026-07-16T23:50:00Z")
    conn.commit()

    verdict = db.get_verdict(conn, 1)
    assert current_median_odds(conn, verdict) == 1.85


def test_current_median_odds_falls_back_when_market_not_quoted(conn):
    """Si le marché/sélection du verdict n'est pas coté au dernier relevé : repli."""
    # Un relevé existe, mais sur une AUTRE sélection (Miami) : Boston non coté.
    _add_spread_snapshot(conn, book="pinnacle", odds=1.95, at="2026-07-16T23:50:00Z",
                         selection="Miami Heat", line=5.0)
    conn.commit()

    verdict = db.get_verdict(conn, 1)
    assert current_median_odds(conn, verdict) == 1.91  # odds_at_verdict


def test_current_median_odds_falls_back_without_snapshots(conn):
    """Aucun relevé en base → repli sur odds_at_verdict."""
    verdict = db.get_verdict(conn, 1)
    assert current_median_odds(conn, verdict) == 1.91


# ─────────────────────── record_click (idempotence) ───────────────────────

def _positions(conn):
    return conn.execute("SELECT * FROM positions ORDER BY id").fetchall()


def test_record_click_stores_take_with_odds(conn):
    out = record_click(conn, verdict_id=1, action=TAKE, odds_at_click=1.85,
                       clicked_at="2026-07-16T23:55:00Z")
    assert out.recorded is True and out.action == TAKE
    rows = _positions(conn)
    assert len(rows) == 1
    assert rows[0]["action"] == "take"
    assert rows[0]["odds_at_click"] == 1.85


def test_record_click_stores_pass_with_odds(conn):
    """« Passer » enregistre aussi une ligne, avec sa cote (donnée évaluable)."""
    out = record_click(conn, verdict_id=1, action=PASS, odds_at_click=1.85,
                       clicked_at="2026-07-16T23:55:00Z")
    assert out.recorded is True and out.action == PASS
    rows = _positions(conn)
    assert len(rows) == 1
    assert rows[0]["action"] == "pass"
    assert rows[0]["odds_at_click"] == 1.85


def test_idempotent_take_then_pass_keeps_first(conn):
    """Croisement pos→skip : la première décision (take) fait foi, une seule ligne."""
    record_click(conn, verdict_id=1, action=TAKE, odds_at_click=1.85, clicked_at="t1")
    out = record_click(conn, verdict_id=1, action=PASS, odds_at_click=1.70, clicked_at="t2")

    assert out.recorded is False
    assert out.action == TAKE            # action retenue = celle du 1er clic
    rows = _positions(conn)
    assert len(rows) == 1
    assert rows[0]["action"] == "take"
    assert rows[0]["odds_at_click"] == 1.85  # cote du 1er clic conservée


def test_idempotent_pass_then_take_keeps_first(conn):
    """Croisement skip→pos : la première décision (pass) fait foi, une seule ligne."""
    record_click(conn, verdict_id=1, action=PASS, odds_at_click=1.85, clicked_at="t1")
    out = record_click(conn, verdict_id=1, action=TAKE, odds_at_click=1.70, clicked_at="t2")

    assert out.recorded is False
    assert out.action == PASS
    rows = _positions(conn)
    assert len(rows) == 1
    assert rows[0]["action"] == "pass"


def test_record_click_rejects_invalid_action(conn):
    with pytest.raises(ValueError):
        record_click(conn, verdict_id=1, action="wager", odds_at_click=1.85, clicked_at="t")
