"""Tests du modèle de force Elo (V1.1 §5, chantier B5), sur la vraie configuration.

Module pur : aucune base, aucun réseau, aucun mock. Chaque test verrouille une propriété
mathématique du modèle — ce sont ces propriétés qui rendront le rejeu chronologique du
backfill fiable sur une saison entière.
"""
from __future__ import annotations

import math

import pytest

from analyzer.model import (
    EloParams,
    ModelConfigError,
    contextual_home_advantage,
    edge_points,
    edge_prob,
    expected_home_win,
    fair_spread_home,
    k_for,
    mov_multiplier,
    params_from_config,
    rest_adjustment,
    spread_to_prob,
    update_ratings,
)
from common.config import load_config

CFG = load_config()
P = params_from_config(CFG)


# ─────────────────────────── probabilité attendue ───────────────────────────


def test_expected_home_symmetric_without_hfa():
    """Deux équipes de même force sur terrain neutre : exactement 50 %.

    C'est l'ancrage du modèle — toute dérive ici fausserait toutes les autres formules.
    """
    assert expected_home_win(1500.0, 1500.0, home_advantage=0.0) == pytest.approx(0.5)
    # Vrai quelle que soit la valeur commune des notes, pas seulement à 1500.
    assert expected_home_win(1720.0, 1720.0, home_advantage=0.0) == pytest.approx(0.5)


def test_expected_home_advantage_raises_home_prob():
    """À forces égales, l'avantage du terrain seul fait passer le domicile au-dessus de 50 %.

    62 pts Elo (valeur WNBA de config.yaml) valent ~+8,8 pts de probabilité.
    """
    neutral = expected_home_win(1500.0, 1500.0, home_advantage=0.0)
    at_home = expected_home_win(1500.0, 1500.0, home_advantage=P.home_advantage_elo)
    assert at_home > neutral
    assert at_home == pytest.approx(0.5883, abs=1e-4)


def test_expected_probs_sum_to_one():
    """Vue domicile et vue extérieur sont complémentaires : E_home + E_away == 1."""
    hfa = P.home_advantage_elo
    home = expected_home_win(1560.0, 1490.0, home_advantage=hfa)
    # Même match vu de l'extérieur : les notes s'échangent, l'avantage change de camp.
    away = expected_home_win(1490.0, 1560.0, home_advantage=-hfa)
    assert home + away == pytest.approx(1.0)


# ─────────────────────── marge de victoire (amortissement) ───────────────────────


def test_mov_multiplier_dampens_blowouts():
    """Un +40 pèse plus qu'un +10, mais très loin de quatre fois plus (croissance en ln).

    Sans cet amortissement, une équipe forte qui écrase s'auto-alimenterait.
    """
    small = mov_multiplier(10, 200.0)
    blowout = mov_multiplier(40, 200.0)
    assert blowout > small                       # une marge plus large compte davantage…
    assert blowout / small < 4.0                 # …mais sous-linéairement (ln(41)/ln(11) ≈ 1,55)
    assert blowout / small == pytest.approx(math.log(41) / math.log(11))


def test_mov_multiplier_upset_moves_more():
    """À marge identique, la victoire surprise déplace plus les notes que celle du favori.

    C'est la correction d'autocorrélation : `elo_diff_winner` négatif réduit le
    dénominateur, donc gonfle le multiplicateur.
    """
    favourite_wins = mov_multiplier(10, 200.0)   # le mieux noté l'emporte
    underdog_wins = mov_multiplier(10, -200.0)   # la surprise
    assert underdog_wins > favourite_wins


def test_margin_zero_is_rejected():
    """Marge nulle = donnée corrompue (pas de nul en basket) → échec bruyant, pas 0.0."""
    with pytest.raises(ValueError, match="Marge de victoire nulle"):
        mov_multiplier(0, 200.0)


# ─────────────────────────── mise à jour des notes ───────────────────────────


def test_update_is_zero_sum():
    """Ce que l'une gagne, l'autre le perd exactement : la moyenne de la ligue est stable.

    Propriété critique pour le backfill : un rejeu de 300 matchs ne doit pas faire dériver
    le centre de gravité de la ligue.
    """
    before_sum = 1580.0 + 1470.0
    update = update_ratings(
        rating_home=1580.0, rating_away=1470.0,
        home_score=104, away_score=97,
        home_advantage=P.home_advantage_elo, k=P.k_factor,
    )
    assert update.delta_home == pytest.approx(-(update.away_after - 1470.0))
    assert update.home_after + update.away_after == pytest.approx(before_sum)
    assert update.delta_home > 0                 # le domicile a gagné : il monte


def test_favorite_narrow_win_gains_less_than_underdog():
    """Même marge, camps inversés : la surprise rapporte bien plus que le résultat attendu.

    Domicile à 1600 contre 1400 sur terrain neutre : gagner de 5 était attendu (petit
    gain), perdre de 5 ne l'était pas (grosse correction).
    """
    expected_result = update_ratings(
        rating_home=1600.0, rating_away=1400.0,
        home_score=105, away_score=100,          # le favori gagne
        home_advantage=0.0, k=P.k_factor,
    )
    upset = update_ratings(
        rating_home=1600.0, rating_away=1400.0,
        home_score=100, away_score=105,          # l'outsider gagne, même marge
        home_advantage=0.0, k=P.k_factor,
    )
    assert abs(upset.delta_home) > abs(expected_result.delta_home)
    assert upset.delta_home < 0                  # le favori battu perd des points


def test_k_burnin_switches_at_threshold():
    """Tant que la note n'a pas convergé, elle bouge vite ; ensuite elle se stabilise."""
    assert k_for(P.burnin_games - 1, P) == P.k_factor_burnin
    assert k_for(P.burnin_games, P) == P.k_factor
    assert k_for(0, P) == P.k_factor_burnin      # équipe jamais vue


# ─────────────────────────── fraîcheur / repos ───────────────────────────


def test_rest_adjustment_b2b_and_three_in_four():
    """Les deux malus sont cumulatifs (§5.2b), et une donnée absente reste neutre."""
    assert rest_adjustment(0, None, P) == P.back_to_back_elo          # match la veille
    assert rest_adjustment(1, None, P) == P.back_to_back_elo
    assert rest_adjustment(2, None, P) == P.rested_elo                # reposée = référence
    assert rest_adjustment(0, 3, P) == P.back_to_back_elo + P.three_in_four_elo   # −75
    # Repos inconnu : on n'invente pas de la fatigue (invariant 5, None explicite).
    assert rest_adjustment(None, None, P) == 0.0


def test_contextual_home_advantage_folds_rest_without_touching_ratings():
    """Le repos vit dans le terme de contexte, jamais dans les notes stockées.

    Domicile en back-to-back contre extérieur reposé : l'avantage du terrain est rogné
    de 50 pts Elo, mais aucune note n'est modifiée (pas de double comptage possible).
    """
    tired_home = contextual_home_advantage(
        home_days_rest=0, away_days_rest=3, params=P,
    )
    assert tired_home == P.home_advantage_elo + P.back_to_back_elo
    # Sans information de repos, on retombe sur le seul avantage du terrain.
    assert contextual_home_advantage(params=P) == P.home_advantage_elo
    # Et l'équipe la plus fraîche des deux prend l'avantage relatif.
    assert contextual_home_advantage(away_days_rest=0, params=P) > P.home_advantage_elo


# ─────────────────────── note → ligne → probabilité ───────────────────────


def test_fair_spread_favorite_is_negative():
    """Convention bookmaker : le favori porte une ligne négative. 25 Elo = 1 point."""
    spread = fair_spread_home(1525.0, 1500.0, home_advantage=0.0, params=P)
    assert spread == pytest.approx(-1.0)         # 25 pts Elo d'écart net → −1,0
    # L'équipe la plus faible à domicile porte une ligne positive (outsider).
    assert fair_spread_home(1475.0, 1500.0, home_advantage=0.0, params=P) == pytest.approx(1.0)


def test_spread_prob_round_trip():
    """Ligne et probabilité disent la même chose : pick'em = 50 %, et c'est monotone."""
    assert spread_to_prob(0.0, P.sigma) == pytest.approx(0.5)
    favourite = spread_to_prob(-5.0, P.sigma)
    underdog = spread_to_prob(5.0, P.sigma)
    assert favourite > 0.5 > underdog
    assert favourite + underdog == pytest.approx(1.0)   # symétrie de la normale
    # Cohérence avec le modèle : plus l'écart de notes est grand, plus la proba monte.
    strong = fair_spread_home(1600.0, 1400.0, home_advantage=0.0, params=P)
    assert spread_to_prob(strong, P.sigma) > favourite


def test_edge_sign_conventions_are_opposite():
    """Piège documenté : `edge_prob` a le signe naturel, `edge_points` l'inverse.

    Modèle plus optimiste que le marché sur le domicile → `edge_prob` positif, mais
    `edge_points` négatif (les lignes de favori sont négatives). Verrouillé par un test
    pour qu'aucune session future ne « corrige » le signe en croyant à un bug.
    """
    assert edge_prob(0.57, 0.53) == pytest.approx(0.04)
    assert edge_points(-7.0, -5.0) == pytest.approx(-2.0)


# ─────────────────────────── configuration ───────────────────────────


def test_params_from_real_config_matches_yaml():
    """Les paramètres se lisent bien depuis config.yaml, σ résolu pour le sport actif."""
    assert P.home_advantage_elo == CFG["model"]["elo"]["home_advantage_elo"]
    assert P.sigma == CFG["model"]["sigma_by_league"][CFG["api"]["sport"]]
    assert P.back_to_back_elo == CFG["model"]["elo"]["rest_adjustment_elo"]["back_to_back"]


def test_params_from_config_missing_key_raises():
    """Aucun défaut silencieux : une clé manquante lève avec son chemin (invariant 6).

    Un paramètre deviné produirait des notes fausses propagées à toute la saison par le
    rejeu chronologique — l'erreur doit sortir au chargement, pas six mois plus tard.
    """
    broken = {"api": {"sport": "basketball_wnba"}, "model": {"sigma_by_league": {}}}
    with pytest.raises(ModelConfigError, match="model.elo"):
        params_from_config(broken)

    unknown_league = {"api": {"sport": "basketball_martian"}, "model": CFG["model"]}
    with pytest.raises(ModelConfigError, match="basketball_martian"):
        params_from_config(unknown_league)


def test_params_accept_explicit_sport():
    """Le σ suit le sport demandé : pré-requis du multi-ligue (WNBA ↔ NBA, §6)."""
    nba = params_from_config(CFG, sport="basketball_nba")
    wnba = params_from_config(CFG, sport="basketball_wnba")
    assert nba.sigma != wnba.sigma
    assert isinstance(nba, EloParams)
