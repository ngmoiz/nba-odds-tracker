"""Traitement métier d'un clic (étape 1.5 + correctif H-1), sans dépendance Telegram.

Réunit les briques pures (`odds`, `positions`) et ajoute la **protection anti-clic
sur message périmé** : un bouton ne vaut que sur le message **courant** du verdict.
Si le verdict a été re-décidé (message remplacé), l'ancien message porte encore ses
boutons jusqu'à son édition — un clic dessus doit être **rejeté**, jamais enregistré.

Fonction pure (connexion + valeurs → résultat), testable sans Telegram.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from common import db
from common.logging_config import get_logger
from listener.odds import current_median_odds
from listener.positions import record_click

logger = get_logger("listener")


@dataclass(frozen=True)
class ClickResult:
    """Issue d'un clic pour l'affichage : statut + éventuelles action/cote retenues."""

    status: str                     # 'recorded' | 'duplicate' | 'stale' | 'unknown'
    action: str | None = None
    odds_at_click: float | None = None


def handle_click(
    conn: sqlite3.Connection,
    *,
    verdict_id: int,
    callback_message_id: int | None,
    action: str,
    clicked_at: str,
) -> ClickResult:
    """Enregistre la décision si le clic vient du message courant du verdict.

    - verdict inconnu → 'unknown' ;
    - clic sur un message qui n'est plus le message courant → 'stale' (rien enregistré) ;
    - sinon → 'recorded' (1er clic) ou 'duplicate' (verdict déjà décidé).
    """
    verdict = db.get_verdict(conn, verdict_id)
    if verdict is None:
        return ClickResult("unknown")

    current_message_id = verdict["telegram_message_id"]
    if current_message_id is None or callback_message_id != current_message_id:
        logger.info(
            "Clic périmé sur le verdict %s (message %s ≠ courant %s) : rejeté.",
            verdict_id, callback_message_id, current_message_id,
        )
        return ClickResult("stale")

    odds = current_median_odds(conn, verdict)
    outcome = record_click(
        conn, verdict_id=verdict_id, action=action, odds_at_click=odds, clicked_at=clicked_at
    )
    return ClickResult(
        "recorded" if outcome.recorded else "duplicate",
        outcome.action,
        outcome.odds_at_click,
    )
