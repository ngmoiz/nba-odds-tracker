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
    """Gros mouvement synchronisé (R1+R4…) au-dessus du seuil → SIGNAL.

    Le signal venant du spread, le verdict est enregistré sur le marché spread
    (avec sa ligne) et non sur le h2h — c'est la base correcte du CLV.
    """
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
    assert verdict.market == "spreads"       # verdict porté par le marché déclencheur
    assert verdict.line == -5.0              # ligne spread de l'équipe pressentie
    assert verdict.odds_at_verdict is not None


def test_selection_follows_spread_direction_when_h2h_flat():
    """Régression : signal spread + h2h plat → la sélection suit le spread (bon côté).

    Avant correctif, la sélection venait d'un tie-break h2h et pouvait désigner
    l'outsider (ex. « Away +5.0 » alors que la steam va sur Home -5.0).
    """
    rows = []
    for book in ("a", "b", "c", "d"):
        rows += fx.spreads(book, "Home", "Away", -2.0, 1.91, 1.91, fx.T[0])
        rows += fx.spreads(book, "Home", "Away", -5.0, 1.91, 1.91, fx.T[1])  # Home devient favori
        rows += fx.h2h(book, "Home", "Away", 1.90, 1.90, fx.T[0])
        rows += fx.h2h(book, "Home", "Away", 1.90, 1.90, fx.T[1])            # h2h PLAT
    verdict = _decide(rows)
    assert verdict.verdict == "SIGNAL"
    assert verdict.selection == "Home"   # le côté vers lequel le spread a bougé
    assert verdict.market == "spreads"
    assert verdict.line == -5.0          # Home -5.0, pas Away +5.0


def test_pressenti_follows_h2h_move_without_spread():
    """Sans spread, la sélection suit la hausse de proba h2h."""
    rows = fx.h2h("dk", "Home", "Away", 1.90, 1.90, fx.T[0]) + fx.h2h("dk", "Home", "Away", 1.60, 2.40, fx.T[1])
    verdict = _decide(rows)
    assert verdict.selection == "Home"   # proba Home en hausse
    assert verdict.market == "h2h"


def test_r7_blocks_even_a_strong_signal():
    """R7 (contradiction) casse la cohérence → ANOMALIE même avec un mouvement fort."""
    rows = []
    for book in ("a", "b", "c", "d"):
        rows += fx.spreads(book, "Home", "Away", -2.0, 1.91, 1.91, fx.T[0])
        rows += fx.spreads(book, "Home", "Away", -5.0, 1.91, 1.91, fx.T[1])  # R1 + R4
    rows += fx.h2h("x", "Home", "Away", 2.20, 1.60, fx.T[1])                 # Away favori ML
    rows += fx.spreads("x", "Home", "Away", -3.5, 1.91, 1.91, fx.T[1])       # Home favori spread → R7
    assert _decide(rows).verdict == "ANOMALIE"


def test_r6_does_not_mask_a_strong_signal():
    """Une divergence R6 ne masque pas un signal fort : SIGNAL + drapeau, score hors R6."""
    rows = []
    for book in ("a", "b", "c", "d"):
        rows += fx.spreads(book, "Home", "Away", -2.0, 1.91, 1.91, fx.T[0])
        rows += fx.spreads(book, "Home", "Away", -5.0, 1.91, 1.91, fx.T[1])  # R1 (+3) + R4 (+3)
    for book in ("a", "b", "c"):
        rows += fx.h2h(book, "Home", "Away", 1.90, 1.90, fx.T[1])            # consensus ~50 %
    rows += fx.h2h("x", "Home", "Away", 1.50, 2.60, fx.T[1])                 # book divergent → R6
    verdict = _decide(rows)
    assert verdict.verdict == "SIGNAL"
    assert "R6" in verdict.rules_triggered      # R6 noté…
    assert verdict.signal_score == 6            # …mais ses points ne comptent pas (R1+R4)


def test_r6_alone_gives_anomaly():
    """Une divergence R6 sans signal de mouvement fort → ANOMALIE."""
    rows = (
        fx.h2h("a", "Home", "Away", 1.90, 1.90)
        + fx.h2h("b", "Home", "Away", 1.90, 1.90)
        + fx.h2h("c", "Home", "Away", 1.90, 1.90)
        + fx.h2h("x", "Home", "Away", 1.50, 2.60)  # divergent
    )
    assert _decide(rows).verdict == "ANOMALIE"
