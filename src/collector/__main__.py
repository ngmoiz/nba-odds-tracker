"""Point d'entrée du collecteur.

Usage :
    uv run python -m collector                        # collecte conditionnelle (sport de config.yaml)
    uv run python -m collector --morning              # collecte inconditionnelle (créneau du matin)
    uv run python -m collector --sport basketball_wnba  # surcharge ponctuelle du sport

La surcharge `--sport` permet de tester sur un sport en cours (ex. WNBA l'été)
sans modifier config.yaml : la cible par défaut du projet reste la NBA.

`--morning` force la collecte même si aucun match actif n'est en base (découverte
de nouveaux matchs) et n'est pas soumis à la garde de réserve. Les autres créneaux
sont conditionnels : skip si aucun match actif ou si la réserve est sous le seuil.
"""
from __future__ import annotations

import argparse

from analyzer.analyzer import analyze_open_matches
from collector.collector import run_collection
from common.config import load_config, load_settings
from common.db import get_connection, init_db
from common.logging_config import configure_logging, get_logger
from common.odds_api_client import OddsApiClient
from notifier.notifier import notify_pending


def main() -> None:
    parser = argparse.ArgumentParser(description="Collecteur de cotes (NBA/WNBA…).")
    parser.add_argument(
        "--sport",
        help="Surcharge le sport de config.yaml (ex. basketball_wnba).",
    )
    parser.add_argument(
        "--morning",
        action="store_true",
        help="Collecte inconditionnelle (créneau du matin : découverte + exempté de la garde de réserve).",
    )
    args = parser.parse_args()

    settings = load_settings()
    config = load_config()
    configure_logging(settings.log_level)
    logger = get_logger("collector")

    sport = args.sport or config["api"]["sport"]
    force = args.morning
    logger.info("Démarrage de la collecte %s pour le sport : %s",
                "inconditionnelle (matin)" if force else "conditionnelle", sport)

    # Garantit que le schéma existe (idempotent) : le collecteur peut tourner
    # sur une base neuve sans dépendre d'un lancement préalable de init_db.
    init_db(settings.database_path)

    conn = get_connection(settings.database_path)
    try:
        client = OddsApiClient(
            api_key=settings.odds_api_key,
            sport=sport,
            region=config["api"]["region"],
            markets=config["api"]["markets"],
            odds_format=config["api"]["odds_format"],
        )
        with client:
            result = run_collection(conn, client, sport, config, force=force, settings=settings)
        # L'analyseur et le notificateur ne tournent que si la collecte a eu lieu.
        if not result.get("skipped"):
            analyze_open_matches(conn, config)
            notify_pending(conn, settings, config)
    finally:
        conn.close()


if __name__ == "__main__":
    main()