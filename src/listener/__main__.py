"""Point d'entrée du bot d'écoute.

Usage :
    uv run python -m listener

Démarre un processus **long-running** qui écoute les clics sur les boutons inline
des verdicts (mode polling) et enregistre les décisions ('take'/'pass') en base.
En phase 1.7 il tournera dans un conteneur avec `restart: unless-stopped`.
"""
from __future__ import annotations

from common.config import load_config, load_settings
from common.db import init_db
from common.logging_config import configure_logging, get_logger
from listener.listener import build_application


def main() -> None:
    settings = load_settings()
    config = load_config()
    configure_logging(settings.log_level)
    logger = get_logger("listener")

    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.error(
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID manquants : le bot d'écoute ne peut "
            "pas démarrer. Renseigne-les dans .env."
        )
        return

    init_db(settings.database_path)  # garantit la présence de la colonne positions.action
    application = build_application(settings, config)
    logger.info("Bot d'écoute démarré (polling). Ctrl+C pour arrêter.")
    # allowed_updates : on ne demande que les clics, pas les messages texte.
    application.run_polling(allowed_updates=["callback_query"])


if __name__ == "__main__":
    main()
