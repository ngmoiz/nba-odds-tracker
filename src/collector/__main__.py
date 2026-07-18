"""Point d'entrée du collecteur auto-ordonnancé (Lot 2).

Usage :
    uv run python -m collector                        # tick anonyme (auto-ordonnancement)
    uv run python -m collector --morning              # force collecte du matin (usage manuel)
    uv run python -m collector --sport basketball_wnba  # surcharge ponctuelle du sport
    uv run python -m collector --now "2026-07-18T09:00:00+00:00"  # injection de l'heure (tests)
    uv run python -m collector --no-sync              # skip analyseur + notificateur (tests)

Architecture Lot 2 (auto-ordonnancement) :
- Tick anonyme (cron */20) : évalue automatiquement les cibles dues (matin + vagues)
- `--morning` : force la collecte du matin (usage manuel, bypass idempotence quotidienne)
- `--now` : injecte l'heure actuelle (tests, simulations)
- `--no-sync` : skip analyseur + notificateur (collecte seule, pour tests)

Le collecteur décide lui-même quoi collecter selon l'heure et l'état des matchs.
Plus besoin de 6 crons distincts : un seul battement toutes les 20 min.
"""
from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone

from analyzer.analyzer import analyze_open_matches
from collector.collector import (
    ConfigurationError,
    run_collection,
    validate_collector_config,
)
from common.config import load_config, load_settings
from common.db import get_connection, init_db
from common.logging_config import configure_logging, get_logger
from common.odds_api_client import OddsApiClient
from notifier.notifier import notify_pending


def main() -> None:
    parser = argparse.ArgumentParser(description="Collecteur auto-ordonnancé (NBA/WNBA…).")
    parser.add_argument(
        "--sport",
        help="Surcharge le sport de config.yaml (ex. basketball_wnba).",
    )
    parser.add_argument(
        "--morning",
        action="store_true",
        help="Force la collecte du matin (usage manuel, bypass idempotence quotidienne).",
    )
    parser.add_argument(
        "--now",
        help="TESTS UNIQUEMENT : Injecte l'heure actuelle (format ISO UTC). INTERDIT en production (snapshots faussement horodatés).",
    )
    parser.add_argument(
        "--no-sync",
        action="store_true",
        help="Skip analyseur + notificateur (collecte seule, pour tests).",
    )
    args = parser.parse_args()

    settings = load_settings()
    config = load_config()
    configure_logging(settings.log_level)
    logger = get_logger("collector")

    # Validation au démarrage : contrainte fenêtre/tick des cibles (échec bruyant).
    # Empêche de recréer silencieusement le trou de captation des clôtures.
    try:
        validate_collector_config(config)
    except ConfigurationError as exc:
        logger.error("Configuration collecteur invalide : %s", exc)
        import sys
        sys.exit(1)

    sport = args.sport or config["api"]["sport"]
    
    # Injection de l'heure (TESTS UNIQUEMENT)
    now = None
    if args.now:
        # GARDE : --now autorisé UNIQUEMENT si marqueur de test explicite présent
        # Refusé par défaut (sécurité : snapshots faussement horodatés, collection_log corrompu)
        test_mode = os.getenv("TEST_MODE") == "true"
        if not test_mode:
            logger.error(
                "ERREUR : --now interdit (corruption append-only). "
                "Autoriser uniquement en tests avec TEST_MODE=true."
            )
            import sys
            sys.exit(1)
        
        now = datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        logger.warning("MODE TEST : Heure injectée %s (snapshots horodatés artificiellement)", now.isoformat())
    
    # Force matin (usage manuel, bypass idempotence)
    if args.morning:
        if now is None:
            now = datetime.now(timezone.utc)
        # Force l'heure à 09:00 pour déclencher _should_collect_morning
        now = now.replace(hour=9, minute=0, second=0, microsecond=0)
        logger.info("Mode --morning : force collecte du matin à %s", now.isoformat())
    
    logger.info("Démarrage du tick collecteur pour le sport : %s", sport)

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
            try:
                result = run_collection(conn, client, sport, config, settings=settings, now=now)
            except Exception as exc:
                # Attrape ConfigurationError (et autres exceptions métier)
                if isinstance(exc, ConfigurationError):
                    logger.error("Configuration invalide : %s", exc)
                    # Notification déjà envoyée par run_collection si possible
                    import sys
                    sys.exit(1)
                else:
                    # Autre exception : re-lever
                    raise
        
        # L'analyseur et le notificateur ne tournent que si la collecte a eu lieu
        # (skip si --no-sync pour tests isolés du collecteur)
        if not result.get("skipped") and not args.no_sync:
            analyze_open_matches(conn, config)
            notify_pending(conn, settings, config)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
