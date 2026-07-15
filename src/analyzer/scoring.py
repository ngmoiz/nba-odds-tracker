"""Agrégation des règles en un score de signal (section 6.4).

Exécute une liste de règles sur un match prétraité et additionne leurs points.
Le verdict final (SIGNAL / ANOMALIE / NO_BET) sera construit en couche C, à partir
du score et de l'orientation des règles déclenchées.
"""
from __future__ import annotations

from analyzer.preprocessing import MatchData
from analyzer.rules import MOVEMENT_RULES, RuleResult


def evaluate_rules(data: MatchData, config: dict, rules=MOVEMENT_RULES) -> list[RuleResult]:
    """Exécute chaque règle et renvoie la liste de tous les résultats."""
    return [rule(data, config) for rule in rules]


def signal_score(results: list[RuleResult]) -> int:
    """Somme des points des règles déclenchées."""
    return sum(result.points for result in results)


def triggered_rules(results: list[RuleResult]) -> list[RuleResult]:
    """Sous-ensemble des règles effectivement déclenchées."""
    return [result for result in results if result.triggered]
