"""Composition du bilan quotidien (section 7.2 §3).

Fonctions **pures** : à partir des lignes évaluées lors du run, produit le texte
Telegram (français, HTML léger). Le rapport hebdomadaire (§4) viendra ensuite.

Le taux de réussite exclut explicitement les **pushes** du dénominateur :
`taux = won / (won + lost)`. Rappelle le garde-fou des 50–100 évaluations (section 11)
tant que le seuil n'est pas atteint.
"""
from __future__ import annotations

import html
from dataclasses import dataclass

from evaluator.clv_format import clv_label
from evaluator.grading import LOST, PUSH, WON

# En dessous de ce nombre d'évaluations cumulées, les taux sont du bruit (section 11).
_MIN_EVALS_FOR_TRUST = 50

_OUTCOME_LABELS = {WON: "✅ gagné", LOST: "❌ perdu", PUSH: "➖ push"}


@dataclass(frozen=True)
class EvalLine:
    """Une ligne du bilan : le verdict évalué et son issue."""

    home_team: str
    away_team: str
    verdict: str                 # SIGNAL / ANOMALIE / NO_BET
    selection: str | None
    home_score: int
    away_score: int
    outcome: str                 # 'won' / 'lost' / 'push' (état explicite)
    clv: float | None
    position_action: str | None  # 'take' / 'pass' / None (pas de clic)
    clv_unit: str | None = None  # 'prob' (h2h) / 'line' (spreads/totals), cf. clv_format


def success_rate(outcomes: list[str]) -> float | None:
    """Taux de réussite = won / (won + lost). Les pushes sont **hors dénominateur**.

    Renvoie None s'il n'y a aucune issue décisive (que des pushes, ou liste vide).
    """
    decisive = [o for o in outcomes if o in (WON, LOST)]
    if not decisive:
        return None
    return sum(o == WON for o in decisive) / len(decisive)


def _position_label(action: str | None, outcome: str) -> str:
    if action is None:
        return ""
    if action == "take":
        return " — 👉 ta prise : " + _OUTCOME_LABELS[outcome]
    aurait = {WON: "aurait gagné", LOST: "aurait perdu", PUSH: "aurait fait push"}[outcome]
    return f" — 🙅 tu as passé (le pari {aurait})"


def _summary_line(outcomes: list[str]) -> str:
    """Ligne de synthèse : décompte par issue + taux hors push."""
    won = outcomes.count(WON)
    lost = outcomes.count(LOST)
    push = outcomes.count(PUSH)
    rate = success_rate(outcomes)
    taux = "n/d" if rate is None else f"{rate * 100:.0f} %"
    return f"Bilan : {won} gagné(s), {lost} perdu(s), {push} push — taux {taux} (hors push)"


def format_daily_report(day_label: str, lines: list[EvalLine], total_evals: int) -> str:
    """Compose le bilan du matin pour les verdicts évalués."""
    header = f"📊 <b>Bilan du {html.escape(day_label)}</b>"
    if not lines:
        return f"{header}\nAucun verdict à évaluer pour cette date."

    body = []
    for ln in lines:
        match = f"{html.escape(ln.away_team)} @ {html.escape(ln.home_team)}"
        score = f"{ln.away_score}-{ln.home_score}"
        cible = f" [{html.escape(ln.selection)}]" if ln.selection else ""
        
        # Correctif 5 : NO_BET affichés sans ✅/❌, avec texte explicite
        if ln.verdict == "NO_BET":
            if ln.outcome == WON:
                outcome_text = "aurait gagné (occasion manquée)"
            elif ln.outcome == LOST:
                outcome_text = "aurait perdu (abstention justifiée)"
            else:  # PUSH
                outcome_text = "aurait fait push"
        else:
            outcome_text = _OUTCOME_LABELS[ln.outcome]
        
        body.append(
            f"• {match} ({score}) — {html.escape(ln.verdict)}{cible} : "
            f"{outcome_text}, {clv_label(ln.clv, ln.clv_unit)}"
            f"{_position_label(ln.position_action, ln.outcome)}"
        )

    # Correctif 5 : Taux de réussite séparé (SIGNAL/ANOMALIE uniquement)
    signal_lines = [ln for ln in lines if ln.verdict in ("SIGNAL", "ANOMALIE")]
    nobet_lines = [ln for ln in lines if ln.verdict == "NO_BET"]
    
    summary_parts = []
    if signal_lines:
        summary_parts.append(_summary_line([ln.outcome for ln in signal_lines]))
    if nobet_lines:
        won_nobet = sum(1 for ln in nobet_lines if ln.outcome == WON)
        total_nobet = len(nobet_lines)
        summary_parts.append(
            f"NO_BET : {won_nobet}/{total_nobet} auraient gagné (faux négatifs)"
        )
    
    summary = "\n".join(summary_parts) if summary_parts else "Aucune évaluation décisive."
    
    footer = f"{total_evals} évaluations cumulées."
    if total_evals < _MIN_EVALS_FOR_TRUST:
        footer += (
            f" ⚠️ En dessous de {_MIN_EVALS_FOR_TRUST}, les taux restent du bruit "
            "statistique — aucun seuil ne doit être modifié (règle 11)."
        )

    return header + "\n" + "\n".join(body) + "\n\n" + summary + "\n" + footer


def format_degraded_report(day_label: str, pending_count: int, cause: str) -> str:
    """Compose un bilan dégradé quand aucun match n'a pu être évalué.
    
    Mode de panne interdit : un composant qui échoue doit le signaler explicitement.
    Ce rapport est envoyé quand des matchs clos attendent évaluation mais que les
    résultats sont indisponibles (API balldontlie en panne, quota épuisé, etc.).
    """
    header = f"📊 <b>Bilan du {html.escape(day_label)}</b>"
    warning = (
        f"⚠️ <b>0 match évalué</b> — {pending_count} match(s) en attente\n"
        f"Cause : {html.escape(cause)}"
    )
    return f"{header}\n{warning}"
