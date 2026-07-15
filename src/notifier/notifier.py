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
from notifier.formatting import build_position_buttons, format_alert, format_verdict
from notifier.telegram_client import TelegramClient, TelegramError

logger = get_logger("notifier")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _try_send(client: TelegramClient, text: str, reply_markup=None) -> bool:
    """Envoie un message ; renvoie False (sans lever) si Telegram échoue."""
    try:
        client.send_message(text, reply_markup=reply_markup)
        return True
    except TelegramError as exc:
        logger.error("Échec d'envoi Telegram — ligne laissée en attente : %s", exc)
        return False


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
            if _try_send(client, format_alert(row, tz_name)):
                db.mark_alert_notified(conn, row["id"], _now_iso())
                conn.commit()
                summary["alerts"] += 1

        for row in db.get_pending_verdicts(conn, verdict_types):
            markup = build_position_buttons(row["id"]) if row["verdict"] in with_buttons else None
            if _try_send(client, format_verdict(row, tz_name), reply_markup=markup):
                db.mark_verdict_notified(conn, row["id"], _now_iso())
                conn.commit()
                summary["verdicts"] += 1
    finally:
        if owns_client:
            client.close()

    logger.info("Notification terminée : %s", summary)
    return summary
