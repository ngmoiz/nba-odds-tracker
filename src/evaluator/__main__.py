"""Point d'entrée de l'évaluateur.

Usage :
    uv run python -m evaluator

Lancé chaque matin (cron) : évalue les verdicts des matchs clos de la veille contre
les résultats officiels (balldontlie), calcule CLV, et envoie le bilan Telegram.
"""
from __future__ import annotations

from datetime import datetime, timezone

from common.config import load_config, load_settings
from common.db import get_connection, init_db
from common.logging_config import configure_logging, get_logger
from evaluator.evaluator import evaluate_pending, run_weekly_report


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
        now = datetime.now(timezone.utc)
        logger.info("Démarrage de l'évaluation des matchs clos.")
        evaluate_pending(conn, settings, config, now=now)

        # Rapport hebdomadaire le lundi matin (en plus du bilan quotidien).
        weekly_weekday = config["evaluator"].get("weekly_report_weekday", 0)
        if now.weekday() == weekly_weekday:
            logger.info("Lundi : envoi du rapport hebdomadaire en plus du bilan.")
            run_weekly_report(conn, settings, config, now=now)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
