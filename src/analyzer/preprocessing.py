"""Prétraitement des relevés pour l'analyseur (section 6.1).

Transforme les `odds_snapshots` bruts d'un match en données exploitables par le
moteur de règles :
- **probabilité implicite dé-margée** par cote : p = 1/cote, puis normalisation
  par (bookmaker, marché, instant) pour que la somme des issues = 100 % ;
- **consensus** par (marché, sélection, instant) = médiane des books (robuste aux
  cotes aberrantes ou périmées) ;
- repérage du **relevé d'ouverture** (référence de tous les mouvements).

Le module est « pur » : `preprocess_rows` travaille sur une simple liste de
relevés, ce qui permet de tout tester sur des données simulées, sans base ni API.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from statistics import median

from common.logging_config import get_logger

logger = get_logger("analyzer.preprocessing")


@dataclass(frozen=True)
class Quote:
    """Une cote dé-margée : un relevé enrichi de sa probabilité implicite."""

    bookmaker: str
    market: str
    selection: str
    line: float | None
    odds: float
    prob: float          # probabilité implicite dé-margée (0..1)
    snapshot_at: str


@dataclass(frozen=True)
class ConsensusPoint:
    """Consensus (médiane des books) pour une issue à un instant donné."""

    market: str
    selection: str
    snapshot_at: str
    line: float | None   # médiane des lignes (None pour h2h)
    prob: float          # médiane des probabilités dé-margées
    odds: float          # médiane des cotes brutes (pour odds_at_verdict / CLV)
    n_books: int         # nombre de books ayant coté cette issue à cet instant


@dataclass
class MatchData:
    """Vue prétraitée d'un match : cotes dé-margées + séries de consensus."""

    match_id: str
    quotes: list[Quote]
    consensus: list[ConsensusPoint]

    def times(self) -> list[str]:
        """Instants de relevé distincts, triés chronologiquement."""
        return sorted({q.snapshot_at for q in self.quotes})

    def opening_time(self) -> str | None:
        """Instant du relevé d'ouverture (le plus ancien), ou None si vide."""
        times = self.times()
        return times[0] if times else None

    def consensus_series(self, market: str, selection: str) -> list[ConsensusPoint]:
        """Série temporelle du consensus pour une issue donnée, triée par instant."""
        points = [
            c for c in self.consensus if c.market == market and c.selection == selection
        ]
        return sorted(points, key=lambda c: c.snapshot_at)

    def consensus_at(self, market: str, selection: str, snapshot_at: str) -> ConsensusPoint | None:
        """Consensus d'une issue à un instant précis, ou None s'il n'existe pas."""
        for c in self.consensus:
            if c.market == market and c.selection == selection and c.snapshot_at == snapshot_at:
                return c
        return None

    def markets(self) -> list[str]:
        """Marchés présents dans les relevés (h2h, spreads, totals)."""
        return sorted({q.market for q in self.quotes})

    def selections(self, market: str) -> list[str]:
        """Issues distinctes d'un marché (équipes, ou Over/Under)."""
        return sorted({q.selection for q in self.quotes if q.market == market})

    def bookmakers(self, market: str, selection: str) -> list[str]:
        """Bookmakers ayant coté une issue donnée."""
        return sorted(
            {q.bookmaker for q in self.quotes if q.market == market and q.selection == selection}
        )

    def book_series(self, market: str, selection: str, bookmaker: str) -> list[Quote]:
        """Série temporelle d'un book pour une issue, triée par instant."""
        quotes = [
            q
            for q in self.quotes
            if q.market == market and q.selection == selection and q.bookmaker == bookmaker
        ]
        return sorted(quotes, key=lambda q: q.snapshot_at)


def _demargin(rows) -> list[Quote]:
    """Calcule la probabilité dé-margée de chaque relevé.

    Normalisation par (bookmaker, marché, instant) : au sein d'un marché à deux
    issues chez un book à un instant, on retire la marge en divisant chaque
    probabilité brute (1/cote) par la somme des probabilités brutes du groupe.
    """
    # Regroupe les relevés par (book, marché, instant) pour normaliser ensemble.
    groups: dict[tuple[str, str, str], list] = defaultdict(list)
    for row in rows:
        groups[(row["bookmaker"], row["market"], row["snapshot_at"])].append(row)

    quotes: list[Quote] = []
    for group in groups.values():
        raw = [1.0 / row["odds"] for row in group]
        total = sum(raw)  # ~1.05 sur un 2-way : c'est la marge du book
        for row, p_raw in zip(group, raw):
            quotes.append(
                Quote(
                    bookmaker=row["bookmaker"],
                    market=row["market"],
                    selection=row["selection"],
                    line=row["line"],
                    odds=row["odds"],
                    prob=p_raw / total if total else 0.0,
                    snapshot_at=row["snapshot_at"],
                )
            )
    return quotes


def _build_consensus(quotes: list[Quote]) -> list[ConsensusPoint]:
    """Agrège les books en un consensus médian par (marché, sélection, instant)."""
    groups: dict[tuple[str, str, str], list[Quote]] = defaultdict(list)
    for q in quotes:
        groups[(q.market, q.selection, q.snapshot_at)].append(q)

    consensus: list[ConsensusPoint] = []
    for (market, selection, snapshot_at), group in groups.items():
        lines = [q.line for q in group if q.line is not None]
        consensus.append(
            ConsensusPoint(
                market=market,
                selection=selection,
                snapshot_at=snapshot_at,
                line=median(lines) if lines else None,
                prob=median(q.prob for q in group),
                odds=median(q.odds for q in group),
                n_books=len(group),
            )
        )
    return consensus


def preprocess_rows(match_id: str, rows) -> MatchData:
    """Prétraite une liste de relevés (dicts ou lignes SQLite) en `MatchData`.

    Chaque relevé doit exposer les clés : bookmaker, market, selection, line,
    odds, snapshot_at (accès par indexation `row["..."]`).
    """
    rows = list(rows)
    quotes = _demargin(rows)
    consensus = _build_consensus(quotes)
    return MatchData(match_id=match_id, quotes=quotes, consensus=consensus)


def preprocess(conn, match_id: str) -> MatchData:
    """Charge les relevés d'un match depuis la base et les prétraite."""
    rows = conn.execute(
        "SELECT bookmaker, market, selection, line, odds, snapshot_at "
        "FROM odds_snapshots WHERE match_id = ? ORDER BY snapshot_at",
        (match_id,),
    ).fetchall()
    logger.info("Prétraitement du match %s : %d relevés chargés.", match_id, len(rows))
    return preprocess_rows(match_id, rows)
