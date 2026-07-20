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
from analyzer.rules import ALERT_RULES, ALL_RULES, parse_state_key
from analyzer.scoring import evaluate_rules, movement_score
from analyzer.verdict import DECISION_LOGIC_VERSION, Verdict, decide
from common import db
from common.logging_config import get_logger

logger = get_logger("analyzer")

# Statuts pour lesquels un match n'a pas encore de verdict (première décision possible).
_DECIDABLE_STATUSES = ("DECOUVERT", "SUIVI")


def _in_decision_window(tipoff_utc: str, now: datetime, config: dict) -> bool:
    """Vrai si l'on est dans la fenêtre de décision avant le tip-off."""
    window = timedelta(hours=config["decision"]["window_hours"])
    tipoff = datetime.fromisoformat(tipoff_utc.replace("Z", "+00:00"))
    return timedelta(0) < (tipoff - now) <= window


def _tipoff_passed(tipoff_utc: str, now: datetime) -> bool:
    """Vrai si le tip-off est passé (now ≥ tipoff). Garde délibérée contre les alertes live."""
    tipoff = datetime.fromisoformat(tipoff_utc.replace("Z", "+00:00"))
    return now >= tipoff


def _too_close_to_decide(tipoff_utc: str, now: datetime, config: dict) -> bool:
    """Vrai si l'on est trop près du tip-off pour qu'une analyse soit actionnable.

    Sous `decision.decision_min_hours`, l'analyseur ne produit RIEN — ni alerte
    (R1-R6, non actionnable à quelques dizaines de minutes du tip-off) ni
    verdict/re-décision. Choix documenté (correctif 2026-07-20, exception au gel) :
    sans cette garde, la collecte de clôture (H-0.4) déclenchait immédiatement une
    re-décision dont `decided_at` tombait à moins d'une seconde du snapshot de
    clôture → CLV structurellement non mesurable (verdict et clôture sur le même
    snapshot, cf. garde de `evaluator/clv.py`). Le snapshot de clôture, lui, est
    toujours stocké par le collecteur : seule l'ANALYSE est sautée ici.
    """
    min_delay = timedelta(hours=config["decision"]["decision_min_hours"])
    tipoff = datetime.fromisoformat(tipoff_utc.replace("Z", "+00:00"))
    return (tipoff - now) <= min_delay


def analyze_match(conn: sqlite3.Connection, match: sqlite3.Row, config: dict, now: datetime) -> dict:
    """Analyse un match : alertes temps réel + verdict si dans la fenêtre de décision.

    Garde tip-off : si le tip-off est passé, on n'émet ni alerte ni verdict/re-décision.
    Cette protection est **délibérée** (bug 17/07 : sans elle, un match resté SUIVI au
    tip-off aurait alerté en live). Le filtre de statut (DECOUVERT/SUIVI) protégeait
    Portland *par coïncidence* (elle était DECIDE) — cette garde rend la protection
    explicite et couvre aussi le chemin de re-décision des matchs DECIDE.
    """
    match_id = match["match_id"]
    if _tipoff_passed(match["tipoff_utc"], now):
        logger.info("Match %s ignoré : tip-off passé, aucune analyse.", match_id)
        return {"alerts": 0, "verdict": None, "score": 0}
    if _too_close_to_decide(match["tipoff_utc"], now, config):
        logger.info(
            "Match %s : analyse sautée (trop proche du tip-off, < decision_min_hours). "
            "Snapshot de clôture stocké, aucune alerte ni verdict/re-décision émis.",
            match_id,
        )
        return {"alerts": 0, "verdict": None, "score": 0}
    data = preprocess(conn, match_id)
    results = evaluate_rules(data, config, ALL_RULES)
    now_iso = now.isoformat()

    alerts = 0
    for result in results:
        if result.triggered and result.rule in ALERT_RULES:
            # Déduplication par changement d'état : ne pas réémettre si l'état
            # (sélection + direction + ampleur) n'a pas changé depuis la dernière alerte.
            detail = result.detail
            if result.state_key:
                last_state = db.get_last_alert_state(conn, match_id, result.rule)
                if last_state == result.state_key:
                    continue  # même état → pas de nouvelle alerte
                # Évolution d'ampleur : injecter "ancien → nouveau" dans le détail.
                if last_state:
                    detail = _inject_evolution(detail, last_state, result.state_key, result.rule)
            db.insert_alert(conn, match_id=match_id, rule=result.rule,
                            details=detail, created_at=now_iso,
                            state_key=result.state_key)
            alerts += 1

    verdict_type = None
    if _in_decision_window(match["tipoff_utc"], now, config):
        if match["status"] in _DECIDABLE_STATUSES:
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
                logic_version=DECISION_LOGIC_VERSION,
            )
            db.update_match_status(conn, match_id, "DECIDE")
            verdict_type = verdict.verdict
            logger.info("Verdict %s pour %s (score %d).", verdict_type, match_id, verdict.signal_score)
        elif match["status"] == "DECIDE":
            verdict_type = _redecide(conn, match_id, decide(data, results, config), now_iso)

    return {"alerts": alerts, "verdict": verdict_type, "score": movement_score(results)}


def _inject_evolution(detail: str, old_key: str, new_key: str, rule: str) -> str:
    """Injecte l'évolution d'ampleur dans le détail d'une alerte ré-émise.

    Compare l'ancien et le nouveau `state_key` : si l'ampleur a changé (ex. R4 :
    8 -> 9 bookmakers), préfixe le détail avec l'évolution. Pour R1/R2, l'évolution
    est déjà visible dans le détail (ligne avant -> après, Delta proba) -- on n'ajoute rien.
    """
    old = parse_state_key(old_key)
    new = parse_state_key(new_key)
    if not old["amplitude"] or not new["amplitude"]:
        return detail
    if old["amplitude"] == new["amplitude"]:
        return detail
    if rule == "R4":
        # R4 : amplitude = nombre de books -> "8 -> 9 bookmakers"
        return detail.replace(
            f"{new['amplitude']} bookmakers dans le même sens",
            f"{old['amplitude']} → {new['amplitude']} bookmakers dans le même sens",
        )
    # R1/R2 : l'évolution est déjà dans le détail (ligne/proba avant -> après).
    return detail


def _redecide(conn: sqlite3.Connection, match_id: str, new: Verdict, now_iso: str) -> str | None:
    """Re-décision à H-1 : met à jour le verdict courant tant que le match est en fenêtre.

    - **Gel** : si une position a déjà été prise sur ce verdict, on n'y touche plus
      (le développeur s'est engagé sur cette décision).
    - **Changement matériel** (le type OU la sélection change) → prix ré-ancré
      (`decided_at`/`odds_at_verdict` mis à jour sur le nouveau prix) ET l'ancien
      message devient obsolète : `supersede_verdict` le marque pour édition et
      re-met le verdict en file.
    - **Changement non matériel** (score/règles/justificatif inchangés dans leur
      substance, seule la cote ou le libellé évoluent) → mise à jour silencieuse du
      score/justificatif SEULEMENT. `decided_at` et `odds_at_verdict` restent figés
      sur la VRAIE décision (correctif 2026-07-20) : les réécrire à chaque passage
      de l'analyseur — y compris au tick de clôture — rendait le CLV structurellement
      non mesurable (verdict et clôture sur le même snapshot, cf. `evaluator/clv.py`).
    """
    current = db.get_current_verdict(conn, match_id)
    if current is None:
        return None
    if db.get_position(conn, current["id"]) is not None:
        return current["verdict"]  # gelé : décision engagée

    material = (new.verdict != current["verdict"]) or (new.selection != current["selection"])

    if material:
        db.update_verdict_fields(
            conn,
            current["id"],
            verdict=new.verdict,
            selection=new.selection,
            market=new.market,
            line=new.line,
            odds_at_verdict=new.odds_at_verdict,
            signal_score=new.signal_score,
            rules_triggered=json.dumps(new.rules_triggered),
            rationale=new.rationale,
            decided_at=now_iso,
            logic_version=DECISION_LOGIC_VERSION,
        )
        db.supersede_verdict(conn, current["id"], current["telegram_message_id"])
        logger.info(
            "Re-décision matérielle du verdict %s (%s) : %s → %s.",
            current["id"], match_id, current["verdict"], new.verdict,
        )
    else:
        db.update_verdict_fields_partial(
            conn,
            current["id"],
            signal_score=new.signal_score,
            rules_triggered=json.dumps(new.rules_triggered),
            rationale=new.rationale,
            logic_version=DECISION_LOGIC_VERSION,
        )
    return new.verdict


def analyze_open_matches(conn: sqlite3.Connection, config: dict, now: datetime | None = None) -> dict:
    """Analyse tous les matchs actifs (DECOUVERT/SUIVI/DECIDE). Committe à la fin.

    Inclut DECIDE : un match déjà décidé est **re-analysé** à chaque collecte tant
    qu'il est dans la fenêtre de décision (re-décision H-1). Sans cela, la branche
    `elif status == "DECIDE"` de `analyze_match` est du code mort en production —
    les tests passaient car ils appelaient `_redecide` directement. Correctif C1
    (revue externe) : la sélection doit couvrir tous les statuts actifs, pas seulement
    les pré-décision.
    """
    now = now or datetime.now(timezone.utc)
    placeholders = ",".join("?" * len(db.ACTIVE_STATUSES))
    rows = conn.execute(
        f"SELECT * FROM matches WHERE status IN ({placeholders})",
        db.ACTIVE_STATUSES,
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
