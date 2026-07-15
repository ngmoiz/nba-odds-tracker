"""Tests du moteur de règles R1–R4 sur données simulées.

La config est déclarée explicitement ici pour que les tests portent sur la logique
des règles, indépendamment des valeurs de config.yaml.
"""
from __future__ import annotations

from analyzer.preprocessing import preprocess_rows
from analyzer.rules import (
    evaluate_r1,
    evaluate_r2,
    evaluate_r3,
    evaluate_r4,
    evaluate_r5,
    evaluate_r6,
    evaluate_r7,
)
from analyzer.scoring import evaluate_rules, signal_score, triggered_rules
from tests import fixtures as fx

CONFIG = {
    "rules": {
        "R1_spread_line_move": {"threshold_points": 2.0, "score": 3},
        "R2_steam_move": {"threshold_prob_pct": 5.0, "window_hours": 3, "score": 3},
        "R3_sustained_trend": {"min_consecutive_snapshots": 3, "score": 2},
        "R4_multi_bookmaker_sync": {"min_bookmakers": 4, "score": 3},
        "R5_cross_market_coherence": {
            "min_spread_move_points": 1.0,
            "min_prob_move_pct": 3.0,
            "score": 2,
        },
        "R6_bookmaker_divergence": {"threshold_prob_pct": 7.0, "score": 2},
        "R7_spread_moneyline_inconsistency": {
            "min_prob_gap_pct": 3.0,
            "min_abs_spread": 1.5,
            "score": 2,
        },
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


# ─────────────────────────────── R5 ───────────────────────────────

def test_r5_triggers_when_spread_and_moneyline_agree():
    """Spread plus négatif ET proba moneyline en hausse pour la même équipe → cohérent."""
    rows = (
        fx.spreads("dk", "Home", "Away", -2.0, 1.91, 1.91, fx.T[0])
        + fx.spreads("dk", "Home", "Away", -4.0, 1.91, 1.91, fx.T[1])   # spread -2.0 pt
        + fx.h2h("dk", "Home", "Away", 1.90, 1.90, fx.T[0])              # Home ~50 %
        + fx.h2h("dk", "Home", "Away", 1.65, 2.30, fx.T[1])             # Home ~58 %
    )
    result = evaluate_r5(_data(rows), CONFIG)
    assert result.triggered and result.points == 2


def test_r5_ignores_when_moneyline_flat():
    """Le spread bouge mais le moneyline ne confirme pas → pas de cohérence croisée."""
    rows = (
        fx.spreads("dk", "Home", "Away", -2.0, 1.91, 1.91, fx.T[0])
        + fx.spreads("dk", "Home", "Away", -4.0, 1.91, 1.91, fx.T[1])
        + fx.h2h("dk", "Home", "Away", 1.90, 1.90, fx.T[0])
        + fx.h2h("dk", "Home", "Away", 1.90, 1.90, fx.T[1])  # inchangé
    )
    assert not evaluate_r5(_data(rows), CONFIG).triggered


def test_r5_ignores_when_markets_contradict():
    """Spread plus favorable mais proba en baisse (sens opposés) → non cohérent."""
    rows = (
        fx.spreads("dk", "Home", "Away", -2.0, 1.91, 1.91, fx.T[0])
        + fx.spreads("dk", "Home", "Away", -4.0, 1.91, 1.91, fx.T[1])  # Home plus favori
        + fx.h2h("dk", "Home", "Away", 1.65, 2.30, fx.T[0])            # Home ~58 %
        + fx.h2h("dk", "Home", "Away", 1.90, 1.90, fx.T[1])           # Home retombe à ~50 %
    )
    assert not evaluate_r5(_data(rows), CONFIG).triggered


# ─────────────────────────────── R6 ───────────────────────────────

def test_r6_triggers_on_bookmaker_divergence():
    rows = []
    for book in ("a", "b", "c", "d"):  # consensus à ~50 %
        rows += fx.h2h(book, "Home", "Away", 1.90, 1.90)
    rows += fx.h2h("x", "Home", "Away", 1.50, 2.60)  # book divergent : Home ~63 %
    result = evaluate_r6(_data(rows), CONFIG)
    assert result.triggered and result.orientation == "anomaly"


def test_r6_ignores_when_books_agree():
    rows = (
        fx.h2h("a", "Home", "Away", 1.90, 1.90)
        + fx.h2h("b", "Home", "Away", 1.88, 1.92)
        + fx.h2h("c", "Home", "Away", 1.92, 1.88)
    )
    assert not evaluate_r6(_data(rows), CONFIG).triggered


# ─────────────────────────────── R7 ───────────────────────────────

def test_r7_triggers_on_favorite_contradiction():
    """Spread donne Home favori (-3.5) mais moneyline donne Away favori → incohérence."""
    rows = (
        fx.h2h("x", "Home", "Away", 2.20, 1.60)                 # Away favori au moneyline
        + fx.spreads("x", "Home", "Away", -3.5, 1.91, 1.91)     # Home favori au spread
    )
    result = evaluate_r7(_data(rows), CONFIG)
    assert result.triggered and result.orientation == "anomaly"


def test_r7_ignores_when_markets_coherent():
    """Home favori des deux côtés → pas de contradiction."""
    rows = (
        fx.h2h("x", "Home", "Away", 1.60, 2.20)                 # Home favori au moneyline
        + fx.spreads("x", "Home", "Away", -3.5, 1.91, 1.91)     # Home favori au spread
    )
    assert not evaluate_r7(_data(rows), CONFIG).triggered


def test_r7_ignores_near_pickem_below_spread_guard():
    """|spread| < 1.5 (pick'em) : la contradiction est du bruit, on ne déclenche pas."""
    rows = (
        fx.h2h("x", "Home", "Away", 2.20, 1.60)
        + fx.spreads("x", "Home", "Away", -1.0, 1.91, 1.91)  # |spread| = 1.0 < 1.5
    )
    assert not evaluate_r7(_data(rows), CONFIG).triggered
