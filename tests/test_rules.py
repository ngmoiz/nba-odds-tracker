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


def _snap(bookmaker, market, selection, odds, line, snapshot_at):
    """Relevé unitaire (forme dict), pour construire des rows asymétriques."""
    return {
        "bookmaker": bookmaker,
        "market": market,
        "selection": selection,
        "line": line,
        "odds": odds,
        "snapshot_at": snapshot_at,
    }


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


# ─────────────────── Formatage des mouvements (R1–R4) ───────────────────
# On valide le contenu du `detail` : direction, cote médiane, Δproba en points,
# référence temporelle, conclusion « l'argent va vers ». Aucun nouveau calcul —
# les données proviennent des séries de consensus.
#
# Note : le fixture `spreads()` crée des lignes symétriques (Away = -Home), donc
# |move| Home = |move| Away → R1 sélectionne Away (tri alphabétique, `>` strict).
# Pour tester la sélection Home (baisse/favori qui se renforce), on construit des
# rows asymétriques via `_snap`.


def test_r1_detail_favorite_strengthens():
    """R1 : favori qui se renforce (Home -2,0 → -5,0) → baisse, argent vers Home.

    Rows asymétriques : Home move=3,0 > Away move=2,0 → Home sélectionnée.
    """
    rows = [
        _snap("dk", "spreads", "Home", 1.91, -2.0, fx.T[0]),
        _snap("dk", "spreads", "Away", 1.91, 1.0, fx.T[0]),
        _snap("dk", "spreads", "Home", 1.91, -5.0, fx.T[1]),
        _snap("dk", "spreads", "Away", 1.91, 3.0, fx.T[1]),
    ]
    detail = evaluate_r1(_data(rows), CONFIG).detail
    assert "baisse" in detail
    assert "ligne -2,0 → -5,0" in detail
    assert "depuis l'ouverture" in detail
    assert "l'argent va vers Home" in detail
    assert "Δproba" in detail and "pts" in detail
    assert "cote méd." in detail


def test_r1_detail_outsider_weakens():
    """R1 : outsider qui s'affaiblit (Away +2,0 → +5,0) → hausse, argent vers l'adversaire.

    Fixture symétrique : |move| égal → Away sélectionnée (tri alphabétique).
    Sa ligne passe de +2,0 à +5,0 (plus positive) → l'argent va vers Home.
    """
    rows = (
        fx.spreads("dk", "Home", "Away", -2.0, 1.91, 1.91, fx.T[0])
        + fx.spreads("dk", "Home", "Away", -5.0, 1.91, 1.91, fx.T[1])
    )
    detail = evaluate_r1(_data(rows), CONFIG).detail
    assert "hausse" in detail
    assert "ligne +2,0 → +5,0" in detail
    assert "l'argent va vers Home" in detail


def test_r1_detail_line_crosses_zero():
    """R1 : traversée de zéro (Away +1,5 → -1,5) → baisse, argent vers Away.

    Fixture symétrique : |move| égal → Away sélectionnée. Sa ligne passe de
    +1,5 à -1,5 (devient plus négative) → baisse → l'argent va vers Away.
    """
    rows = (
        fx.spreads("dk", "Home", "Away", -1.5, 1.91, 1.91, fx.T[0])
        + fx.spreads("dk", "Home", "Away", 1.5, 1.91, 1.91, fx.T[1])
    )
    detail = evaluate_r1(_data(rows), CONFIG).detail
    assert "baisse" in detail
    assert "ligne +1,5 → -1,5" in detail
    assert "l'argent va vers Away" in detail


def test_r2_detail_h2h_money_flows_to_home():
    """R2 : h2h, proba Home en hausse → Away observée en baisse, argent vers Home.

    R2 parcourt les sélections triées (["Away", "Home"]) : Away est traitée en
    premier. Sa proba baisse (1/2,30 < 1/1,90) → direction « baisse » → l'argent
    va vers Home (l'adversaire).
    """
    rows = (
        fx.h2h("dk", "Home", "Away", 1.90, 1.90, fx.T[0])
        + fx.h2h("dk", "Home", "Away", 1.65, 2.30, fx.T[1])
    )
    detail = evaluate_r2(_data(rows), CONFIG).detail
    assert "baisse" in detail
    assert "l'argent va vers Home" in detail
    assert "fenêtre ≤" in detail
    assert "Δproba" in detail and "pts" in detail


def test_r2_detail_totals_over():
    """R2 : totals Over en hausse → hausse, « vers l'Over ».

    R2 parcourt ["Over", "Under"] : Over en premier, sa proba monte → hausse.
    """
    rows = (
        fx.totals("dk", 224.5, 1.90, 1.90, fx.T[0])
        + fx.totals("dk", 224.5, 1.65, 2.30, fx.T[1])
    )
    detail = evaluate_r2(_data(rows), CONFIG).detail
    assert "hausse" in detail
    assert "vers l'Over" in detail


# ─────────────────── Cas stable (mouvement consensus négligeable) ───────────────────

def test_format_movement_stable_consensus():
    """Quand le consensus ne bouge pas (|δ| < ε), pas de conclusion directionnelle.

    On teste `_format_movement` directement : before == after (proba et cote
    identiques) → « mouvement consensus négligeable » au lieu d'une direction.
    Le seuil ε = 1e-9 (rules.py, ligne 18) distingue un vrai mouvement du bruit
    numérique sur les probabilités et lignes flottantes.
    """
    from analyzer.rules import _format_movement
    from analyzer.preprocessing import ConsensusPoint

    point = ConsensusPoint(
        market="h2h", selection="Home", snapshot_at=fx.T[0],
        line=None, prob=0.5, odds=1.90, n_books=1,
    )
    detail = _format_movement(_data(fx.h2h("dk", "Home", "Away", 1.90, 1.90)), "h2h", "Home", point, point, "test")
    assert "stable" in detail
    assert "mouvement consensus négligeable" in detail
    assert "l'argent va vers" not in detail


# ─────────────────── state_key canonique (round-trip) ───────────────────

def test_state_key_round_trip():
    """Construction puis parsing du state_key : toutes les composantes retrouvées.

    Format canonique machine (point décimal) : market/selection|signe|amplitude.
    La sélection peut contenir des espaces (ex. Portland Fire).
    """
    from analyzer.rules import _state_key, parse_state_key
    from analyzer.preprocessing import ConsensusPoint

    before = ConsensusPoint(
        market="h2h", selection="Portland Fire", snapshot_at=fx.T[0],
        line=None, prob=0.30, odds=3.00, n_books=8,
    )
    after = ConsensusPoint(
        market="h2h", selection="Portland Fire", snapshot_at=fx.T[1],
        line=None, prob=0.286, odds=3.15, n_books=9,
    )
    key = _state_key("h2h", "Portland Fire", before, after, "9")
    parsed = parse_state_key(key)
    assert parsed["market"] == "h2h"
    assert parsed["selection"] == "Portland Fire"
    assert parsed["sign"] == -1  # proba baisse
    assert parsed["amplitude"] == "9"


# ─────────────────── Quasi stable (seuils métier) ───────────────────

def test_format_movement_quasi_stable_proba():
    """h2h : Δproba < 0,2 pt → « quasi stable », pas de conclusion directionnelle.

    Un mouvement de 0,1 pt de proba est réel (au-dessus du bruit flottant _EPS)
    mais pas actionnable — c'est du bruit d'équilibrage de book.
    """
    from analyzer.rules import _format_movement
    from analyzer.preprocessing import ConsensusPoint

    before = ConsensusPoint(
        market="h2h", selection="Home", snapshot_at=fx.T[0],
        line=None, prob=0.500, odds=1.90, n_books=1,
    )
    after = ConsensusPoint(
        market="h2h", selection="Home", snapshot_at=fx.T[1],
        line=None, prob=0.501, odds=1.89, n_books=1,  # +0,1 pt de proba
    )
    detail = _format_movement(_data(fx.h2h("dk", "Home", "Away", 1.90, 1.90)), "h2h", "Home", before, after, "test")
    assert "quasi stable" in detail
    assert "l'argent va vers" not in detail
    assert "Δproba" in detail  # l'ampleur reste affichée


def test_format_movement_quasi_stable_line():
    """spreads : |Δligne| < 0,25 pt → « quasi stable », pas de conclusion directionnelle."""
    from analyzer.rules import _format_movement
    from analyzer.preprocessing import ConsensusPoint

    before = ConsensusPoint(
        market="spreads", selection="Home", snapshot_at=fx.T[0],
        line=-2.0, prob=0.5, odds=1.91, n_books=1,
    )
    after = ConsensusPoint(
        market="spreads", selection="Home", snapshot_at=fx.T[1],
        line=-2.2, prob=0.5, odds=1.91, n_books=1,  # Δligne = -0.2 < 0.25
    )
    detail = _format_movement(
        _data(fx.spreads("dk", "Home", "Away", -2.0, 1.91, 1.91, fx.T[0])),
        "spreads", "Home", before, after, "test",
    )
    assert "quasi stable" in detail
    assert "l'argent va vers" not in detail


def test_format_movement_above_negligible_proba():
    """h2h : Δproba ≥ 0,2 pt → direction normale + conclusion directionnelle."""
    from analyzer.rules import _format_movement
    from analyzer.preprocessing import ConsensusPoint

    before = ConsensusPoint(
        market="h2h", selection="Home", snapshot_at=fx.T[0],
        line=None, prob=0.500, odds=1.90, n_books=1,
    )
    after = ConsensusPoint(
        market="h2h", selection="Home", snapshot_at=fx.T[1],
        line=None, prob=0.503, odds=1.85, n_books=1,  # +0,3 pt de proba ≥ 0,2
    )
    detail = _format_movement(_data(fx.h2h("dk", "Home", "Away", 1.90, 1.90)), "h2h", "Home", before, after, "test")
    assert "hausse" in detail
    assert "l'argent va vers Home" in detail


def test_format_movement_above_negligible_line():
    """spreads : |Δligne| ≥ 0,25 pt → direction normale + conclusion directionnelle."""
    from analyzer.rules import _format_movement
    from analyzer.preprocessing import ConsensusPoint

    before = ConsensusPoint(
        market="spreads", selection="Home", snapshot_at=fx.T[0],
        line=-2.0, prob=0.5, odds=1.91, n_books=1,
    )
    after = ConsensusPoint(
        market="spreads", selection="Home", snapshot_at=fx.T[1],
        line=-2.3, prob=0.5, odds=1.91, n_books=1,  # Δligne = -0.3 ≥ 0.25
    )
    detail = _format_movement(
        _data(fx.spreads("dk", "Home", "Away", -2.0, 1.91, 1.91, fx.T[0])),
        "spreads", "Home", before, after, "test",
    )
    assert "baisse" in detail
    assert "l'argent va vers Home" in detail


# ─────────────────── Config prime sur constante (règle 0.4.7) ───────────────────

def test_config_overrides_negligible_thresholds():
    """La config prime sur les constantes _NEGLIGIBLE_* (règle 0.4.7 : rien en dur).

    Avec movement_negligible_prob = 0.5 (au lieu de 0.002 par défaut), un mouvement
    de 0,3 pt de proba — qui serait directionnel avec le défaut — devient « quasi stable ».
    Prouve que la config contrôle le comportement, pas la constante.
    """
    from analyzer.rules import _format_movement
    from analyzer.preprocessing import ConsensusPoint

    before = ConsensusPoint(
        market="h2h", selection="Home", snapshot_at=fx.T[0],
        line=None, prob=0.500, odds=1.90, n_books=1,
    )
    after = ConsensusPoint(
        market="h2h", selection="Home", snapshot_at=fx.T[1],
        line=None, prob=0.503, odds=1.85, n_books=1,  # +0,3 pt
    )
    data = _data(fx.h2h("dk", "Home", "Away", 1.90, 1.90))

    # Avec le défaut (0.002), 0,3 pt ≥ seuil → directionnel
    detail_default = _format_movement(data, "h2h", "Home", before, after, "test")
    assert "hausse" in detail_default
    assert "l'argent va vers Home" in detail_default

    # Avec config 0.5, 0,3 pt < 0.5 → quasi stable
    detail_config = _format_movement(
        data, "h2h", "Home", before, after, "test",
        negligible_prob=0.5,
    )
    assert "quasi stable" in detail_config
    assert "l'argent va vers" not in detail_config


# ─────────────────── Bornes exactes (figer < vs ≤) ───────────────────

def test_format_movement_line_at_exact_boundary():
    """spreads : |Δligne| = 0,25 pile → directionnel (convention < pour quasi-stable).

    La borne est exclusive pour quasi-stable : 0,25 n'est PAS quasi-stable,
    c'est directionnel. Figé par ce test.
    """
    from analyzer.rules import _format_movement
    from analyzer.preprocessing import ConsensusPoint

    before = ConsensusPoint(
        market="spreads", selection="Home", snapshot_at=fx.T[0],
        line=-2.0, prob=0.5, odds=1.91, n_books=1,
    )
    after = ConsensusPoint(
        market="spreads", selection="Home", snapshot_at=fx.T[1],
        line=-2.25, prob=0.5, odds=1.91, n_books=1,  # |Δ| = 0.25 exact
    )
    detail = _format_movement(
        _data(fx.spreads("dk", "Home", "Away", -2.0, 1.91, 1.91, fx.T[0])),
        "spreads", "Home", before, after, "test",
    )
    assert "baisse" in detail
    assert "l'argent va vers Home" in detail
    assert "quasi stable" not in detail


def test_format_movement_proba_at_exact_boundary():
    """h2h : Δproba = 0,002 pile → directionnel (convention < pour quasi-stable).

    La borne est exclusive pour quasi-stable : 0,002 n'est PAS quasi-stable,
    c'est directionnel. Figé par ce test.
    """
    from analyzer.rules import _format_movement
    from analyzer.preprocessing import ConsensusPoint

    before = ConsensusPoint(
        market="h2h", selection="Home", snapshot_at=fx.T[0],
        line=None, prob=0.500, odds=1.90, n_books=1,
    )
    after = ConsensusPoint(
        market="h2h", selection="Home", snapshot_at=fx.T[1],
        line=None, prob=0.502, odds=1.85, n_books=1,  # Δ = 0.002 exact
    )
    detail = _format_movement(
        _data(fx.h2h("dk", "Home", "Away", 1.90, 1.90)),
        "h2h", "Home", before, after, "test",
    )
    assert "hausse" in detail
    assert "l'argent va vers Home" in detail
    assert "quasi stable" not in detail


# ─────────────────── Formatage totals : ligne avant → après ───────────────────

def test_format_movement_totals_with_line():
    """totals : la ligne du total est affichée avant → après (ex. « ligne 163,5 → 162,5 »).

    Défaut de complétude constaté en réel : les alertes totals n'affichaient pas
    la ligne, contrairement aux spreads. La donnée existe dans ConsensusPoint.line.
    """
    from analyzer.rules import _format_movement
    from analyzer.preprocessing import ConsensusPoint

    before = ConsensusPoint(
        market="totals", selection="Over", snapshot_at=fx.T[0],
        line=163.5, prob=0.500, odds=1.91, n_books=1,
    )
    after = ConsensusPoint(
        market="totals", selection="Over", snapshot_at=fx.T[1],
        line=162.5, prob=0.520, odds=1.85, n_books=1,  # ligne baisse, proba hausse
    )
    detail = _format_movement(
        _data(fx.totals("dk", 163.5, 1.91, 1.91, fx.T[0]) + fx.totals("dk", 162.5, 1.85, 1.97, fx.T[1])),
        "totals", "Over", before, after, "test",
    )
    assert "ligne 163,5 → 162,5" in detail
    assert "📈 hausse" in detail
    assert "l'argent va vers l'Over" in detail


def test_format_movement_totals_line_stable():
    """totals : ligne stable → « ligne 162,5 » sans flèche (pas de mouvement de ligne)."""
    from analyzer.rules import _format_movement
    from analyzer.preprocessing import ConsensusPoint

    before = ConsensusPoint(
        market="totals", selection="Over", snapshot_at=fx.T[0],
        line=162.5, prob=0.500, odds=1.91, n_books=1,
    )
    after = ConsensusPoint(
        market="totals", selection="Over", snapshot_at=fx.T[1],
        line=162.5, prob=0.520, odds=1.85, n_books=1,  # ligne stable, proba hausse
    )
    detail = _format_movement(
        _data(fx.totals("dk", 162.5, 1.91, 1.91, fx.T[0]) + fx.totals("dk", 162.5, 1.85, 1.97, fx.T[1])),
        "totals", "Over", before, after, "test",
    )
    assert "ligne 162,5" in detail
    assert "→" not in detail.split("ligne")[1].split(",")[0]  # pas de flèche après "ligne 162,5"
