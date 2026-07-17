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
from common.db import DECISION_LOGIC_VERSION  # noqa: F401 — re-export (constante déplacée vers common/db)


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

# Tolérance pour distinguer un mouvement réel du bruit numérique.
_EPS = 1e-9


def _favored_by_spread_move(data: MatchData) -> str | None:
    """Équipe dont la ligne de spread est devenue la plus négative (plus favorite)."""
    best_team, best_drop = None, -_EPS
    for selection in data.selections("spreads"):
        series = data.consensus_series("spreads", selection)
        if len(series) < 2 or series[0].line is None or series[-1].line is None:
            continue
        delta = series[-1].line - series[0].line  # < 0 : l'équipe devient plus favorite
        if delta < best_drop:
            best_drop, best_team = delta, selection
    return best_team


def _favored_by_h2h_move(data: MatchData) -> str | None:
    """Équipe dont la probabilité moneyline a le plus augmenté."""
    best_team, best_delta = None, _EPS
    for selection in data.selections("h2h"):
        series = data.consensus_series("h2h", selection)
        if len(series) < 2:
            continue
        delta = series[-1].prob - series[0].prob
        if delta > best_delta:
            best_delta, best_team = delta, selection
    return best_team


def _current_favorite(data: MatchData) -> str | None:
    """Favori courant : proba h2h la plus haute, à défaut ligne spread la plus négative."""
    times = data.times()
    if not times:
        return None
    latest = times[-1]

    h2h_selections = data.selections("h2h")
    if h2h_selections:
        def h2h_prob(selection: str) -> float:
            point = data.consensus_at("h2h", selection, latest)
            return point.prob if point else 0.0
        return max(h2h_selections, key=h2h_prob)

    spread_selections = data.selections("spreads")
    if spread_selections:
        def spread_line(selection: str) -> float:
            point = data.consensus_at("spreads", selection, latest)
            return point.line if point and point.line is not None else 0.0
        return min(spread_selections, key=spread_line)

    return None


def favored_selection(data: MatchData, driving_market: str = "h2h") -> str | None:
    """Équipe pressentie, cohérente avec le marché qui porte le signal.

    - signal spread (R1/R5) : l'équipe dont la ligne est devenue la plus négative ;
    - signal moneyline (ou repli) : l'équipe dont la proba h2h a le plus monté ;
    - sans mouvement : le favori courant.

    Corrige la dette où la sélection, déduite du seul h2h, pouvait désigner le
    mauvais côté d'un signal porté par le spread.
    """
    if driving_market == "spreads":
        team = _favored_by_spread_move(data)
        if team is not None:
            return team
    team = _favored_by_h2h_move(data)
    if team is not None:
        return team
    return _current_favorite(data)


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


def _reference_quote(data: MatchData, selection: str | None, driving_market: str):
    """Marché, ligne et cote de référence du verdict (base du CLV).

    Le verdict porte sur le marché qui a déclenché le signal (spread ou moneyline).
    On enregistre la ligne et la cote médiane de ce marché pour l'équipe pressentie.
    """
    times = data.times()
    if selection is None or not times:
        return "h2h", None, None

    market = driving_market
    point = data.consensus_at(market, selection, times[-1])
    if point is None and market != "h2h":
        market = "h2h"  # repli si le marché déclencheur n'est pas coté
        point = data.consensus_at("h2h", selection, times[-1])
    if point is None:
        return market, None, None
    return market, point.line, point.odds


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

    driving_market = "spreads" if any(r.rule in ("R1", "R5") for r in triggered) else "h2h"
    selection = favored_selection(data, driving_market)
    market, line, odds = _reference_quote(data, selection, driving_market)
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
