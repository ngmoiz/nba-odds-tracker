"""Mesure de la qualité prédictive d'un modèle de probabilité (chantier B5, lot 3).

Raison d'être. Un rejeu de saison peut s'exécuter sans la moindre erreur et produire des
notes dénuées de sens. La seule preuve qui compte n'est pas « le script tourne » mais
« les prédictions valent mieux que le hasard ». Ce module fournit les mesures qui
tranchent, et surtout le **contrôle négatif** qui vérifie que la mesure elle-même n'est
pas complaisante.

Pourquoi la log-loss plutôt que le taux de bonnes réponses
----------------------------------------------------------
L'exactitude ne regarde que le côté du pari, jamais la confiance : un modèle qui annonce
51 % et un autre qui annonce 99 % marquent pareil s'ils tombent juste. La log-loss punit
la certitude mal placée, ce qui est exactement le défaut qu'on redoute d'un Elo mal
rejoué. Sa référence est `ln 2 ≈ 0,6931` — c'est le score d'un modèle qui répond 50 % à
tout, c'est-à-dire du hasard. **Un modèle utile passe sous cette barre.**

Le piège que le contrôle négatif ferme
--------------------------------------
Un code de mesure faux (indices décalés, issue lue à l'envers) peut « battre le hasard »
sans qu'aucun test unitaire ne bronche. `shuffled()` permute les prédictions entre les
matchs : le lien prédiction ↔ résultat est détruit, donc la log-loss doit remonter à
`ln 2` ou au-dessus. Si des prédictions mélangées battent elles aussi le hasard, c'est la
mesure qui est fausse, pas le modèle qui est bon. Même principe que le calibrage du
greffon d'horloge du 2026-08-08 : un harnais incapable de virer au rouge ne prouve rien.

Module pur : aucune base, aucun réseau, aucune dépendance externe (pas de numpy/scipy).
"""
from __future__ import annotations

import math
import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

# Score de référence du hasard : ln(2). Une log-loss au-dessus signifie « pire que
# répondre 50 % à tout » — c'est-à-dire une information négative.
CHANCE_LOG_LOSS = math.log(2.0)


class MetricError(Exception):
    """Donnée de mesure impossible (probabilité hors bornes, échantillon vide)."""


@dataclass(frozen=True)
class Prediction:
    """Une probabilité annoncée avant un match, et ce qui s'est réellement passé."""

    probability: float      # probabilité annoncée pour l'équipe à domicile
    home_won: bool
    label: str = ""         # libellé facultatif (traçabilité dans les rapports)


@dataclass(frozen=True)
class ScoreCard:
    """Résultat chiffré d'un échantillon de prédictions."""

    n: int
    log_loss: float
    brier: float
    accuracy: float

    @property
    def versus_chance(self) -> float:
        """Écart relatif à la log-loss du hasard, en pourcentage (négatif = mieux)."""
        return (self.log_loss - CHANCE_LOG_LOSS) / CHANCE_LOG_LOSS * 100.0

    @property
    def beats_chance(self) -> bool:
        return self.log_loss < CHANCE_LOG_LOSS


def _validated(predictions: Iterable[Prediction]) -> list[Prediction]:
    """Vérifie que chaque probabilité est strictement dans ]0, 1[.

    Aucun écrêtage silencieux : une probabilité de 0 ou 1 rendrait la log-loss infinie,
    et un modèle logistique ne peut pas en produire. La rencontrer signifie qu'une
    valeur a été fabriquée quelque part — mieux vaut le savoir que le lisser
    (invariant 6).
    """
    items = list(predictions)
    if not items:
        raise MetricError("Échantillon de prédictions vide : aucune mesure possible.")
    for item in items:
        if not 0.0 < item.probability < 1.0:
            raise MetricError(
                f"Probabilité hors bornes : {item.probability} pour {item.label!r}. "
                f"Une log-loss ne se calcule pas sur une certitude absolue."
            )
    return items


def log_loss(predictions: Iterable[Prediction]) -> float:
    """Log-loss binaire moyenne : `-(1/N) Σ [y·ln(p) + (1-y)·ln(1-p)]`."""
    items = _validated(predictions)
    total = sum(
        math.log(item.probability) if item.home_won else math.log(1.0 - item.probability)
        for item in items
    )
    return -total / len(items)


def brier(predictions: Iterable[Prediction]) -> float:
    """Score de Brier : erreur quadratique moyenne sur la probabilité (0 = parfait)."""
    items = _validated(predictions)
    return sum((item.probability - (1.0 if item.home_won else 0.0)) ** 2
               for item in items) / len(items)


def accuracy(predictions: Iterable[Prediction]) -> float:
    """Part des matchs dont le vainqueur était le plus probable selon le modèle."""
    items = _validated(predictions)
    return sum(1 for item in items
               if (item.probability > 0.5) == item.home_won) / len(items)


def score(predictions: Iterable[Prediction]) -> ScoreCard:
    items = _validated(predictions)
    return ScoreCard(n=len(items), log_loss=log_loss(items),
                     brier=brier(items), accuracy=accuracy(items))


def home_win_rate(predictions: Iterable[Prediction]) -> float:
    """Taux de victoire à domicile observé sur l'échantillon."""
    items = _validated(predictions)
    return sum(1 for item in items if item.home_won) / len(items)


def constant_baseline(predictions: Iterable[Prediction], probability: float) -> ScoreCard:
    """Référence « même probabilité pour tous les matchs ».

    Avec le taux de victoire à domicile observé, c'est le modèle le plus bête qui
    exploite quand même une information réelle. **Battre le hasard sans battre
    celle-ci ne prouverait presque rien** : cela signifierait que le modèle n'a rien
    appris d'autre que l'avantage du terrain.
    """
    items = _validated(predictions)
    return score([Prediction(probability, item.home_won, item.label) for item in items])


def shuffled(predictions: Sequence[Prediction], *, seed: int = 20260809) -> list[Prediction]:
    """Contrôle négatif : les mêmes probabilités, réaffectées à d'autres matchs.

    Détruit le lien prédiction ↔ résultat sans changer la distribution des
    probabilités. La log-loss qui en sort doit être **au moins celle du hasard** ; si
    elle est meilleure, c'est le code de mesure qui est faux.

    Graine fixe : un rapport doit être reproductible à l'identique.
    """
    items = _validated(predictions)
    probabilities = [item.probability for item in items]
    random.Random(seed).shuffle(probabilities)
    return [Prediction(probability, item.home_won, item.label)
            for probability, item in zip(probabilities, items)]


@dataclass(frozen=True)
class CalibrationBucket:
    """Une tranche de probabilité annoncée, confrontée au réel."""

    low: float
    high: float
    n: int
    predicted: float        # moyenne des probabilités annoncées dans la tranche
    observed: float         # taux de victoire réellement observé


def calibration(predictions: Iterable[Prediction], *, width: float = 0.1
                ) -> list[CalibrationBucket]:
    """Groupe les prédictions par tranche et compare annoncé vs observé.

    Un modèle bien calibré gagne ~60 % des matchs qu'il annonce à 60 %. Affichage seul,
    jamais une porte : sur quelques centaines de matchs, les tranches extrêmes comptent
    trop peu de cas pour qu'un écart soit significatif.
    """
    items = _validated(predictions)
    buckets: list[CalibrationBucket] = []
    steps = int(round(1.0 / width))
    for index in range(steps):
        # Bornes calculées par division et non par multiplication : `7 * 0.1` vaut
        # 0.7000000000000001, alors que `7 / 10` vaut exactement le même double que le
        # littéral 0.7. Sans cela les bornes dérivent, et une tranche devient
        # impossible à désigner par sa valeur nominale.
        low, high = index / steps, (index + 1) / steps
        inside = [item for item in items
                  if low <= item.probability < high or (index == steps - 1
                                                        and item.probability == high)]
        if not inside:
            continue
        buckets.append(CalibrationBucket(
            low=low, high=high, n=len(inside),
            predicted=sum(item.probability for item in inside) / len(inside),
            observed=sum(1 for item in inside if item.home_won) / len(inside),
        ))
    return buckets


def _average_ranks(values: Sequence[float]) -> list[float]:
    """Rangs moyens (les ex æquo partagent la moyenne de leurs rangs)."""
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(order):
        end = position
        while end + 1 < len(order) and values[order[end + 1]] == values[order[position]]:
            end += 1
        shared = (position + end) / 2 + 1
        for index in range(position, end + 1):
            ranks[order[index]] = shared
        position = end + 1
    return ranks


def spearman(first: Sequence[float], second: Sequence[float]) -> float:
    """Corrélation de rang de Spearman entre deux séries de même longueur.

    Implémentée ici plutôt qu'importée : `statistics.correlation(method='ranked')`
    n'existe qu'à partir de Python 3.12, et le projet cible 3.11. Aucune dépendance
    ajoutée pour une vingtaine de lignes.
    """
    if len(first) != len(second):
        raise MetricError(
            f"Séries de longueurs différentes : {len(first)} et {len(second)}."
        )
    if len(first) < 2:
        raise MetricError("Au moins deux observations sont nécessaires.")

    ranks_first, ranks_second = _average_ranks(first), _average_ranks(second)
    mean_first = sum(ranks_first) / len(ranks_first)
    mean_second = sum(ranks_second) / len(ranks_second)

    covariance = sum((a - mean_first) * (b - mean_second)
                     for a, b in zip(ranks_first, ranks_second))
    spread_first = math.sqrt(sum((a - mean_first) ** 2 for a in ranks_first))
    spread_second = math.sqrt(sum((b - mean_second) ** 2 for b in ranks_second))
    if spread_first == 0 or spread_second == 0:
        raise MetricError(
            "Série constante : la corrélation de rang n'est pas définie."
        )
    return covariance / (spread_first * spread_second)
