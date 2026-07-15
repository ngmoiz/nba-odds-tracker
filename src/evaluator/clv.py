"""Cote de clôture et CLV (Closing Line Value), section 11.

Le CLV mesure si le verdict a « battu la clôture » : on compare la probabilité
**dé-marginée** du consensus au moment du verdict à celle du **dernier relevé avant
le tip-off** (clôture). Convention (validée) :

    clv = proba_clôture − proba_verdict        (sur la proba dé-marginée du consensus)

Positif → depuis le verdict, le marché s'est déplacé *vers* la sélection (sa
probabilité a monté, sa cote a baissé) : on a pris un meilleur prix que la clôture.

On réutilise le prétraitement de l'analyseur (probas dé-marginées + consensus médian),
donc aucune logique de marge n'est ré-implémentée ici.
"""
from __future__ import annotations

from datetime import datetime

from analyzer.preprocessing import MatchData


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


def compute_clv(
    data: MatchData,
    *,
    market: str,
    selection: str,
    decided_at: str,
    tipoff_utc: str,
) -> tuple[float | None, float | None]:
    """Renvoie (cote_de_clôture, clv).

    - cote de clôture = médiane des cotes brutes du dernier relevé avant tip-off ;
    - clv = proba dé-marginée de clôture − proba dé-marginée au verdict.
    L'une ou l'autre vaut None si le marché/sélection n'est pas coté à l'instant voulu.
    """
    if not selection or not market:
        return None, None

    closing = closing_point(data, market, selection, tipoff_utc)
    opening = verdict_point(data, market, selection, decided_at)

    closing_odds = closing.odds if closing else None
    if closing is None or opening is None:
        return closing_odds, None
    return closing_odds, closing.prob - opening.prob
