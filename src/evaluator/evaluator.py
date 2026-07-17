"""Orchestration de l'évaluateur (étape 1.6), lancé chaque matin par cron.

Pour chaque match **clos** non encore évalué : récupère le résultat officiel
(balldontlie), évalue son (ses) verdict(s) — `verdict_won`, cote de clôture, CLV —,
écrit dans `evaluations`, passe le match en `EVALUE`, puis envoie le **bilan** Telegram.

Découplage : l'auto-évaluation du modèle porte sur **tous** les verdicts (y compris
`NO_BET` et indépendamment des positions personnelles). Les positions, si elles
existent, enrichissent seulement l'affichage du bilan.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from analyzer.preprocessing import preprocess
from common import db
from common.config import Settings
from common.db import DECISION_LOGIC_VERSION
from common.logging_config import get_logger
from common.results_api_client import ResultsApiClient
from evaluator.clv import compute_clv
from evaluator.grading import grade_verdict
from evaluator.reconcile import find_result, tipoff_calendar_date
from evaluator.reporting import EvalLine, format_daily_report
from evaluator.weekly import format_weekly_report
from notifier.direct import send_direct

logger = get_logger("evaluator")


def _date_range(now: datetime, calendar_tz: str, lookback_days: int) -> tuple[str, str]:
    """Fenêtre de dates (calendrier US) à interroger côté balldontlie."""
    today = now.astimezone(ZoneInfo(calendar_tz)).date()
    start = today - timedelta(days=lookback_days)
    return start.isoformat(), today.isoformat()


def evaluate_pending(
    conn: sqlite3.Connection,
    settings: Settings,
    config: dict,
    *,
    results_client: ResultsApiClient | None = None,
    telegram_client=None,
    now: datetime | None = None,
) -> dict:
    """Évalue les matchs clos, écrit les évaluations, envoie le bilan. Renvoie un résumé."""
    now = now or datetime.now(timezone.utc)
    calendar_tz = config["results"]["calendar_timezone"]
    lookback = config["evaluator"]["lookback_days"]
    today_cal = now.astimezone(ZoneInfo(calendar_tz)).date()

    matches = db.get_matches_to_evaluate(conn)
    summary = {"evaluated": 0, "given_up": 0, "skipped": 0, "ungradable": 0}
    if not matches:
        logger.info("Aucun match clos à évaluer.")
        return summary

    start_date, end_date = _date_range(now, calendar_tz, lookback)
    owns_client = results_client is None
    if results_client is None:
        results_client = ResultsApiClient.from_config(settings, config)
    try:
        games = results_client.get_games(start_date, end_date)
    finally:
        if owns_client:
            results_client.close()

    now_iso = now.isoformat()
    lines: list[EvalLine] = []
    for match in matches:
        result = find_result(
            games,
            home_team=match["home_team"],
            away_team=match["away_team"],
            tipoff_utc=match["tipoff_utc"],
            calendar_tz=calendar_tz,
        )
        if result is None or not result.is_final:
            # Pas (encore) de résultat : on réessaie demain, sauf si le match est trop
            # ancien (au-delà de la fenêtre) — dans ce cas on abandonne pour ne pas le
            # rescanner indéfiniment.
            age = (today_cal - tipoff_calendar_date(match["tipoff_utc"], calendar_tz)).days
            if age > lookback:
                db.update_match_status(conn, match["match_id"], "EVALUE")
                summary["given_up"] += 1
                logger.warning("Match %s sans résultat après %d j : abandon.", match["match_id"], age)
            else:
                summary["skipped"] += 1
            continue

        data = preprocess(conn, match["match_id"])
        for verdict in db.get_verdicts_for_match(conn, match["match_id"]):
            outcome = grade_verdict(
                market=verdict["market"],
                selection=verdict["selection"],
                line=verdict["line"],
                home_team=match["home_team"],
                away_team=match["away_team"],
                home_score=result.home_score,
                away_score=result.away_score,
            )
            if outcome is None:
                # Sélection incohérente avec le match (données) : non notable, on ne
                # fabrique pas une issue arbitraire. On log et on n'insère rien.
                logger.warning(
                    "Verdict %s non notable (sélection '%s' hors du match %s).",
                    verdict["id"], verdict["selection"], match["match_id"],
                )
                summary["ungradable"] += 1
                continue

            closing_odds, clv = compute_clv(
                data,
                market=verdict["market"],
                selection=verdict["selection"],
                decided_at=verdict["decided_at"],
                tipoff_utc=match["tipoff_utc"],
            )
            db.insert_evaluation(
                conn,
                verdict_id=verdict["id"],
                home_score=result.home_score,
                away_score=result.away_score,
                outcome=outcome,
                closing_odds=closing_odds,
                clv=clv,
                evaluated_at=now_iso,
            )
            position = db.get_position(conn, verdict["id"])
            lines.append(
                EvalLine(
                    home_team=match["home_team"],
                    away_team=match["away_team"],
                    verdict=verdict["verdict"],
                    selection=verdict["selection"],
                    home_score=result.home_score,
                    away_score=result.away_score,
                    outcome=outcome,
                    clv=clv,
                    position_action=position["action"] if position else None,
                )
            )
            summary["evaluated"] += 1

        db.update_match_status(conn, match["match_id"], "EVALUE")

    conn.commit()

    if lines:
        day_label = now.astimezone(ZoneInfo(config["display"]["timezone"])).strftime("%d/%m/%Y")
        text = format_daily_report(day_label, lines, db.count_evaluations(conn))
        send_direct(settings, text, client=telegram_client)

    logger.info("Évaluation terminée : %s", summary)
    return summary


def run_weekly_report(
    conn: sqlite3.Connection,
    settings: Settings,
    config: dict,
    *,
    telegram_client=None,
    now: datetime | None = None,
) -> bool:
    """Calcule et envoie le rapport hebdomadaire (7 jours glissants sur `evaluated_at`).

    Pure agrégation par-dessus `evaluations` : aucune nouvelle donnée produite.
    Segregation par `logic_version` (v1 pré-correction H-1 vs v2 décision H-1).
    Renvoie True si le rapport a été envoyé, False sinon (vide ou échec d'envoi).
    """
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=7)
    since_iso = since.isoformat()

    signal_rows = db.get_weekly_signal_evals(conn, since_iso)
    nobet_rows = db.get_weekly_nobet_evals(conn, since_iso)
    total_evals = db.count_evaluations(conn)
    v2_evals = db.count_evaluations_by_logic_version(conn, logic_version=DECISION_LOGIC_VERSION)

    display_tz = config["display"]["timezone"]
    fmt = "%d/%m/%Y"
    since_label = since.astimezone(ZoneInfo(display_tz)).strftime(fmt)
    now_label = now.astimezone(ZoneInfo(display_tz)).strftime(fmt)
    week_label = f"7 derniers jours ({since_label} → {now_label})"

    text = format_weekly_report(week_label, signal_rows, nobet_rows, total_evals, v2_evals)
    sent = send_direct(settings, text, client=telegram_client)
    logger.info(
        "Rapport hebdomadaire : %d SIGNAL, %d NO_BET pressentis sur la période.",
        len(signal_rows), len(nobet_rows),
    )
    return sent
