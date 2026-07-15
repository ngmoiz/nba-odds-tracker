"""Envoi direct d'un message Telegram déjà composé (bilans, rapports).

Contrairement à `notify_pending` (qui vide une file d'attente d'alertes/verdicts en
base), un bilan est un message **calculé** à la volée par l'évaluateur. On réutilise
le `TelegramClient` bas niveau de l'étape 1.4 pour l'expédier, sans passer par la base.
"""
from __future__ import annotations

from common.config import Settings
from common.logging_config import get_logger
from notifier.telegram_client import TelegramClient, TelegramError

logger = get_logger("notifier")


def send_direct(settings: Settings, text: str, client: TelegramClient | None = None) -> bool:
    """Envoie un message unique. Renvoie True si envoyé, False sinon (no-op ou échec).

    `client` peut être injecté pour les tests. Sans configuration Telegram, no-op.
    """
    owns_client = client is None
    if client is None:
        client = TelegramClient(settings.telegram_bot_token, settings.telegram_chat_id)
    try:
        if not client.is_configured:
            logger.warning("Telegram non configuré : bilan non envoyé.")
            return False
        try:
            client.send_message(text)
            return True
        except TelegramError as exc:
            logger.error("Échec d'envoi du bilan : %s", exc)
            return False
    finally:
        if owns_client:
            client.close()
