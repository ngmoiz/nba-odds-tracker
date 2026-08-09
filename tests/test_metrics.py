"""Tests des mesures de qualité prédictive (B5 lot 3).

Ces fonctions décident si le backfill est accepté ou rejeté : une porte calculée par du
code non vérifié n'est pas une porte. Les valeurs attendues sont donc **calculées à la
main**, jamais recopiées d'une exécution — sans quoi le test figerait le comportement
courant, bug compris.

Module pur : aucune base, aucun réseau, aucune dépendance externe.
"""
from __future__ import annotations

import math

import pytest

from analyzer.metrics import (
    CHANCE_LOG_LOSS,
    MetricError,
    Prediction,
    accuracy,
    brier,
    calibration,
    constant_baseline,
    home_win_rate,
    log_loss,
    score,
    shuffled,
    spearman,
)

# p=0,8 sur une victoire à domicile, puis p=0,6 sur une défaite à domicile.
PAIR = [Prediction(0.8, True, "a"), Prediction(0.6, False, "b")]


def test_log_loss_of_a_coin_flip_is_the_chance_reference():
    """Répondre 50 % à tout donne exactement ln 2 — la barre à franchir."""
    assert log_loss([Prediction(0.5, True), Prediction(0.5, False)]) == pytest.approx(
        CHANCE_LOG_LOSS
    )
    assert CHANCE_LOG_LOSS == pytest.approx(0.6931471805599453)


def test_log_loss_matches_a_hand_computed_value():
    """-(ln 0,8 + ln 0,4) / 2 = 0,569717…"""
    expected = -(math.log(0.8) + math.log(0.4)) / 2
    assert log_loss(PAIR) == pytest.approx(expected)
    assert log_loss(PAIR) == pytest.approx(0.5697171, abs=1e-6)


def test_brier_matches_a_hand_computed_value():
    """((0,8 − 1)² + (0,6 − 0)²) / 2 = (0,04 + 0,36) / 2 = 0,20"""
    assert brier(PAIR) == pytest.approx(0.20)


def test_accuracy_counts_the_side_not_the_confidence():
    """p=0,8 sur une victoire : juste. p=0,6 sur une défaite : faux. Donc 50 %."""
    assert accuracy(PAIR) == pytest.approx(0.5)


def test_a_confident_mistake_is_punished_far_more_than_a_hesitant_one():
    """C'est toute la raison de préférer la log-loss à l'exactitude.

    Les deux modèles se trompent de côté de la même façon ; seul l'excès de confiance
    les sépare — et c'est exactement le défaut qu'on redoute d'un Elo mal rejoué.
    """
    hesitant = log_loss([Prediction(0.51, False)])
    certain = log_loss([Prediction(0.99, False)])
    assert certain > hesitant
    assert accuracy([Prediction(0.51, False)]) == accuracy([Prediction(0.99, False)])


def test_scorecard_reports_the_gap_to_chance():
    card = score([Prediction(0.5, True), Prediction(0.5, False)])
    assert card.n == 2
    assert card.versus_chance == pytest.approx(0.0)
    assert card.beats_chance is False

    better = score([Prediction(0.9, True), Prediction(0.9, True)])
    assert better.beats_chance is True
    assert better.versus_chance < 0


def test_a_certainty_is_rejected_rather_than_clipped():
    """Une probabilité de 0 ou 1 rendrait la log-loss infinie.

    Aucun écrêtage silencieux : un modèle logistique ne peut pas produire une telle
    valeur, la rencontrer signifie qu'une valeur a été fabriquée quelque part
    (invariant 6).
    """
    for impossible in (0.0, 1.0, -0.1, 1.2):
        with pytest.raises(MetricError, match="hors bornes"):
            log_loss([Prediction(impossible, True)])


def test_an_empty_sample_fails_rather_than_returning_zero():
    """Zéro serait un score PARFAIT : le pire résultat possible pour une porte."""
    with pytest.raises(MetricError, match="vide"):
        log_loss([])


def test_home_win_rate_and_its_constant_baseline():
    predictions = [Prediction(0.9, True), Prediction(0.2, True), Prediction(0.5, False)]
    assert home_win_rate(predictions) == pytest.approx(2 / 3)

    baseline = constant_baseline(predictions, 0.5)
    assert baseline.log_loss == pytest.approx(CHANCE_LOG_LOSS)
    assert baseline.n == 3


# ───────────────────────── contrôle négatif ─────────────────────────


def test_shuffling_keeps_the_probabilities_but_breaks_the_link():
    predictions = [Prediction(0.9, True), Prediction(0.2, False), Prediction(0.7, True)]
    mixed = shuffled(predictions)
    assert sorted(p.probability for p in mixed) == sorted(
        p.probability for p in predictions
    )
    assert [p.home_won for p in mixed] == [p.home_won for p in predictions]


def test_shuffling_is_reproducible():
    """Un rapport doit se rejouer à l'identique : graine fixe."""
    predictions = [Prediction(0.1 * i, i % 2 == 0) for i in range(1, 10)]
    assert [p.probability for p in shuffled(predictions)] == [
        p.probability for p in shuffled(predictions)
    ]


def test_the_negative_control_destroys_a_genuinely_good_model():
    """LE test qui rend les portes crédibles.

    Sur un modèle qui prédit parfaitement, permuter les prédictions doit faire remonter
    la log-loss au-dessus du hasard. Si ce n'était pas le cas, la mesure serait
    complaisante et « battre le hasard » ne voudrait rien dire.
    """
    perfect = [Prediction(0.95, True) for _ in range(10)]
    perfect += [Prediction(0.05, False) for _ in range(10)]
    assert score(perfect).log_loss < CHANCE_LOG_LOSS
    assert score(shuffled(perfect)).log_loss > CHANCE_LOG_LOSS


# ───────────────────────── calibration et rangs ─────────────────────────


def test_calibration_groups_by_bucket_and_compares_to_reality():
    predictions = (
        [Prediction(0.75, True) for _ in range(3)]
        + [Prediction(0.75, False)]
        + [Prediction(0.25, False) for _ in range(4)]
    )
    buckets = {(b.low, b.high): b for b in calibration(predictions)}
    high = buckets[(0.7, 0.8)]
    assert high.n == 4
    assert high.predicted == pytest.approx(0.75)
    assert high.observed == pytest.approx(0.75)      # 3 victoires sur 4 : bien calibré
    low = buckets[(0.2, 0.3)]
    assert low.n == 4 and low.observed == pytest.approx(0.0)


def test_spearman_on_perfectly_ordered_and_reversed_series():
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)


def test_spearman_handles_ties_with_average_ranks():
    """Deux ex æquo partagent la moyenne de leurs rangs — sinon l'ordre d'itération
    déciderait du résultat, et la mesure cesserait d'être déterministe."""
    assert spearman([1, 2, 2, 3], [1, 2, 2, 3]) == pytest.approx(1.0)


def test_spearman_ignores_scale_and_only_reads_order():
    """C'est une corrélation de RANG : un classement Elo et un pourcentage de
    victoires ne vivent pas sur la même échelle, seule leur concordance compte."""
    assert spearman([1500, 1600, 1700], [0.2, 0.5, 0.9]) == pytest.approx(1.0)


def test_spearman_refuses_degenerate_inputs():
    with pytest.raises(MetricError, match="longueurs différentes"):
        spearman([1, 2], [1, 2, 3])
    with pytest.raises(MetricError, match="deux observations"):
        spearman([1], [1])
    with pytest.raises(MetricError, match="constante"):
        spearman([1, 1, 1], [1, 2, 3])
