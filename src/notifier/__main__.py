"""Point d'entrée du notificateur.

Usage :
    uv run python -m notifier

Envoie sur Telegram toutes les alertes et tous les verdicts en attente
(`notified_at IS NULL`). Utile en cron ou pour rejouer manuellement des envois
laissés en attente après un incident réseau. En fonctionnement nominal, le
notificateur est aussi appelé automatiquement à la fin de chaque collecte.
"""
from __future__ import annotations

from common.config import load_config, load_settings
from common.db import get_connection, init_db
from common.logging_config import configure_logging, get_logger
from notifier.notifier import notify_pending


def main() -> None:
    settings = load_settings()
    config = load_config()
    configure_logging(settings.log_level)
    logger = get_logger("notifier")

    init_db(settings.database_path)  # garantit la présence de la colonne notified_at
    conn = get_connection(settings.database_path)
    try:
        logger.info("Envoi des notifications en attente.")
        notify_pending(conn, settings, config)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
