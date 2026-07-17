"""Tests de l'évaluateur (étape 1.6) — composant critique, priorité au grading.

Cas limites exigés par la roadmap : ligne qui traverse zéro, push (ligne entière),
match reporté (pas de résultat), bookmaker manquant au dernier relevé, h2h/spreads/
totals, et NO_BET (faux négatif). Aucun appel réseau (client balldontlie mocké).
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from common import db
from common.config import Settings
from common.db import get_connection, init_db
from common.results_api_client import GameResult, ResultsApiClient
from evaluator.clv import compute_clv
from evaluator.evaluator import evaluate_pending
from evaluator.grading import grade_verdict
from evaluator.reconcile import find_result, normalize_team, tipoff_calendar_date
from evaluator.reporting import EvalLine, format_daily_report, success_rate

BOS, MIA = "Boston Celtics", "Miami Heat"


# ─────────────────────────── grading (cœur critique) ───────────────────────────

def test_grade_h2h_win_and_loss():
    assert grade_verdict(market="h2h", selection=BOS, line=None, home_team=BOS,
                         away_team=MIA, home_score=110, away_score=100) == "won"
    assert grade_verdict(market="h2h", selection=MIA, line=None, home_team=BOS,
                         away_team=MIA, home_score=110, away_score=100) == "lost"


def test_grade_spread_favorite_covers_and_fails():
    # Boston -5 : gagne de 10 → couvre (10 - 5 = 5 > 0).
    assert grade_verdict(market="spreads", selection=BOS, line=-5.0, home_team=BOS,
                         away_team=MIA, home_score=110, away_score=100) == "won"
    # Boston -5 : gagne de 3 → ne couvre pas (3 - 5 = -2 < 0).
    assert grade_verdict(market="spreads", selection=BOS, line=-5.0, home_team=BOS,
                         away_team=MIA, home_score=103, away_score=100) == "lost"


def test_grade_spread_crossing_zero_underdog():
    # Miami +6 perd de 4 → couvre (-4 + 6 = 2 > 0) : la ligne « traverse zéro ».
    assert grade_verdict(market="spreads", selection=MIA, line=6.0, home_team=BOS,
                         away_team=MIA, home_score=104, away_score=100) == "won"


def test_grade_spread_push_is_explicit():
    # Boston -5 gagne exactement de 5 → push (remboursé), état EXPLICITE (pas None).
    assert grade_verdict(market="spreads", selection=BOS, line=-5.0, home_team=BOS,
                         away_team=MIA, home_score=105, away_score=100) == "push"


def test_grade_totals_over_under_and_push():
    assert grade_verdict(market="totals", selection="Over", line=210.5, home_team=BOS,
                         away_team=MIA, home_score=110, away_score=105) == "won"   # 215 > 210,5
    assert grade_verdict(market="totals", selection="Under", line=210.5, home_team=BOS,
                         away_team=MIA, home_score=100, away_score=105) == "won"   # 205 < 210,5
    assert grade_verdict(market="totals", selection="Over", line=210.0, home_team=BOS,
                         away_team=MIA, home_score=105, away_score=105) == "push"  # 210 = 210 push


def test_grade_unknown_selection_is_none_not_push():
    # Sélection hors du match → non notable (None), à NE PAS confondre avec un push.
    assert grade_verdict(market="h2h", selection="Lakers", line=None, home_team=BOS,
                         away_team=MIA, home_score=110, away_score=100) is None


# ─────────────────────────── réconciliation ───────────────────────────

def _game(date_, home, away, hs, as_, status="Final"):
    return GameResult(game_date=date_, status=status, home_team=home,
                      away_team=away, home_score=hs, away_score=as_)


def test_normalize_team_is_case_and_space_insensitive():
    assert normalize_team("  Boston   Celtics ") == normalize_team("boston celtics")


def test_tipoff_calendar_date_uses_us_timezone():
    # 00:20 UTC le 17 = encore le 16 au soir à New York.
    assert tipoff_calendar_date("2026-01-17T00:20:00Z", "America/New_York").isoformat() == "2026-01-16"


def test_find_result_matches_names_and_nearby_date():
    games = [_game("2026-01-16", BOS, MIA, 110, 100)]
    r = find_result(games, home_team=BOS, away_team=MIA,
                    tipoff_utc="2026-01-17T00:20:00Z", calendar_tz="America/New_York")
    assert r is not None and r.home_score == 110


def test_find_result_returns_none_when_absent():
    games = [_game("2026-01-16", "Lakers", "Suns", 100, 99)]
    assert find_result(games, home_team=BOS, away_team=MIA,
                       tipoff_utc="2026-01-17T00:20:00Z", calendar_tz="America/New_York") is None


# ─────────────────────────── client balldontlie ───────────────────────────

def test_results_client_parses_and_paginates():
    pages = [
        {"data": [{"date": "2026-01-16", "status": "Final",
                   "home_team": {"full_name": BOS}, "visitor_team": {"full_name": MIA},
                   "home_team_score": 110, "visitor_team_score": 100}],
         "meta": {"next_cursor": 90}},
        {"data": [{"date": "2026-01-16T00:00:00.000Z", "status": "Final",
                   "home_team": {"full_name": "Lakers"}, "visitor_team": {"full_name": "Suns"},
                   "home_team_score": 99, "visitor_team_score": 98}],
         "meta": {"next_cursor": None}},
    ]
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        resp = pages[calls["n"]]
        calls["n"] += 1
        return httpx.Response(200, json=resp)

    client = ResultsApiClient("key", "https://api.balldontlie.io", "/v1/games",
                              transport=httpx.MockTransport(handler))
    games = client.get_games("2026-01-16", "2026-01-16")
    assert len(games) == 2                       # deux pages agrégées
    assert games[0].is_final and games[0].home_score == 110
    assert games[1].game_date == "2026-01-16"    # date tronquée à YYYY-MM-DD


# ─────────────────────── CLV (proba dé-marginée) ───────────────────────

@pytest.fixture
def conn(tmp_path: Path):
    db_path = tmp_path / "test.db"
    init_db(db_path)
    connection = get_connection(db_path)
    connection.execute(
        "INSERT INTO matches VALUES "
        "('m1','basketball_nba','Boston Celtics','Miami Heat','2026-01-17T00:20:00Z',"
        "'CLOS','2026-01-16T09:00:00Z')"
    )
    connection.commit()
    yield connection
    connection.close()


def _snap(conn, *, book, sel, line, odds, at, market="spreads"):
    db.insert_snapshot(conn, match_id="m1", bookmaker=book, market=market,
                       selection=sel, line=line, odds=odds, snapshot_at=at)


def test_compute_clv_uses_demargined_consensus(conn):
    """CLV = proba dé-marginée clôture − proba dé-marginée verdict (deux books)."""
    from analyzer.preprocessing import preprocess
    # Verdict (t0) : Boston/Miami cotés 1.91/1.91 → proba dé-marginée 0.5.
    for sel, ln, od in [("Boston Celtics", -5.0, 1.91), ("Miami Heat", 5.0, 1.91)]:
        _snap(conn, book="pinnacle", sel=sel, line=ln, odds=od, at="2026-01-16T22:00:00Z")
    # Clôture (t1) : Boston se raccourcit (1.60), Miami s'allonge (2.40) → proba Boston monte.
    for sel, ln, od in [("Boston Celtics", -5.0, 1.60), ("Miami Heat", 5.0, 2.40)]:
        _snap(conn, book="pinnacle", sel=sel, line=ln, odds=od, at="2026-01-17T00:00:00Z")
    conn.commit()

    data = preprocess(conn, "m1")
    closing_odds, clv = compute_clv(data, market="spreads", selection="Boston Celtics",
                                    decided_at="2026-01-16T22:05:00Z", tipoff_utc="2026-01-17T00:20:00Z")
    assert closing_odds == 1.60
    assert clv is not None and clv > 0     # la proba de Boston a monté → CLV positif


def test_compute_clv_none_when_selection_not_quoted(conn):
    from analyzer.preprocessing import preprocess
    _snap(conn, book="pinnacle", sel="Miami Heat", line=5.0, odds=1.91, at="2026-01-17T00:00:00Z")
    conn.commit()
    data = preprocess(conn, "m1")
    closing_odds, clv = compute_clv(data, market="spreads", selection="Boston Celtics",
                                    decided_at="2026-01-16T22:05:00Z", tipoff_utc="2026-01-17T00:20:00Z")
    assert closing_odds is None and clv is None


# ─────────────────────── orchestration bout en bout ───────────────────────

def _fake_results_client(games):
    class _Fake:
        def get_games(self, start, end):
            return games
        def close(self):
            pass
    return _Fake()


def _settings() -> Settings:
    return Settings(odds_api_key="", balldontlie_api_key="k", telegram_bot_token="",
                    telegram_chat_id="", database_path=Path("x.db"), log_level="INFO")


CONFIG = {
    "display": {"timezone": "Europe/Paris"},
    "results": {"calendar_timezone": "America/New_York",
                "base_url": "https://api.balldontlie.io", "games_path": "/v1/games"},
    "evaluator": {"lookback_days": 3},
}


def test_evaluate_pending_grades_and_marks_evalue(conn):
    """Un match clos + verdict → évaluation écrite, statut EVALUE, résumé cohérent."""
    db.insert_verdict(conn, match_id="m1", verdict="SIGNAL", selection="Boston Celtics",
                      market="spreads", line=-5.0, odds_at_verdict=1.91, signal_score=6,
                      rules_triggered=json.dumps(["R1"]), rationale="…",
                      decided_at="2026-01-16T23:20:00Z")
    conn.commit()

    games = [_game("2026-01-16", BOS, MIA, 110, 100)]  # Boston gagne de 10 → couvre -5
    from datetime import datetime, timezone
    summary = evaluate_pending(conn, _settings(), CONFIG,
                               results_client=_fake_results_client(games),
                               telegram_client=None,
                               now=datetime(2026, 1, 17, 12, 0, tzinfo=timezone.utc))

    assert summary["evaluated"] == 1
    ev = conn.execute("SELECT * FROM evaluations").fetchone()
    assert ev["outcome"] == "won" and ev["home_score"] == 110
    assert conn.execute("SELECT status FROM matches WHERE match_id='m1'").fetchone()["status"] == "EVALUE"


def test_evaluate_pending_skips_when_no_result_recent(conn):
    """Match reporté (pas de résultat) et récent → sauté, reste CLOS pour réessai."""
    db.insert_verdict(conn, match_id="m1", verdict="NO_BET", selection="Boston Celtics",
                      market="spreads", line=-5.0, odds_at_verdict=1.91, signal_score=0,
                      rules_triggered="[]", rationale="…", decided_at="2026-01-16T23:20:00Z")
    conn.commit()

    from datetime import datetime, timezone
    summary = evaluate_pending(conn, _settings(), CONFIG,
                               results_client=_fake_results_client([]),  # aucun résultat
                               now=datetime(2026, 1, 17, 12, 0, tzinfo=timezone.utc))

    assert summary == {"evaluated": 0, "given_up": 0, "skipped": 1, "ungradable": 0}
    assert conn.execute("SELECT status FROM matches WHERE match_id='m1'").fetchone()["status"] == "CLOS"


def test_evaluate_pending_gives_up_on_old_unmatched(conn):
    """Match sans résultat au-delà de la fenêtre → EVALUE (abandon), pas de rescan infini."""
    db.insert_verdict(conn, match_id="m1", verdict="SIGNAL", selection="Boston Celtics",
                      market="spreads", line=-5.0, odds_at_verdict=1.91, signal_score=6,
                      rules_triggered="[]", rationale="…", decided_at="2026-01-16T23:20:00Z")
    conn.commit()

    from datetime import datetime, timezone
    summary = evaluate_pending(conn, _settings(), CONFIG,
                               results_client=_fake_results_client([]),
                               now=datetime(2026, 1, 25, 12, 0, tzinfo=timezone.utc))  # 8 j plus tard

    assert summary["given_up"] == 1
    assert conn.execute("SELECT status FROM matches WHERE match_id='m1'").fetchone()["status"] == "EVALUE"


# ─────────────────────────── bilan ───────────────────────────

def test_success_rate_excludes_push_from_denominator():
    # 1 gagné, 1 perdu, 1 push → 1 / (1+1) = 50 % (le push n'est PAS au dénominateur).
    assert success_rate(["won", "lost", "push"]) == 0.5
    # Que des pushes → aucune issue décisive → None.
    assert success_rate(["push", "push"]) is None
    assert success_rate([]) is None


def test_format_daily_report_shows_outcome_clv_rate_and_guardrail():
    lines = [
        EvalLine(home_team=BOS, away_team=MIA, verdict="SIGNAL", selection="Boston Celtics",
                 home_score=110, away_score=100, outcome="won", clv=0.03, position_action="take"),
        EvalLine(home_team="Lakers", away_team="Suns", verdict="SIGNAL", selection="Lakers",
                 home_score=100, away_score=100, outcome="push", clv=None, position_action=None),
    ]
    msg = format_daily_report("17/01/2026", lines, total_evals=12)
    assert "Bilan du 17/01/2026" in msg
    assert "✅ gagné" in msg and "➖ push" in msg
    assert "CLV +3,0 pts" in msg
    assert "ta prise" in msg
    # Taux hors push : 1 gagné, 0 perdu, 1 push → 100 %.
    assert "taux 100 % (hors push)" in msg
    assert "bruit statistique" in msg  # garde-fou < 50 évaluations


def test_format_daily_report_empty():
    assert "Aucun verdict" in format_daily_report("17/01/2026", [], total_evals=0)


# ─────────────────── CLV insensible aux snapshots post-tip-off ───────────────────

def test_clv_ignores_post_tipoff_snapshot(conn):
    """Un snapshot post-tip-off (cote live) ne fausse pas le calcul de clôture.

    Le CLV filtre sur snapshot_at <= tipoff_utc : les cotes live sont exclues.
    Bug 17/07 : 30 snapshots live à 00:45 (tip-off 23:10) auraient faussé la clôture
    sans ce filtre.
    """
    from evaluator.clv import compute_clv
    from analyzer.preprocessing import preprocess

    T0 = "2026-01-16T20:00:00+00:00"
    T1 = "2026-01-16T22:00:00+00:00"
    T2 = "2026-01-17T00:20:00+00:00"  # tip-off
    T3 = "2026-01-17T01:00:00+00:00"  # post-tip-off (live)

    db.insert_match(
        conn, match_id="m_clv", sport="basketball_wnba",
        home_team="Home", away_team="Away",
        tipoff_utc=T2, status="DECIDE", created_at=T0,
    )
    db.insert_verdict(
        conn, match_id="m_clv", verdict="SIGNAL", selection="Home", market="h2h",
        line=None, odds_at_verdict=1.90, signal_score=6,
        rules_triggered="[]", rationale="test", decided_at=T1,
        logic_version="test",
    )
    for book in ("a", "b", "c", "d", "e"):
        db.insert_snapshot(conn, match_id="m_clv", bookmaker=book, market="h2h", selection="Home", line=None, odds=1.90, snapshot_at=T0)
        db.insert_snapshot(conn, match_id="m_clv", bookmaker=book, market="h2h", selection="Away", line=None, odds=1.90, snapshot_at=T0)
        db.insert_snapshot(conn, match_id="m_clv", bookmaker=book, market="h2h", selection="Home", line=None, odds=1.85, snapshot_at=T1)
        db.insert_snapshot(conn, match_id="m_clv", bookmaker=book, market="h2h", selection="Away", line=None, odds=1.95, snapshot_at=T1)
        # Snapshot post-tip-off (cote live aberrante : 1.02)
        db.insert_snapshot(conn, match_id="m_clv", bookmaker=book, market="h2h", selection="Home", line=None, odds=1.02, snapshot_at=T3)
        db.insert_snapshot(conn, match_id="m_clv", bookmaker=book, market="h2h", selection="Away", line=None, odds=15.0, snapshot_at=T3)
    conn.commit()

    data = preprocess(conn, "m_clv")
    closing_odds, clv = compute_clv(
        data, market="h2h", selection="Home",
        decided_at=T1, tipoff_utc=T2,
    )
    # La cote de clôture doit être ~1.85 (T1, dernier avant tip-off T2),
    # PAS 1.02 (T3, post-tip-off, live).
    assert closing_odds is not None
    assert abs(closing_odds - 1.85) < 0.01  # clôture = T1, pas T3
