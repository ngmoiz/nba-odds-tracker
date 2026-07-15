"""Construction du verdict final (section 6.4).

À partir des règles évaluées et du score de signal, produit un verdict :
- **ANOMALIE** si une règle d'anomalie (R6/R7) se déclenche (incohérence de marché
  à vérifier manuellement — la cohérence globale n'est pas acquise) ;
- **SIGNAL** si le score ≥ seuil et aucune anomalie ;
- **NO_BET** sinon (défaut). On stocke quand même la sélection « pressentie » pour
  pouvoir évaluer les faux négatifs.
"""
from __future__ import annotations

from dataclasses import dataclass

from analyzer.preprocessing import MatchData
from analyzer.rules import RuleResult
from analyzer.scoring import signal_score, triggered_rules


@dataclass(frozen=True)
class Verdict:
    verdict: str                 # NO_BET / SIGNAL / ANOMALIE
    selection: str | None        # équipe pressentie (favori du mouvement)
    market: str | None           # marché de référence du verdict (h2h en V1)
    line: float | None
    odds_at_verdict: float | None
    signal_score: int
    rules_triggered: list[str]
    rationale: str


def favored_selection(data: MatchData) -> str | None:
    """Équipe vers laquelle le marché a le plus bougé (proba moneyline en hausse).

    À défaut de mouvement, on retient le favori courant (proba la plus haute).
    """
    selections = data.selections("h2h")
    if not selections:
        return None
    times = data.times()
    latest = times[-1] if times else None

    def rank(selection: str) -> tuple[float, float]:
        series = data.consensus_series("h2h", selection)
        delta = series[-1].prob - series[0].prob if series else 0.0
        current = data.consensus_at("h2h", selection, latest) if latest else None
        return (delta, current.prob if current else 0.0)

    return max(selections, key=rank)


def _rationale(verdict: str, triggered: list[RuleResult], selection: str | None, score: int) -> str:
    """Compose un justificatif lisible pour Telegram."""
    if not triggered:
        return f"{verdict} — aucune règle déclenchée (score {score})."
    details = " ; ".join(f"{r.rule}: {r.detail}" for r in triggered)
    cible = f" sur {selection}" if selection else ""
    return f"{verdict}{cible} (score {score}) — {details}."


def decide(data: MatchData, results: list[RuleResult], config: dict) -> Verdict:
    """Construit le verdict à partir des résultats de règles."""
    score = signal_score(results)
    threshold = config["decision"]["signal_score_threshold"]
    triggered = triggered_rules(results)
    has_anomaly = any(r.orientation == "anomaly" for r in triggered)

    if has_anomaly:
        verdict = "ANOMALIE"
    elif score >= threshold:
        verdict = "SIGNAL"
    else:
        verdict = "NO_BET"

    # Sélection pressentie + cote de référence (moneyline en V1).
    selection = favored_selection(data)
    odds = None
    times = data.times()
    if selection is not None and times:
        consensus = data.consensus_at("h2h", selection, times[-1])
        odds = consensus.odds if consensus else None

    return Verdict(
        verdict=verdict,
        selection=selection,
        market="h2h",
        line=None,
        odds_at_verdict=odds,
        signal_score=score,
        rules_triggered=[r.rule for r in triggered],
        rationale=_rationale(verdict, triggered, selection, score),
    )
