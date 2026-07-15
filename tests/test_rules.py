"""Tests du moteur de règles R1–R4 sur données simulées.

La config est déclarée explicitement ici pour que les tests portent sur la logique
des règles, indépendamment des valeurs de config.yaml.
"""
from __future__ import annotations

from analyzer.preprocessing import preprocess_rows
from analyzer.rules import evaluate_r1, evaluate_r2, evaluate_r3, evaluate_r4
from analyzer.scoring import evaluate_rules, signal_score, triggered_rules
from tests import fixtures as fx

CONFIG = {
    "rules": {
        "R1_spread_line_move": {"threshold_points": 2.0, "score": 3},
        "R2_steam_move": {"threshold_prob_pct": 5.0, "window_hours": 3, "score": 3},
        "R3_sustained_trend": {"min_consecutive_snapshots": 3, "score": 2},
        "R4_multi_bookmaker_sync": {"min_bookmakers": 4, "score": 3},
    }
}


def _data(rows):
    return preprocess_rows("m", rows)


# ─────────────────────────────── R1 ───────────────────────────────

def test_r1_triggers_on_large_spread_move():
    rows = (
        fx.spreads("dk", "Home", "Away", -1.5, 1.91, 1.91, fx.T[0])
        + fx.spreads("dk", "Home", "Away", -4.5, 1.91, 1.91, fx.T[1])  # 3.0 pt
    )
    result = evaluate_r1(_data(rows), CONFIG)
    assert result.triggered and result.points == 3


def test_r1_ignores_small_spread_move():
    rows = (
        fx.spreads("dk", "Home", "Away", -1.5, 1.91, 1.91, fx.T[0])
        + fx.spreads("dk", "Home", "Away", -2.5, 1.91, 1.91, fx.T[1])  # 1.0 pt
    )
    assert not evaluate_r1(_data(rows), CONFIG).triggered


def test_r1_handles_line_crossing_zero():
    """La ligne traverse zéro (le favori change de camp) : la magnitude reste correcte."""
    rows = (
        fx.spreads("dk", "Home", "Away", -1.0, 1.91, 1.91, fx.T[0])
        + fx.spreads("dk", "Home", "Away", 1.5, 1.91, 1.91, fx.T[1])  # 2.5 pt
    )
    assert evaluate_r1(_data(rows), CONFIG).triggered


# ─────────────────────────────── R2 ───────────────────────────────

def test_r2_triggers_on_steam_move():
    rows = (
        fx.h2h("dk", "Home", "Away", 1.90, 1.90, fx.T[0])   # Home ~50 %
        + fx.h2h("dk", "Home", "Away", 1.65, 2.30, fx.T[1])  # Home ~58 %, 3 h plus tard
    )
    result = evaluate_r2(_data(rows), CONFIG)
    assert result.triggered and result.points == 3


def test_r2_ignores_small_move():
    rows = (
        fx.h2h("dk", "Home", "Away", 1.90, 1.90, fx.T[0])
        + fx.h2h("dk", "Home", "Away", 1.85, 1.95, fx.T[1])  # ~1 % de variation
    )
    assert not evaluate_r2(_data(rows), CONFIG).triggered


def test_r2_ignores_move_outside_time_window():
    """Un gros mouvement mais étalé sur > 3 h ne compte pas comme steam move."""
    rows = (
        fx.h2h("dk", "Home", "Away", 1.90, 1.90, fx.T[0])
        + fx.h2h("dk", "Home", "Away", 1.65, 2.30, fx.T[3])  # 9 h plus tard
    )
    assert not evaluate_r2(_data(rows), CONFIG).triggered


# ─────────────────────────────── R3 ───────────────────────────────

def test_r3_triggers_on_sustained_trend():
    rows = (
        fx.spreads("dk", "Home", "Away", -1.5, 1.91, 1.91, fx.T[0])
        + fx.spreads("dk", "Home", "Away", -2.5, 1.91, 1.91, fx.T[1])
        + fx.spreads("dk", "Home", "Away", -3.5, 1.91, 1.91, fx.T[2])  # 3 relevés, même sens
    )
    result = evaluate_r3(_data(rows), CONFIG)
    assert result.triggered and result.points == 2


def test_r3_ignores_non_monotonic_series():
    rows = (
        fx.spreads("dk", "Home", "Away", -1.5, 1.91, 1.91, fx.T[0])
        + fx.spreads("dk", "Home", "Away", -2.5, 1.91, 1.91, fx.T[1])
        + fx.spreads("dk", "Home", "Away", -2.0, 1.91, 1.91, fx.T[2])  # rebond
    )
    assert not evaluate_r3(_data(rows), CONFIG).triggered


# ─────────────────────────────── R4 ───────────────────────────────

def test_r4_triggers_when_four_books_move_together():
    rows = []
    for book in ("a", "b", "c", "d"):  # 4 books, tous vers -3.5
        rows += fx.spreads(book, "Home", "Away", -1.5, 1.91, 1.91, fx.T[0])
        rows += fx.spreads(book, "Home", "Away", -3.5, 1.91, 1.91, fx.T[1])
    result = evaluate_r4(_data(rows), CONFIG)
    assert result.triggered and result.points == 3


def test_r4_ignores_when_only_three_books_agree():
    rows = []
    for book in ("a", "b", "c"):  # 3 books baissent
        rows += fx.spreads(book, "Home", "Away", -1.5, 1.91, 1.91, fx.T[0])
        rows += fx.spreads(book, "Home", "Away", -3.5, 1.91, 1.91, fx.T[1])
    rows += fx.spreads("d", "Home", "Away", -1.5, 1.91, 1.91, fx.T[0])  # 1 book monte
    rows += fx.spreads("d", "Home", "Away", -0.5, 1.91, 1.91, fx.T[1])
    assert not evaluate_r4(_data(rows), CONFIG).triggered


# ────────────────────────────── Scoring ──────────────────────────────

def test_scoring_sums_triggered_rules():
    """Un gros mouvement soutenu déclenche R1 (+3) et R3 (+2) → score 5."""
    rows = (
        fx.spreads("dk", "Home", "Away", -1.5, 1.91, 1.91, fx.T[0])
        + fx.spreads("dk", "Home", "Away", -3.5, 1.91, 1.91, fx.T[1])
        + fx.spreads("dk", "Home", "Away", -5.5, 1.91, 1.91, fx.T[2])
    )
    results = evaluate_rules(_data(rows), CONFIG)
    assert signal_score(results) == 5
    assert {r.rule for r in triggered_rules(results)} == {"R1", "R3"}
