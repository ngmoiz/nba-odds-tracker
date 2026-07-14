"""Initialise la base SQLite du projet (tables, index, triggers append-only).

Usage :
    uv run python scripts/init_db.py

Idempotent : peut être relancé sans risque (CREATE ... IF NOT EXISTS).
"""
from __future__ import annotations

from common.config import load_settings
from common.db import init_db
from common.logging_config import configure_logging, get_logger


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    logger = get_logger("init_db")
    logger.info("Démarrage de l'initialisation de la base.")
    init_db(settings.database_path)
    logger.info("Initialisation terminée.")


if __name__ == "__main__":
    main()
