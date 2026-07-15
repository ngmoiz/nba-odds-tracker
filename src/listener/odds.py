"""Cote de référence au moment du clic (étape 1.5, option 1).

Quand le développeur clique, on enregistre la **cote médiane du dernier relevé**
disponible en base pour le marché et la sélection du verdict — sans appel API (zéro
quota). C'est la lecture la plus fidèle à « la cote au moment du clic ».

Repli : s'il n'existe aucun relevé exploitable (marché non coté au dernier instant,
ou aucun relevé), on retombe sur `odds_at_verdict` figée au moment du verdict.

Fonction pure (une connexion + une ligne de verdict → un flottant), testable sans
Telegram ni réseau.
"""
from __future__ import annotations

import sqlite3

from analyzer.preprocessing import preprocess
from common.logging_config import get_logger

logger = get_logger("listener")


def current_median_odds(conn: sqlite3.Connection, verdict: sqlite3.Row) -> float | None:
    """Médiane des books au dernier relevé pour (marché, sélection) du verdict.

    Retombe sur `odds_at_verdict` si le marché/sélection n'est pas coté au dernier
    instant connu (ou en l'absence de relevé).
    """
    market = verdict["market"]
    selection = verdict["selection"]
    fallback = verdict["odds_at_verdict"]

    if not market or not selection:
        return fallback

    data = preprocess(conn, verdict["match_id"])
    times = data.times()
    if not times:
        return fallback

    point = data.consensus_at(market, selection, times[-1])
    if point is None:
        logger.info(
            "Aucun consensus %s/%s au dernier relevé du match %s : repli sur odds_at_verdict.",
            market, selection, verdict["match_id"],
        )
        return fallback
    return point.odds
