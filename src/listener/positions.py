"""Enregistrement des décisions humaines : prise ('take') ou passage ('pass').

Les deux sont des décisions évaluables (résultat + CLV plus tard, via l'évaluateur) :
« passer » n'est pas « ne pas réagir ». On enregistre donc l'action ET la cote
médiane au moment du clic dans les deux cas.

Idempotence : **premier clic gagnant, toutes actions confondues**. Un second clic
(même croisé, ex. 'pass' après 'take') est ignoré — la première décision fait foi.

Fonction pure (connexion + valeurs → écriture), testable sans Telegram.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from common import db
from common.logging_config import get_logger

logger = get_logger("listener")

# Actions valides déclenchées par les deux boutons inline (contrat défini en 1.4).
TAKE = "take"
PASS = "pass"
VALID_ACTIONS = (TAKE, PASS)


@dataclass(frozen=True)
class ClickOutcome:
    """Résultat d'un clic : a-t-il été enregistré, et quelle action fait foi."""

    recorded: bool          # True si ce clic a créé la position ; False si déjà décidé
    action: str             # action retenue (celle du 1er clic si déjà décidé)
    odds_at_click: float | None


def record_click(
    conn: sqlite3.Connection,
    *,
    verdict_id: int,
    action: str,
    odds_at_click: float | None,
    clicked_at: str,
) -> ClickOutcome:
    """Enregistre la décision si le verdict n'a pas encore été cliqué. Committe si écriture.

    Renvoie un `ClickOutcome` : `recorded=False` avec l'action déjà en base si un clic
    antérieur existe (premier clic gagnant).
    """
    if action not in VALID_ACTIONS:
        raise ValueError(f"Action invalide : {action!r} (attendu {VALID_ACTIONS}).")

    existing = db.get_position(conn, verdict_id)
    if existing is not None:
        logger.info(
            "Clic ignoré pour le verdict %s : décision déjà enregistrée (%s).",
            verdict_id, existing["action"],
        )
        return ClickOutcome(False, existing["action"], existing["odds_at_click"])

    db.insert_position(
        conn,
        verdict_id=verdict_id,
        action=action,
        odds_at_click=odds_at_click,
        clicked_at=clicked_at,
    )
    conn.commit()
    logger.info("Décision '%s' enregistrée pour le verdict %s (cote %s).",
                action, verdict_id, odds_at_click)
    return ClickOutcome(True, action, odds_at_click)
