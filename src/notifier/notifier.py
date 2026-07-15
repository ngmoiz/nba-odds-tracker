"""Notificateur : envoie sur Telegram les alertes et verdicts en attente.

Découplage (voir l'analyseur) : l'analyseur **écrit** alertes/verdicts en base, le
notificateur **lit** les lignes `notified_at IS NULL` et les envoie. La base est la
file d'attente entre les deux.

Robustesse : un envoi échoué **ne marque pas** la ligne → elle repart au prochain
passage (livraison « au moins une fois »). On committe après chaque envoi réussi
pour qu'un incident en cours de lot ne provoque au pire qu'un seul doublon.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from common import db
from common.config import Settings
from common.logging_config import get_logger
from notifier.formatting import (
    SUPERSEDED_TEXT,
    build_position_buttons,
    format_alert,
    format_cancellation,
    format_verdict,
)
from notifier.telegram_client import TelegramClient, TelegramError

logger = get_logger("notifier")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _send(client: TelegramClient, text: str, reply_markup=None) -> dict | None:
    """Envoie un message ; renvoie la réponse JSON, ou None (sans lever) si échec."""
    try:
        return client.send_message(text, reply_markup=reply_markup)
    except TelegramError as exc:
        logger.error("Échec d'envoi Telegram — ligne laissée en attente : %s", exc)
        return None


def _try_edit(client: TelegramClient, message_id: int, text: str) -> bool:
    """Édite un message (retire les boutons) ; renvoie False (sans lever) si échec."""
    try:
        client.edit_message_text(message_id, text)
        return True
    except TelegramError as exc:
        logger.error("Échec d'édition du message %s — supersession conservée : %s", message_id, exc)
        return False


def _message_id(response: dict | None) -> int | None:
    """Extrait l'id du message d'une réponse sendMessage, ou None."""
    try:
        return int(response["result"]["message_id"])
    except (KeyError, TypeError, ValueError):
        return None


def notify_pending(
    conn: sqlite3.Connection,
    settings: Settings,
    config: dict,
    client: TelegramClient | None = None,
) -> dict:
    """Envoie les alertes puis les verdicts en attente. Renvoie un résumé des envois.

    `client` peut être injecté (tests) ; sinon il est construit depuis `settings`.
    Si Telegram n'est pas configuré (token/chat_id absents), aucun envoi n'est tenté.
    """
    tz_name = config["display"]["timezone"]
    verdict_types = config["notifier"]["verdicts_notified"]
    with_buttons = set(config["notifier"]["verdicts_with_buttons"])

    owns_client = client is None
    if client is None:
        client = TelegramClient(settings.telegram_bot_token, settings.telegram_chat_id)

    summary = {"alerts": 0, "verdicts": 0}
    try:
        if not client.is_configured:
            logger.warning("Telegram non configuré (token/chat_id manquants) : envois ignorés.")
            return summary

        for row in db.get_pending_alerts(conn):
            if _send(client, format_alert(row, tz_name)) is not None:
                db.mark_alert_notified(conn, row["id"], _now_iso())
                conn.commit()
                summary["alerts"] += 1

        for row in db.get_pending_verdicts(conn, verdict_types):
            if _process_verdict(conn, client, row, tz_name, verdict_types, with_buttons):
                summary["verdicts"] += 1
    finally:
        if owns_client:
            client.close()

    logger.info("Notification terminée : %s", summary)
    return summary


def _process_verdict(conn, client, row, tz_name, verdict_types, with_buttons) -> bool:
    """Traite un verdict en file : édition de l'ancien message, puis (re)envoi éventuel.

    Renvoie True si un message a été (r)envoyé. Deux étapes indépendantes, chacune
    validée à part (at-least-once) :
    1. supersession en attente → éditer l'ancien message ; `superseded_message_id`
       n'est effacé qu'**après** une édition réussie ;
    2. si le verdict n'est pas encore notifié : envoyer le nouveau message (SIGNAL/
       ANOMALIE, avec bandeau « mis à jour » si supersession) ou, pour un NO_BET
       succédant à un message actionnable, un message d'**annulation**.
    """
    was_superseded = row["superseded_message_id"] is not None

    # Étape 1 : neutraliser l'ancien message (boutons retirés + « remplacé »).
    if was_superseded and _try_edit(client, row["superseded_message_id"], SUPERSEDED_TEXT):
        db.clear_superseded(conn, row["id"])
        conn.commit()

    # Étape 2 : (re)envoi si le verdict est en attente de notification.
    if row["notified_at"] is not None:
        return False

    if row["verdict"] in verdict_types:
        markup = build_position_buttons(row["id"]) if row["verdict"] in with_buttons else None
        response = _send(client, format_verdict(row, tz_name, updated=was_superseded), markup)
        if response is not None:
            db.set_verdict_notified(conn, row["id"], _message_id(response), _now_iso())
            conn.commit()
            return True
    elif was_superseded:  # NO_BET succédant à un signal → message d'annulation
        response = _send(client, format_cancellation(row, tz_name))
        if response is not None:
            db.set_verdict_notified(conn, row["id"], None, _now_iso())
            conn.commit()
            return True
    return False
