#!/usr/bin/env python3
"""Script de neutralisation du verdict_id=2 (correctif 4, Lot 1).

Contexte : Le match New York @ Dallas (verdict_id=2) a été évalué avec des scores 0-0
(bug API balldontlie, status="post" avec données invalides). Le grading a accepté ces
scores et produit un push erroné. Ce verdict pollue les statistiques.

Action : Marquer l'évaluation comme invalidée (invalidated=1) pour l'exclure de toutes
les agrégations (compteurs, taux de réussite, rapport hebdo). Principe append-only
respecté : pas de DELETE, juste un flag.

Idempotence : Le script peut être exécuté plusieurs fois sans effet de bord (WHERE clause
vérifie que l'évaluation existe avant de la marquer).

Usage :
    python scripts/invalidate_verdict_2.py [--db-path data/nba_odds.db]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ajoute le répertoire racine au PYTHONPATH pour importer les modules du projet
sys.path.insert(0, str(Path(__file__).parent.parent))

from common.db import get_connection
from common.logging_config import get_logger

logger = get_logger("invalidate_verdict_2")


def main() -> int:
    """Point d'entrée du script."""
    parser = argparse.ArgumentParser(description="Neutralise le verdict_id=2 (scores 0-0 invalides)")
    parser.add_argument(
        "--db-path",
        type=Path,
        default=Path("data/nba_odds.db"),
        help="Chemin vers la base SQLite (défaut: data/nba_odds.db)",
    )
    args = parser.parse_args()

    if not args.db_path.exists():
        logger.error("Base de données introuvable : %s", args.db_path)
        return 1

    conn = get_connection(args.db_path)
    try:
        # Vérifie que le verdict_id=2 existe et a bien une évaluation
        verdict = conn.execute("SELECT * FROM verdicts WHERE id = 2").fetchone()
        if not verdict:
            logger.warning("verdict_id=2 introuvable — rien à faire.")
            return 0

        evaluation = conn.execute("SELECT * FROM evaluations WHERE verdict_id = 2").fetchone()
        if not evaluation:
            logger.warning("Aucune évaluation pour verdict_id=2 — rien à faire.")
            return 0

        # Affiche les détails avant neutralisation
        logger.info(
            "Verdict #2 : %s @ %s, verdict=%s, sélection=%s",
            verdict["match_id"],
            verdict["decided_at"],
            verdict["verdict"],
            verdict["selection"],
        )
        
        # Vérifie si la colonne invalidated existe (migration peut ne pas avoir été exécutée)
        try:
            invalidated_value = evaluation["invalidated"]
        except (KeyError, IndexError):
            invalidated_value = 0  # Colonne absente, défaut 0
        
        logger.info(
            "Évaluation #%d : scores %d-%d, outcome=%s, invalidated=%d",
            evaluation["id"],
            evaluation["home_score"],
            evaluation["away_score"],
            evaluation["outcome"],
            invalidated_value,
        )

        # Neutralise l'évaluation (idempotent : WHERE invalidated = 0)
        cursor = conn.execute(
            "UPDATE evaluations SET invalidated = 1 WHERE verdict_id = 2 AND invalidated = 0"
        )
        rows_affected = cursor.rowcount
        conn.commit()

        if rows_affected > 0:
            logger.info("✅ Évaluation verdict_id=2 neutralisée (invalidated=1).")
        else:
            logger.info("Évaluation verdict_id=2 déjà neutralisée — rien à faire.")

        # Vérifie le compteur après neutralisation
        count_before = conn.execute("SELECT COUNT(*) AS n FROM evaluations").fetchone()["n"]
        count_after = conn.execute("SELECT COUNT(*) AS n FROM evaluations WHERE invalidated = 0").fetchone()["n"]
        logger.info("Compteur d'évaluations : %d total, %d valides (après exclusion).", count_before, count_after)

        return 0

    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
