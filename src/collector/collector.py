"""Collecteur : interroge The Odds API, enregistre les relevés, fait avancer la
machine à états des matchs.

Transitions gérées ici :
    (inconnu)   --découverte-->  DECOUVERT   (+ cotes d'ouverture)
    DECOUVERT   --2e relevé--->  SUIVI
    actif       --tip-off passé-> CLOS

Les états DECIDE (analyseur) et EVALUE (évaluateur) sont hors de ce composant.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from common import db
from common.logging_config import get_logger
from common.odds_api_client import OddsEvent

logger = get_logger("collector")


def _record_snapshots(conn: sqlite3.Connection, event: OddsEvent, snapshot_at: str) -> int:
    """Aplatit un match en lignes de relevés (bookmaker × marché × sélection)."""
    count = 0
    for bookmaker in event.bookmakers:
        for market in bookmaker.markets:
            for outcome in market.outcomes:
                db.insert_snapshot(
                    conn,
                    match_id=event.id,
                    bookmaker=bookmaker.key,
                    market=market.key,
                    selection=outcome.name,
                    line=outcome.point,  # None pour h2h
                    odds=outcome.price,
                    snapshot_at=snapshot_at,
                )
                count += 1
    return count


def run_collection(conn: sqlite3.Connection, client, sport: str) -> dict[str, int]:
    """Exécute une collecte complète pour le sport donné.

    Une collecte = une transaction : on ne committe qu'à la fin, pour que la base
    reste cohérente même en cas d'erreur au milieu.
    """
    now = datetime.now(timezone.utc)
    snapshot_at = now.isoformat()

    events = client.get_odds()
    credits = getattr(client, "credits_remaining", None) or "?"
    logger.info(
        "Collecte %s : %d match(s) à venir — crédits restants : %s.",
        sport, len(events), credits,
    )

    discovered = newly_tracked = snapshots = 0
    for event in events:
        existing = db.get_match(conn, event.id)
        if existing is None:
            # Nouveau match : on l'enregistre et ce premier relevé = cotes d'ouverture.
            db.insert_match(
                conn,
                match_id=event.id,
                sport=sport,
                home_team=event.home_team,
                away_team=event.away_team,
                tipoff_utc=event.commence_time,
                status="DECOUVERT",
                created_at=snapshot_at,
            )
            discovered += 1
            logger.info(
                "Nouveau match DECOUVERT : %s @ %s (%s)",
                event.away_team, event.home_team, event.id,
            )
        elif existing["status"] == "DECOUVERT":
            # 2e relevé d'un match déjà connu : il entre en phase de suivi.
            db.update_match_status(conn, event.id, "SUIVI")
            newly_tracked += 1

        snapshots += _record_snapshots(conn, event, snapshot_at)

    closed = db.close_finished_matches(conn, now)
    conn.commit()

    summary = {
        "discovered": discovered,
        "newly_tracked": newly_tracked,
        "snapshots": snapshots,
        "closed": closed,
    }
    logger.info("Collecte terminée : %s — crédits restants : %s.", summary, credits)
    return summary
