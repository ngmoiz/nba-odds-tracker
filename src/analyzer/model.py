"""Modèle de force Elo — opinion indépendante sur l'issue (V1.1 §5, chantier B5).

Raison d'être. Les règles R1–R7 mesurent le *mouvement du marché* : elles suivent
l'argent, elles ne le contredisent jamais. Les 51 évaluations de calibration ont montré
un CLV structurellement nul sur les SIGNAL — l'outil détecte des mouvements déjà
accomplis. Ce module fournit la pièce manquante : une **probabilité de victoire calculée
sans regarder les cotes**, confrontable au prix. Un mouvement qui va dans le sens du
modèle crée de la valeur ; un mouvement qui va contre lui en détruit (value trap).

**Ce module ne décide rien.** Il est pur : aucun accès base, aucun accès réseau, aucun
effet de bord, aucun état global. Il n'est appelé par aucun composant du pipeline à ce
jour (le branchement shadow puis l'activation sont des chantiers ultérieurs, cf. le bloc
`model:` de `config.yaml`, marqué INERTE).

Conventions, à lire avant de toucher une formule
------------------------------------------------
**Unité.** Tout ce qui s'appelle `*_elo` est en points Elo. Tout ce qui s'appelle
`*_spread` / `*_points` est en points de ligne. `elo_points_per_spread` (25) est le seul
pont entre les deux. Ne jamais additionner les deux familles — c'est exactement la
confusion qui a produit le bug CLV du 2026-07-21 (points de ligne mélangés à des
probabilités).

**`home_advantage`.** Un seul paramètre porte *tout* l'avantage contextuel de l'équipe à
domicile, exprimé en Elo : avantage du terrain **plus** différentiel de fraîcheur entre
les deux équipes. Il se construit une fois via `contextual_home_advantage()`, puis se
passe tel quel à `expected_home_win()` et `fair_spread_home()`. Conséquence importante :
l'ajustement de repos n'est **jamais** appliqué aux notes stockées — il ne vit que dans
ce terme de contexte, le temps d'un match. Pas de double comptage possible, et une note
en base reste une mesure de force pure, indépendante du calendrier.

**Signe de `edge_points`.** `fair_spread_home − market_spread_home`, donc un edge
**négatif** signifie que le modèle juge l'équipe à domicile **plus forte** que le marché
(le modèle lui donne une ligne plus négative). Contre-intuitif, conservé tel quel parce
que c'est la convention de la spec §5.2e ; `edge_prob` garde lui le signe naturel
(positif = le modèle est plus optimiste que le marché).

**Échec bruyant.** Une donnée impossible lève (`ModelConfigError`, `ValueError`) plutôt
que de renvoyer une valeur plausible : un rating faux se propage silencieusement à toute
la saison via le rejeu chronologique. Une donnée simplement *absente* (repos inconnu)
renvoie l'élément neutre, jamais une valeur inventée.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist
from typing import Any

# Échelle Elo standard : 400 points d'écart = 10 fois plus de chances de gagner.
_ELO_SCALE = 400.0

# Constantes du multiplicateur de marge de victoire (formulation FiveThirtyEight).
_MOV_NUMERATOR = 2.2
_MOV_DIFF_WEIGHT = 0.001

# Tolérance numérique (cohérente avec grading.py / verdict.py).
_EPS = 1e-9


class ModelConfigError(Exception):
    """Configuration du modèle absente ou incomplète (jamais de défaut silencieux)."""


@dataclass(frozen=True)
class EloParams:
    """Paramètres du modèle, extraits une fois de `config.yaml`.

    Immuable et sans dict : les formules reçoivent des champs typés, pas le YAML brut.
    """

    initial_rating: float
    k_factor: float
    k_factor_burnin: float
    burnin_games: int
    home_advantage_elo: float
    elo_points_per_spread: float
    back_to_back_elo: float
    three_in_four_elo: float
    rested_elo: float
    sigma: float


@dataclass(frozen=True)
class RatingUpdate:
    """Résultat d'une mise à jour de notes après un match joué."""

    home_after: float
    away_after: float
    delta_home: float       # variation de la note domicile ; delta_away == -delta_home
    expected_home: float    # probabilité attendue AVANT le match (traçabilité)
    mov_multiplier: float   # amortissement appliqué au K (traçabilité)


def params_from_config(config: dict[str, Any], *, sport: str | None = None) -> EloParams:
    """Construit les paramètres depuis `config.yaml`.

    `sport` sert à choisir le σ de la ligue ; par défaut celui de `api.sport`. Toute clé
    manquante lève `ModelConfigError` avec son chemin — on ne devine aucun défaut, un
    paramètre absent signifierait des ratings faux propagés à toute la saison.
    """
    model = _require(config, "model")
    elo = _require(model, "elo", path="model")
    rest = _require(elo, "rest_adjustment_elo", path="model.elo")
    sigmas = _require(model, "sigma_by_league", path="model")

    league = sport if sport is not None else _require(_require(config, "api"), "sport", path="api")
    if league not in sigmas:
        raise ModelConfigError(
            f"Aucun σ configuré pour le sport '{league}' dans model.sigma_by_league. "
            f"Sports disponibles : {sorted(sigmas)}"
        )

    return EloParams(
        initial_rating=float(_require(elo, "initial_rating", path="model.elo")),
        k_factor=float(_require(elo, "k_factor", path="model.elo")),
        k_factor_burnin=float(_require(elo, "k_factor_burnin", path="model.elo")),
        burnin_games=int(_require(elo, "burnin_games", path="model.elo")),
        home_advantage_elo=float(_require(elo, "home_advantage_elo", path="model.elo")),
        elo_points_per_spread=float(_require(elo, "elo_points_per_spread", path="model.elo")),
        back_to_back_elo=float(_require(rest, "back_to_back", path="model.elo.rest_adjustment_elo")),
        three_in_four_elo=float(_require(rest, "three_in_four", path="model.elo.rest_adjustment_elo")),
        rested_elo=float(_require(rest, "rested", path="model.elo.rest_adjustment_elo")),
        sigma=float(sigmas[league]),
    )


def _require(mapping: Any, key: str, *, path: str = "") -> Any:
    """Lit une clé obligatoire, ou lève avec son chemin complet (échec bruyant)."""
    if not isinstance(mapping, dict) or key not in mapping:
        full = f"{path}.{key}" if path else key
        raise ModelConfigError(f"Clé de configuration manquante : '{full}'.")
    return mapping[key]


# ─────────────────────────── fraîcheur (§5.2b) ───────────────────────────


def rest_adjustment(
    days_rest: int | None,
    games_in_four_nights: int | None,
    params: EloParams,
) -> float:
    """Malus de fatigue d'une équipe, en points Elo (donc ≤ 0).

    `days_rest` : nombre de jours pleins depuis le dernier match (0 ou 1 = back-to-back).
    `games_in_four_nights` : nombre de matchs joués sur la fenêtre de 4 nuits qui se
    **termine par celui-ci, ce match inclus** — c'est donc 3 qui déclenche le malus
    « 3 matchs en 4 nuits ».

    Les deux malus sont **cumulatifs** (§5.2b : « −25 Elo *supplémentaires* ») : une
    équipe en back-to-back ET en 3-en-4 cumule −75.

    Une donnée absente (`None`) renvoie l'élément neutre, jamais une valeur inventée :
    on ne fabrique pas de la fatigue faute d'historique (invariant 5).
    """
    adjustment = 0.0
    if days_rest is not None:
        adjustment += params.back_to_back_elo if days_rest <= 1 else params.rested_elo
    if games_in_four_nights is not None and games_in_four_nights >= 3:
        adjustment += params.three_in_four_elo
    return adjustment


def contextual_home_advantage(
    *,
    home_days_rest: int | None = None,
    home_games_in_four_nights: int | None = None,
    away_days_rest: int | None = None,
    away_games_in_four_nights: int | None = None,
    params: EloParams,
) -> float:
    """Avantage total de l'équipe à domicile avant le match, en points Elo.

    Terrain **plus** différentiel de fraîcheur. C'est le seul endroit où §5.2b alimente
    §5.2a et §5.2d : partout ailleurs, l'avantage circule comme un scalaire unique, ce
    qui rend le double comptage structurellement impossible.

    Sans information de repos, renvoie le seul avantage du terrain.
    """
    return (
        params.home_advantage_elo
        + rest_adjustment(home_days_rest, home_games_in_four_nights, params)
        - rest_adjustment(away_days_rest, away_games_in_four_nights, params)
    )


# ─────────────────────── probabilité attendue (§5.2a) ───────────────────────


def expected_home_win(
    rating_home: float,
    rating_away: float,
    *,
    home_advantage: float,
) -> float:
    """Probabilité que l'équipe à domicile gagne (courbe logistique Elo).

    `E_home = 1 / (1 + 10 ** (-(R_home + avantage - R_away) / 400))`

    `home_advantage` est le scalaire produit par `contextual_home_advantage()` ; le
    passer à `0.0` donne la confrontation sur terrain neutre (et, à forces égales,
    exactement 0,5 — c'est le test de symétrie).
    """
    diff = (rating_home + home_advantage) - rating_away
    return 1.0 / (1.0 + 10.0 ** (-diff / _ELO_SCALE))


# ─────────────── marge de victoire et mise à jour (§5.2c) ───────────────


def mov_multiplier(margin: int, elo_diff_winner: float) -> float:
    """Amortisseur de blowout appliqué au facteur K.

    `ln(|marge| + 1) * (2.2 / (elo_diff_winner * 0.001 + 2.2))`

    Deux effets distincts, souvent confondus :
    - le logarithme fait qu'un +40 pèse plus qu'un +10, mais **pas quatre fois plus** :
      une équipe forte qui écrase ne s'auto-alimente pas indéfiniment ;
    - le dénominateur corrige l'autocorrélation : `elo_diff_winner` est positif quand le
      favori l'emporte (dénominateur plus grand → multiplicateur plus petit) et négatif
      lors d'une surprise (dénominateur plus petit → le résultat compte davantage).

    `elo_diff_winner` = note du **gagnant** (avantage contextuel inclus s'il jouait à
    domicile) moins celle du perdant.

    Lève sur donnée impossible plutôt que de renvoyer un nombre plausible :
    - marge nulle : il n'y a pas de match nul en basket, c'est une donnée corrompue —
      même famille que le garde-fou 0-0 de l'évaluateur (`evaluator.py`) ;
    - dénominateur ≤ 0 (écart de plus de 2200 Elo, hors de portée en pratique) :
      le multiplicateur deviendrait négatif et la note partirait à l'envers.
    """
    if abs(margin) < 1:
        raise ValueError(
            "Marge de victoire nulle : impossible en basket, donnée invalide "
            "(cf. garde-fou des scores 0-0 côté évaluateur)."
        )
    denominator = elo_diff_winner * _MOV_DIFF_WEIGHT + _MOV_NUMERATOR
    if denominator <= _EPS:
        raise ValueError(
            f"elo_diff_winner={elo_diff_winner} rend le multiplicateur de marge non "
            f"physique (dénominateur {denominator} ≤ 0)."
        )
    return math.log(abs(margin) + 1) * (_MOV_NUMERATOR / denominator)


def k_for(games_played: int, params: EloParams) -> float:
    """Facteur d'apprentissage : accéléré tant que la note n'a pas convergé.

    En début de saison une note vaut peu — on la laisse bouger vite (`k_factor_burnin`),
    puis on la stabilise (`k_factor`) une fois `burnin_games` matchs intégrés.
    """
    return params.k_factor_burnin if games_played < params.burnin_games else params.k_factor


def update_ratings(
    *,
    rating_home: float,
    rating_away: float,
    home_score: int,
    away_score: int,
    home_advantage: float,
    k: float,
) -> RatingUpdate:
    """Applique le résultat d'un match aux deux notes (jeu à somme nulle).

    `R_home' = R_home + K_eff * (S_home − E_home)` et l'opposé côté extérieur : ce que
    l'une gagne, l'autre le perd exactement. C'est ce qui garde la moyenne de la ligue
    constante, et c'est la propriété que le rejeu chronologique du backfill ne doit
    jamais violer.

    Les notes passées et renvoyées sont les notes **brutes** (celles stockées en base) :
    l'avantage contextuel n'entre que dans le calcul de `E_home`, jamais dans le résultat.
    """
    expected = expected_home_win(rating_home, rating_away, home_advantage=home_advantage)
    margin = home_score - away_score
    home_won = margin > 0
    actual_home = 1.0 if home_won else 0.0

    # L'écart se mesure du point de vue du gagnant, avantage contextuel inclus pour
    # l'équipe à domicile (c'est elle qui en a bénéficié pendant le match).
    if home_won:
        elo_diff_winner = (rating_home + home_advantage) - rating_away
    else:
        elo_diff_winner = rating_away - (rating_home + home_advantage)

    multiplier = mov_multiplier(margin, elo_diff_winner)
    delta = k * multiplier * (actual_home - expected)

    return RatingUpdate(
        home_after=rating_home + delta,
        away_after=rating_away - delta,
        delta_home=delta,
        expected_home=expected,
        mov_multiplier=multiplier,
    )


# ─────────────────── note → ligne → probabilité (§5.2d/e) ───────────────────


def fair_spread_home(
    rating_home: float,
    rating_away: float,
    *,
    home_advantage: float,
    params: EloParams,
) -> float:
    """Spread théorique de l'équipe à domicile, en points de ligne.

    `-(R_home + avantage - R_away) / elo_points_per_spread`

    Signe conforme à la convention bookmaker et au reste du projet : un favori porte une
    ligne **négative**. 25 points Elo d'écart net valent 1 point de ligne.
    """
    diff = (rating_home + home_advantage) - rating_away
    return -diff / params.elo_points_per_spread


def spread_to_prob(spread_home: float, sigma: float) -> float:
    """Convertit un spread domicile en probabilité de victoire : `Φ(-spread / σ)`.

    Modélise la marge finale par une normale centrée sur le spread, d'écart-type `σ`
    propre à la ligue (~10,5 WNBA, ~11,5 NBA). Une ligne à 0 rend exactement 0,5.

    Utilise `statistics.NormalDist` (bibliothèque standard) : aucune dépendance ajoutée.
    """
    if sigma <= _EPS:
        raise ValueError(f"σ doit être strictement positif (reçu {sigma}).")
    return NormalDist().cdf(-spread_home / sigma)


def edge_prob(p_model: float, p_market: float) -> float:
    """Écart de probabilité modèle − marché (signe naturel : positif = modèle optimiste).

    `p_market` est la probabilité **dé-margée** du consensus, celle que produit déjà
    `analyzer/preprocessing.py` — jamais un `1/cote` brut, qui contiendrait la marge du
    bookmaker (principe métier 1.1).
    """
    return p_model - p_market


def edge_points(fair_spread: float, market_spread: float) -> float:
    """Écart de ligne modèle − marché, en points (§5.2e).

    ⚠️ Signe contre-intuitif, conservé tel quel depuis la spec : les lignes de favori
    étant négatives, un edge **négatif** signifie que le modèle juge l'équipe **plus
    forte** que le marché. Exemple : modèle −7,0 contre marché −5,0 → −2,0, soit deux
    points de valeur en faveur de l'équipe à domicile.
    """
    return fair_spread - market_spread
