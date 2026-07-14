"""Constructeurs de relevés simulés pour tester l'analyseur.

Objectif : fabriquer des séquences de relevés déterministes (steam move,
mouvement de ligne, synchro multi-books, divergence…) sans réseau ni quota.
Ces helpers seront réutilisés par les tests des règles (couche B).
"""
from __future__ import annotations

# Instants de relevé lisibles, dans l'ordre chronologique.
T = [
    "2026-01-10T09:00:00Z",  # T[0] = ouverture
    "2026-01-10T12:00:00Z",
    "2026-01-10T15:00:00Z",
    "2026-01-10T18:00:00Z",
    "2026-01-10T20:00:00Z",
]


def snap(bookmaker, market, selection, odds, line=None, snapshot_at=T[0]) -> dict:
    """Construit un relevé unique (même forme qu'une ligne de odds_snapshots)."""
    return {
        "bookmaker": bookmaker,
        "market": market,
        "selection": selection,
        "line": line,
        "odds": odds,
        "snapshot_at": snapshot_at,
    }


def h2h(bookmaker, home, away, home_odds, away_odds, snapshot_at=T[0]) -> list[dict]:
    """Deux relevés h2h (les deux issues) pour un book à un instant."""
    return [
        snap(bookmaker, "h2h", home, home_odds, None, snapshot_at),
        snap(bookmaker, "h2h", away, away_odds, None, snapshot_at),
    ]


def spreads(bookmaker, home, away, home_line, home_odds, away_odds, snapshot_at=T[0]) -> list[dict]:
    """Deux relevés spreads pour un book (la ligne away est l'opposée de la home)."""
    return [
        snap(bookmaker, "spreads", home, home_odds, home_line, snapshot_at),
        snap(bookmaker, "spreads", away, away_odds, -home_line, snapshot_at),
    ]


def totals(bookmaker, line, over_odds, under_odds, snapshot_at=T[0]) -> list[dict]:
    """Deux relevés totals (Over/Under) pour un book à un instant."""
    return [
        snap(bookmaker, "totals", "Over", over_odds, line, snapshot_at),
        snap(bookmaker, "totals", "Under", under_odds, line, snapshot_at),
    ]
