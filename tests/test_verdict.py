"""Tests de la décision finale (verdict) sur la vraie configuration."""
from __future__ import annotations

from analyzer.preprocessing import preprocess_rows
from analyzer.rules import ALL_RULES
from analyzer.scoring import evaluate_rules
from analyzer.verdict import decide
from common.config import load_config
from tests import fixtures as fx

CFG = load_config()


def _decide(rows):
    data = preprocess_rows("m", rows)
    results = evaluate_rules(data, CFG, ALL_RULES)
    return decide(data, results, CFG)


def test_no_bet_is_default():
    """Sans mouvement significatif : NO_BET, mais la sélection pressentie est stockée."""
    rows = fx.h2h("dk", "Home", "Away", 1.8, 2.0) + fx.spreads("dk", "Home", "Away", -2.5, 1.91, 1.91)
    verdict = _decide(rows)
    assert verdict.verdict == "NO_BET"
    assert verdict.selection is not None


def test_signal_when_score_reaches_threshold():
    """Gros mouvement synchronisé (R1+R4…) au-dessus du seuil → SIGNAL."""
    rows = []
    for book in ("a", "b", "c", "d"):
        rows += fx.spreads(book, "Home", "Away", -2.0, 1.91, 1.91, fx.T[0])
        rows += fx.spreads(book, "Home", "Away", -5.0, 1.91, 1.91, fx.T[1])
        rows += fx.h2h(book, "Home", "Away", 1.90, 1.90, fx.T[0])
        rows += fx.h2h(book, "Home", "Away", 1.70, 2.15, fx.T[1])
    verdict = _decide(rows)
    assert verdict.verdict == "SIGNAL"
    assert verdict.signal_score >= CFG["decision"]["signal_score_threshold"]
    assert verdict.selection == "Home"
    assert verdict.odds_at_verdict is not None


def test_anomaly_takes_precedence():
    """Une incohérence R7 oriente vers ANOMALIE (la cohérence globale n'est pas acquise)."""
    rows = fx.h2h("x", "Home", "Away", 2.20, 1.60) + fx.spreads("x", "Home", "Away", -3.5, 1.91, 1.91)
    assert _decide(rows).verdict == "ANOMALIE"
