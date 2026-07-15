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
from analyzer.scoring import movement_score, triggered_rules


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


def _rationale(
    verdict: str,
    triggered: list[RuleResult],
    selection: str | None,
    score: int,
    flag_r6: bool = False,
) -> str:
    """Compose un justificatif lisible pour Telegram."""
    if not triggered:
        return f"{verdict} — aucune règle déclenchée (score {score})."
    details = " ; ".join(f"{r.rule}: {r.detail}" for r in triggered)
    cible = f" sur {selection}" if selection else ""
    base = f"{verdict}{cible} (score {score}) — {details}."
    if flag_r6:
        base += " ⚠ divergence bookmaker signalée (R6), signal maintenu."
    return base


def _reference_quote(data: MatchData, selection: str | None, triggered: list[RuleResult]):
    """Marché, ligne et cote de référence du verdict (base du CLV).

    Le verdict porte sur le marché qui a déclenché le signal : le **spread** si
    R1/R5 sont en jeu (cas dominant en basket), sinon le **moneyline**. On
    enregistre la ligne et la cote médiane de ce marché pour l'équipe pressentie.
    """
    times = data.times()
    if selection is None or not times:
        return "h2h", None, None

    driving_market = "spreads" if any(r.rule in ("R1", "R5") for r in triggered) else "h2h"
    point = data.consensus_at(driving_market, selection, times[-1])
    if point is None and driving_market != "h2h":
        driving_market = "h2h"  # repli si le marché déclencheur n'est pas coté
        point = data.consensus_at("h2h", selection, times[-1])
    if point is None:
        return driving_market, None, None
    return driving_market, point.line, point.odds


def decide(data: MatchData, results: list[RuleResult], config: dict) -> Verdict:
    """Construit le verdict à partir des résultats de règles (arbitrage option 2).

    - R7 (contradiction spread/moneyline) casse la cohérence → ANOMALIE, toujours.
    - Sinon un score de mouvement ≥ seuil → SIGNAL (une divergence R6 devient un
      simple drapeau, elle ne masque pas un signal fort).
    - Sinon une divergence R6 seule → ANOMALIE (à vérifier).
    - Sinon → NO_BET (défaut).
    """
    threshold = config["decision"]["signal_score_threshold"]
    triggered = triggered_rules(results)
    score = movement_score(results)  # hors points d'anomalie

    r7_fired = any(r.rule == "R7" for r in triggered)
    r6_fired = any(r.rule == "R6" for r in triggered)

    if r7_fired:
        verdict = "ANOMALIE"
    elif score >= threshold:
        verdict = "SIGNAL"
    elif r6_fired:
        verdict = "ANOMALIE"
    else:
        verdict = "NO_BET"

    selection = favored_selection(data)
    market, line, odds = _reference_quote(data, selection, triggered)
    rationale = _rationale(verdict, triggered, selection, score, flag_r6=(verdict == "SIGNAL" and r6_fired))

    return Verdict(
        verdict=verdict,
        selection=selection,
        market=market,
        line=line,
        odds_at_verdict=odds,
        signal_score=score,
        rules_triggered=[r.rule for r in triggered],
        rationale=rationale,
    )
