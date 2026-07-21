"""Formatage partagé du CLV pour les rapports Telegram (bilan quotidien + hebdo).

Module dédié pour éviter que le bilan quotidien (`reporting.py`) et le rapport
hebdomadaire (`weekly.py`) fassent dériver deux copies indépendantes de la même
logique — exactement l'endroit où une inversion de signe ou d'unité passerait
inaperçue (correctif CLV ligne/proba, 2026-07-21).
"""
from __future__ import annotations

from common.logging_config import get_logger

logger = get_logger("evaluator")

_PROB_SUFFIX = "pts de proba"
_LINE_SUFFIX = "pt(s) de ligne"


def format_signed(value: float, decimals: int) -> str:
    """Formate un nombre signé (+/−) en notation française (virgule décimale)."""
    signe = "+" if value >= 0 else ""
    return f"{signe}{value:.{decimals}f}".replace(".", ",")


def clv_label(clv: float | None, unit: str | None) -> str:
    """Formate un CLV unique (bilan quotidien). `unit` ∈ {'prob','line'}.

    Un `clv` non-None avec une unité absente ou non reconnue est un état
    structurellement incohérent (ne devrait jamais survenir si `compute_clv` est
    correctement branché) : on ne l'affiche jamais tel quel — un nombre sans unité
    recréerait exactement la classe de bug corrigée ici. On logge une erreur et on
    affiche « CLV n/d » comme si la valeur était absente.
    """
    if clv is None:
        return "CLV n/d"
    if unit == "prob":
        return f"CLV {format_signed(clv * 100, 1)} {_PROB_SUFFIX}"
    if unit == "line":
        return f"CLV {format_signed(clv, 1)} {_LINE_SUFFIX}"
    logger.error(
        "CLV non-None sans unité reconnue (clv=%r, unit=%r) — affichage 'n/d' au lieu "
        "d'un nombre sans unité.",
        clv, unit,
    )
    return "CLV n/d"
