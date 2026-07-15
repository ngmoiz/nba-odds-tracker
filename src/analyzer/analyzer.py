"""Orchestration de l'analyseur : appelé après chaque collecte.

Pour chaque match encore suivi :
1. prétraite ses relevés et évalue toutes les règles (R1–R7) ;
2. émet des **alertes temps réel** (R1/R2/R4) dans la table `alerts` ;
3. si le match entre dans la **fenêtre de décision** (proche du tip-off) et n'a pas
   encore été décidé, produit le **verdict final** et le fait passer en `DECIDE`.

L'envoi Telegram n'est PAS ici : l'analyseur écrit en base ; le notificateur
(étape 1.4) lira alertes et verdicts pour les envoyer.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from analyzer.preprocessing import preprocess
from analyzer.rules import ALERT_RULES, ALL_RULES
from analyzer.scoring import evaluate_rules, movement_score
from analyzer.verdict import decide
from common import db
from common.logging_config import get_logger

logger = get_logger("analyzer")

# Statuts pour lesquels un match peut encore être analysé/décidé.
_DECIDABLE_STATUSES = ("DECOUVERT", "SUIVI")


def _in_decision_window(tipoff_utc: str, now: datetime, config: dict) -> bool:
    """Vrai si l'on est dans la fenêtre de décision avant le tip-off."""
    window = timedelta(hours=config["decision"]["window_hours"])
    tipoff = datetime.fromisoformat(tipoff_utc.replace("Z", "+00:00"))
    return timedelta(0) < (tipoff - now) <= window


def analyze_match(conn: sqlite3.Connection, match: sqlite3.Row, config: dict, now: datetime) -> dict:
    """Analyse un match : alertes temps réel + verdict si dans la fenêtre de décision."""
    match_id = match["match_id"]
    data = preprocess(conn, match_id)
    results = evaluate_rules(data, config, ALL_RULES)
    now_iso = now.isoformat()

    alerts = 0
    for result in results:
        if result.triggered and result.rule in ALERT_RULES:
            db.insert_alert(conn, match_id=match_id, rule=result.rule,
                            details=result.detail, created_at=now_iso)
            alerts += 1

    verdict_type = None
    if match["status"] in _DECIDABLE_STATUSES and _in_decision_window(match["tipoff_utc"], now, config):
        verdict = decide(data, results, config)
        db.insert_verdict(
            conn,
            match_id=match_id,
            verdict=verdict.verdict,
            selection=verdict.selection,
            market=verdict.market,
            line=verdict.line,
            odds_at_verdict=verdict.odds_at_verdict,
            signal_score=verdict.signal_score,
            rules_triggered=json.dumps(verdict.rules_triggered),
            rationale=verdict.rationale,
            decided_at=now_iso,
        )
        db.update_match_status(conn, match_id, "DECIDE")
        verdict_type = verdict.verdict
        logger.info("Verdict %s pour %s (score %d).", verdict_type, match_id, verdict.signal_score)

    return {"alerts": alerts, "verdict": verdict_type, "score": movement_score(results)}


def analyze_open_matches(conn: sqlite3.Connection, config: dict, now: datetime | None = None) -> dict:
    """Analyse tous les matchs encore suivis. Committe à la fin."""
    now = now or datetime.now(timezone.utc)
    rows = conn.execute(
        "SELECT * FROM matches WHERE status IN ('DECOUVERT', 'SUIVI')"
    ).fetchall()

    summary = {"analyzed": 0, "alerts": 0, "verdicts": 0}
    for row in rows:
        result = analyze_match(conn, row, config, now)
        summary["analyzed"] += 1
        summary["alerts"] += result["alerts"]
        if result["verdict"]:
            summary["verdicts"] += 1
    conn.commit()

    logger.info("Analyse terminée : %s", summary)
    return summary
