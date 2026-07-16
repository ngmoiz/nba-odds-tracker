"""Tests du rapport hebdomadaire (post-1.6) — pure agrégation sur `evaluations`.

Cas couverts :
- agrégat par marché (taux hors push, CLV moyen) ;
- agrégat par règle déclenchante (multi-comptage assumé) ;
- parsing défensif **non silencieux** (JSON malformé → warning loggé + mention) ;
- ségrégation `logic_version` (v1 vs v2, cohorte vide omise) ;
- NO_BET pressentis (faux négatifs) ;
- garde-fou règle 11 sur la **cohorte v2** (pas le cumul global) ;
- rapport vide (aucune évaluation sur la période) ;
- orchestration `run_weekly_report` (période glissante, envoi Telegram).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from common.config import Settings
from common.db import get_connection, init_db
from evaluator.evaluator import run_weekly_report
from evaluator.weekly import (
    aggregate_nobet,
    aggregate_signal_by_market,
    aggregate_signal_by_rule,
    format_weekly_report,
)

BOS, MIA = "Boston Celtics", "Miami Heat"


# ─────────────────────────── Fixtures ───────────────────────────


@pytest.fixture
def conn(tmp_path: Path):
    db_path = tmp_path / "test.db"
    init_db(db_path)
    c = get_connection(db_path)
    c.execute(
        "INSERT INTO matches VALUES "
        "('m1','basketball_nba','Boston Celtics','Miami Heat','2026-01-17T00:20:00Z',"
        "'EVALUE','2026-01-16T09:00:00Z')"
    )
    c.commit()
    yield c
    c.close()


def _add_verdict(
    conn, verdict_id, *, verdict="SIGNAL", selection=BOS, market="spreads",
    line=-5.0, rules="[]", logic_version=2, match_id="m1",
):
    """Insère un verdict avec un id déterministe (pour contrôler les jointures)."""
    conn.execute(
        "INSERT INTO verdicts (id, match_id, verdict, selection, market, line, "
        "odds_at_verdict, signal_score, rules_triggered, rationale, decided_at, "
        "logic_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (verdict_id, match_id, verdict, selection, market, line, 1.91, 6,
         rules, "…", "2026-01-16T23:00:00Z", logic_version),
    )


def _add_eval(conn, verdict_id, *, outcome, clv, evaluated_at):
    conn.execute(
        "INSERT INTO evaluations (verdict_id, home_score, away_score, outcome, "
        "closing_odds, clv, evaluated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (verdict_id, 110, 100, outcome, 1.85, clv, evaluated_at),
    )


def _row(verdict_id, logic_version, market, rules, outcome, clv):
    """Fabrique une ligne type sqlite3.Row pour les tests d'agrégation pure."""
    return {"verdict_id": verdict_id, "logic_version": logic_version, "market": market,
            "rules_triggered": rules, "outcome": outcome, "clv": clv}


def _nobet_row(logic_version, outcome):
    return {"logic_version": logic_version, "outcome": outcome}


# ─────────────────────── Agrégation par marché ───────────────────────


def test_aggregate_signal_by_market_basic():
    rows = [
        _row(1, 2, "spreads", '["R1"]', "won", 0.03),
        _row(2, 2, "spreads", '["R1"]', "lost", -0.01),
        _row(3, 2, "h2h", '["R2"]', "won", 0.05),
    ]
    stats = aggregate_signal_by_market(rows, logic_version=2)
    assert len(stats) == 2
    spread = next(s for s in stats if s.label == "spreads")
    assert spread.won == 1 and spread.lost == 1 and spread.push == 0
    assert spread.rate == 0.5
    assert spread.avg_clv == pytest.approx(0.01)  # (0.03 + -0.01) / 2
    h2h = next(s for s in stats if s.label == "h2h")
    assert h2h.rate == 1.0


def test_aggregate_signal_by_market_excludes_other_cohort():
    rows = [
        _row(1, 2, "spreads", '["R1"]', "won", 0.0),
        _row(2, 1, "spreads", '["R1"]', "lost", 0.0),
    ]
    stats = aggregate_signal_by_market(rows, logic_version=2)
    assert len(stats) == 1
    assert stats[0].won == 1 and stats[0].lost == 0


def test_aggregate_signal_by_market_push_excluded_from_rate():
    rows = [
        _row(1, 2, "spreads", '["R1"]', "won", None),
        _row(2, 2, "spreads", '["R1"]', "push", None),
    ]
    stats = aggregate_signal_by_market(rows, logic_version=2)
    assert stats[0].rate == 1.0  # 1 won / (1 won + 0 lost), push hors dénominateur
    assert stats[0].avg_clv is None  # tous les clv sont None


# ─────────────────────── Agrégation par règle ───────────────────────


def test_aggregate_signal_by_rule_multi_comptage():
    """Un SIGNAL déclenché par R1+R5 est compté dans CHAQUE règle."""
    rows = [
        _row(1, 2, "spreads", '["R1", "R5"]', "lost", -0.02),
    ]
    stats, unreadable = aggregate_signal_by_rule(rows, logic_version=2)
    assert len(stats) == 2
    r1 = next(s for s in stats if s.label == "R1")
    r5 = next(s for s in stats if s.label == "R5")
    assert r1.lost == 1 and r5.lost == 1  # compté dans les deux
    assert unreadable == 0


def test_aggregate_signal_by_rule_malformed_json_logs_and_counts(caplog):
    """JSON malformé → ligne absente des stats, warning émis, unreadable incrémenté."""
    rows = [
        _row(1, 2, "spreads", "not-json", "won", 0.0),
        _row(2, 2, "spreads", '["R1"]', "won", 0.0),
    ]
    with caplog.at_level(logging.WARNING, logger="evaluator"):
        stats, unreadable = aggregate_signal_by_rule(rows, logic_version=2)
    # La ligne malformée ne contribue à aucune règle ; seule R1 compte.
    assert len(stats) == 1
    assert stats[0].label == "R1" and stats[0].won == 1
    assert unreadable == 1
    # Warning émis avec le verdict_id concerné.
    assert any("verdict 1" in r.message for r in caplog.records)


# ─────────────────────── NO_BET pressentis ───────────────────────


def test_aggregate_nobet_false_negatives():
    rows = [
        _nobet_row(2, "won"),   # aurait gagné → faux négatif
        _nobet_row(2, "won"),
        _nobet_row(2, "lost"),
        _nobet_row(2, "push"),
    ]
    stats = aggregate_nobet(rows, logic_version=2)
    assert stats is not None
    assert stats.won == 2 and stats.lost == 1 and stats.push == 1
    assert stats.false_negative_rate == pytest.approx(2 / 3)  # 2 / (2+1)


def test_aggregate_nobet_empty_returns_none():
    assert aggregate_nobet([], logic_version=2) is None
    assert aggregate_nobet([_nobet_row(1, "won")], logic_version=2) is None


# ─────────────────────── Formatage ───────────────────────


def test_format_weekly_report_empty():
    msg = format_weekly_report("7 jours", [], [], total_evals=0, v2_evals=0)
    assert "Aucune évaluation sur la période" in msg
    assert "bruit statistique" in msg  # 0 v2 < 50


def test_format_weekly_report_guardrail_on_v2_cohort():
    """55 cumul dont 15 v2 → garde-fou actif (la cohorte de calibration est sous 50)."""
    rows = [_row(1, 2, "spreads", '["R1"]', "won", 0.02)]
    msg = format_weekly_report("7 jours", rows, [], total_evals=55, v2_evals=15)
    assert "bruit statistique" in msg
    assert "55 évaluations cumulées, dont 15 en logique v2" in msg


def test_format_weekly_report_no_guardrail_when_v2_above_50():
    """55 cumul dont 55 v2 → garde-fou inactif."""
    rows = [_row(1, 2, "spreads", '["R1"]', "won", 0.02)]
    msg = format_weekly_report("7 jours", rows, [], total_evals=55, v2_evals=55)
    assert "bruit statistique" not in msg
    assert "55 évaluations cumulées, dont 55 en logique v2" in msg


def test_format_weekly_report_segregates_logic_versions():
    signal = [
        _row(1, 2, "spreads", '["R1"]', "won", 0.02),
        _row(2, 1, "spreads", '["R1"]', "lost", -0.01),
    ]
    msg = format_weekly_report("7 jours", signal, [], total_evals=10, v2_evals=5)
    assert "Logique v2 (décision H-1)" in msg
    assert "Logique v1 (pré-correction H-1)" in msg
    # v2 d'abord
    assert msg.index("v2") < msg.index("v1")


def test_format_weekly_report_omits_empty_cohort():
    signal = [_row(1, 2, "spreads", '["R1"]', "won", 0.02)]
    msg = format_weekly_report("7 jours", signal, [], total_evals=5, v2_evals=5)
    assert "Logique v2" in msg
    assert "Logique v1" not in msg  # cohorte v1 vide → omise


def test_format_weekly_report_multi_comptage_note():
    signal = [_row(1, 2, "spreads", '["R1", "R5"]', "won", 0.02)]
    msg = format_weekly_report("7 jours", signal, [], total_evals=5, v2_evals=5)
    assert "multi-comptage assumé" in msg


def test_format_weekly_report_nobet_section():
    nobet = [_nobet_row(2, "won"), _nobet_row(2, "lost")]
    msg = format_weekly_report("7 jours", [], nobet, total_evals=5, v2_evals=5)
    assert "faux négatifs" in msg
    assert "1 auraient gagné" in msg


def test_format_weekly_report_unreadable_mention():
    """Un verdict à règles illisibles → mention visible dans la section par règle."""
    signal = [
        _row(1, 2, "spreads", "not-json", "won", 0.0),
        _row(2, 2, "spreads", '["R1"]', "won", 0.0),
    ]
    msg = format_weekly_report("7 jours", signal, [], total_evals=5, v2_evals=5)
    assert "1 verdict(s) à règles illisibles" in msg


# ─────────────────────── Orchestration (DB + envoi) ───────────────────────


def _settings() -> Settings:
    return Settings(odds_api_key="", balldontlie_api_key="k", telegram_bot_token="",
                    telegram_chat_id="", database_path=Path("x.db"), log_level="INFO")


CONFIG = {
    "display": {"timezone": "Europe/Paris"},
    "results": {"calendar_timezone": "America/New_York",
                "base_url": "https://api.balldontlie.io", "games_path": "/v1/games"},
    "evaluator": {"lookback_days": 3, "weekly_report_weekday": 0},
}


def test_run_weekly_report_gliding_window(conn):
    """Seules les évaluations avec evaluated_at >= now-7j sont incluses."""
    now = datetime(2026, 1, 20, 12, 0, tzinfo=timezone.utc)
    since = now - timedelta(days=7)

    # Verdict SIGNAL récent (dans la fenêtre).
    _add_verdict(conn, 1, rules=json.dumps(["R1"]), logic_version=2)
    _add_eval(conn, 1, outcome="won", clv=0.03, evaluated_at=now.isoformat())

    # Verdict SIGNAL ancien (hors fenêtre).
    _add_verdict(conn, 2, rules=json.dumps(["R1"]), logic_version=1)
    old_at = (since - timedelta(days=1)).isoformat()
    _add_eval(conn, 2, outcome="lost", clv=-0.01, evaluated_at=old_at)

    conn.commit()

    sent_texts: list[str] = []

    class _FakeClient:
        is_configured = True
        def send_message(self, text):
            sent_texts.append(text)
        def close(self):
            pass

    result = run_weekly_report(conn, _settings(), CONFIG, telegram_client=_FakeClient(), now=now)
    assert result is True
    assert len(sent_texts) == 1
    msg = sent_texts[0]
    # Le verdict récent (v2, won) est dans le rapport.
    assert "Logique v2" in msg
    # Le verdict ancien (v1) est hors fenêtre → cohorte v1 absente.
    assert "Logique v1" not in msg


def test_run_weekly_report_no_telegram_returns_false(conn):
    """Sans client Telegram configuré, l'envoi échoue silencieusement (no-op)."""
    now = datetime(2026, 1, 20, 12, 0, tzinfo=timezone.utc)
    _add_verdict(conn, 1, rules=json.dumps(["R1"]), logic_version=2)
    _add_eval(conn, 1, outcome="won", clv=0.03, evaluated_at=now.isoformat())
    conn.commit()

    class _Unconfigured:
        is_configured = False
        def close(self):
            pass

    result = run_weekly_report(conn, _settings(), CONFIG, telegram_client=_Unconfigured(), now=now)
    assert result is False