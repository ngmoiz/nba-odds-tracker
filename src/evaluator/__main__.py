"""Point d'entrée de l'évaluateur.

Usage :
    uv run python -m evaluator

Lancé chaque matin (cron) : évalue les verdicts des matchs clos de la veille contre
les résultats officiels (balldontlie), calcule CLV, et envoie le bilan Telegram.
"""
from __future__ import annotations

from common.config import load_config, load_settings
from common.db import get_connection, init_db
from common.logging_config import configure_logging, get_logger
from evaluator.evaluator import evaluate_pending


def main() -> None:
    settings = load_settings()
    config = load_config()
    configure_logging(settings.log_level)
    logger = get_logger("evaluator")

    if not settings.balldontlie_api_key:
        logger.error("BALLDONTLIE_API_KEY manquant : impossible de récupérer les scores.")
        return

    init_db(settings.database_path)
    conn = get_connection(settings.database_path)
    try:
        logger.info("Démarrage de l'évaluation des matchs clos.")
        evaluate_pending(conn, settings, config)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
