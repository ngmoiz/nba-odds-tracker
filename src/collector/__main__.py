"""Point d'entrée du collecteur.

Usage :
    uv run python -m collector                        # sport de config.yaml (NBA)
    uv run python -m collector --sport basketball_wnba  # surcharge ponctuelle

La surcharge `--sport` permet de tester sur un sport en cours (ex. WNBA l'été)
sans modifier config.yaml : la cible par défaut du projet reste la NBA.
"""
from __future__ import annotations

import argparse

from analyzer.analyzer import analyze_open_matches
from collector.collector import run_collection
from common.config import load_config, load_settings
from common.db import get_connection, init_db
from common.logging_config import configure_logging, get_logger
from common.odds_api_client import OddsApiClient


def main() -> None:
    parser = argparse.ArgumentParser(description="Collecteur de cotes (NBA/WNBA…).")
    parser.add_argument(
        "--sport",
        help="Surcharge le sport de config.yaml (ex. basketball_wnba).",
    )
    args = parser.parse_args()

    settings = load_settings()
    config = load_config()
    configure_logging(settings.log_level)
    logger = get_logger("collector")

    sport = args.sport or config["api"]["sport"]
    logger.info("Démarrage de la collecte pour le sport : %s", sport)

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
            run_collection(conn, client, sport)
        # L'analyseur tourne immédiatement après la collecte (alertes + verdict H-1).
        analyze_open_matches(conn, config)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
