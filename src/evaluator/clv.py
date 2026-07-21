"""Cote de clôture et CLV (Closing Line Value), section 11.

Principe unificateur (à respecter dans toute évolution de ce module — c'est l'endroit
où une inversion de signe passerait inaperçue, cf. correctif du 2026-07-21) :

    Un CLV positif signifie que le marché a bougé dans le sens du pari après la
    décision — meilleur numéro (ligne) ou meilleure cote (probabilité) que celui
    offert à la clôture.

Deux unités, jamais mélangées :

- **h2h** (pas de ligne) : CLV en **points de probabilité** dé-margée du consensus.
  `clv = proba_clôture − proba_verdict`. Convention inchangée depuis l'origine.
- **spreads** et **totals** (marchés à ligne) : CLV en **points de ligne**. Comparer
  des probabilités entre deux instants où la ligne a bougé compare deux propositions
  différentes (un −5,5 n'est pas un −4,5) — bug confirmé le 2026-07-21 sur un cas réel
  (verdict New York Liberty −5,5 @ 1,95, clôture −4,5 @ 1,87 : l'ancien calcul rendait
  un CLV proba faussement positif +0,0209, alors que le parieur a obtenu le pire
  numéro des deux). Le CLV ligne se calcule directement sur `ConsensusPoint.line`,
  déjà signé pour la sélection pariée (spreads), ou explicitement selon Over/Under
  (totals, où la ligne est partagée entre les deux issues) :

    spreads : clv = ligne_verdict − ligne_clôture
    totals, Over  : clv = ligne_clôture − ligne_verdict
    totals, Under : clv = ligne_verdict − ligne_clôture

On réutilise le prétraitement de l'analyseur (probas dé-marginées + consensus médian),
donc aucune logique de marge n'est ré-implémentée ici.
"""
from __future__ import annotations

from datetime import datetime

from analyzer.preprocessing import MatchData

_LINE_MARKETS = ("spreads", "totals")


def _to_dt(ts: str) -> datetime:
    """Parse un timestamp UTC quel que soit son suffixe ('...Z' ou '...+00:00')."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _consensus_at_or_before(data: MatchData, market: str, selection: str, moment: str):
    """Dernier `ConsensusPoint` d'une issue à un instant ≤ `moment`, ou None.

    Comparaison sur des `datetime` (et non des chaînes) pour gérer sans ambiguïté les
    formats UTC mêlés ('...Z' de l'API et '...+00:00' de Python).
    """
    moment_dt = _to_dt(moment)
    series = data.consensus_series(market, selection)
    chosen = None
    for point in series:  # série triée par instant croissant
        if _to_dt(point.snapshot_at) <= moment_dt:
            chosen = point
        else:
            break
    return chosen


def closing_point(data: MatchData, market: str, selection: str, tipoff_utc: str):
    """Consensus de clôture : dernier relevé de l'issue avant (ou à) le tip-off."""
    return _consensus_at_or_before(data, market, selection, tipoff_utc)


def verdict_point(data: MatchData, market: str, selection: str, decided_at: str):
    """Consensus au moment du verdict : dernier relevé de l'issue avant (ou à) la décision."""
    return _consensus_at_or_before(data, market, selection, decided_at)


def _clv_unit_for(market: str) -> str | None:
    """Unité du CLV pour un marché : propriété du marché, indépendante du succès du calcul."""
    if market == "h2h":
        return "prob"
    if market in _LINE_MARKETS:
        return "line"
    return None


def _line_clv(market: str, selection: str, opening_line: float, closing_line: float) -> float | None:
    """CLV en points de ligne pour spreads/totals. None + warning si les données sont incohérentes."""
    if market == "spreads":
        return opening_line - closing_line
    # totals : la ligne est partagée Over/Under, la faveur est opposée selon le côté.
    if selection == "Over":
        return closing_line - opening_line
    if selection == "Under":
        return opening_line - closing_line
    from common.logging_config import get_logger
    logger = get_logger("clv")
    logger.warning(
        "CLV ligne non calculable : sélection totals inattendue %r (ni 'Over' ni 'Under').",
        selection,
    )
    return None


def compute_clv(
    data: MatchData,
    *,
    market: str,
    selection: str,
    decided_at: str,
    tipoff_utc: str,
) -> tuple[float | None, float | None, str | None]:
    """Renvoie (cote_de_clôture, clv, clv_unit).

    - cote de clôture = médiane des cotes brutes du dernier relevé avant tip-off ;
    - clv_unit ∈ {'prob','line'} (ou None si le marché n'est pas reconnu) : propriété
      du marché, renvoyée même quand `clv` est None (échec de calcul) ;
    - clv : voir le principe unificateur en tête de module. None si le marché/la
      sélection n'est pas coté à l'instant voulu, si verdict et clôture pointent vers
      le même relevé (non mesurable), ou si la ligne manque sur un marché à ligne.
    """
    if not selection or not market:
        return None, None, None

    clv_unit = _clv_unit_for(market)

    closing = closing_point(data, market, selection, tipoff_utc)
    opening = verdict_point(data, market, selection, decided_at)

    closing_odds = closing.odds if closing else None
    if closing is None or opening is None:
        return closing_odds, None, clv_unit

    # Garde-fou : si verdict et clôture pointent vers le même snapshot, le CLV
    # n'est pas mesurable (pas de collecte entre verdict et tip-off) → None.
    # Distinction critique : CLV None (non mesurable) ≠ CLV 0,0 (marché stable).
    if closing.snapshot_at == opening.snapshot_at:
        from common.logging_config import get_logger
        logger = get_logger("clv")
        logger.warning(
            "CLV non mesurable : verdict et clôture sur le même snapshot (%s). "
            "Pas de collecte entre verdict et tip-off.",
            closing.snapshot_at
        )
        return closing_odds, None, clv_unit

    if market == "h2h":
        return closing_odds, closing.prob - opening.prob, clv_unit

    if market in _LINE_MARKETS:
        if opening.line is None or closing.line is None:
            from common.logging_config import get_logger
            logger = get_logger("clv")
            logger.warning(
                "CLV ligne non calculable : ligne manquante sur un marché à ligne "
                "(market=%s, selection=%s, verdict_line=%s, closing_line=%s).",
                market, selection, opening.line, closing.line,
            )
            return closing_odds, None, clv_unit
        return closing_odds, _line_clv(market, selection, opening.line, closing.line), clv_unit

    from common.logging_config import get_logger
    logger = get_logger("clv")
    logger.warning("CLV non calculable : marché inconnu %r.", market)
    return closing_odds, None, clv_unit
