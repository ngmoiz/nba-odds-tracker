"""Tests de la traduction FR des sélections (style bookmaker français, cf. Betclic).

`traduire_selection` est une fonction **pure** : on la teste directement, sans base
ni réseau. On couvre les quatre marchés, les **deux côtés du spread** et le **cas
limite de la ligne entière** (push / remboursement).
"""
from __future__ import annotations

import sqlite3

from notifier.formatting import (
    _fr_number,
    format_verdict,
    traduire_selection,
)


# ─────────────────────────── Fonction pure ───────────────────────────

def test_fr_number_uses_comma():
    """La virgule décimale remplace le point ; décimales fixes possibles pour les cotes."""
    assert _fr_number(1.91, 2) == "1,91"
    assert _fr_number(1.9, 2) == "1,90"
    assert _fr_number(-4.5) == "-4,5"
    assert _fr_number(-5.0) == "-5"  # entier : pas de décimale parasite


def test_spread_half_favorite():
    """Spread -X,5 côté favori → « gagne de (X+1)+ »."""
    assert traduire_selection("spreads", "Toronto", -1.5) == "Toronto gagne de 2+"
    assert traduire_selection("spreads", "Boston", -4.5) == "Boston gagne de 5+"


def test_spread_half_underdog():
    """Spread +X,5 côté outsider → « ne perd pas ou perd de X max »."""
    assert (
        traduire_selection("spreads", "Washington", 1.5)
        == "Washington ne perd pas ou perd de 1 max"
    )
    assert (
        traduire_selection("spreads", "Miami", 4.5)
        == "Miami ne perd pas ou perd de 4 max"
    )


def test_spread_whole_line_favorite_is_push_refunded():
    """Ligne entière -X,0 côté favori : écart = X remboursé (push)."""
    assert (
        traduire_selection("spreads", "Boston", -5.0)
        == "Boston gagne de 6+ (remboursé si écart = 5)"
    )


def test_spread_whole_line_underdog_is_push_refunded():
    """Ligne entière +X,0 côté outsider : symétrie du favori, avec push à l'écart = X."""
    assert (
        traduire_selection("spreads", "Miami", 5.0)
        == "Miami ne perd pas ou perd de 4 max (remboursé si écart = 5)"
    )


def test_h2h_is_match_winner():
    """h2h → vainqueur sec, prolongations incluses."""
    assert (
        traduire_selection("h2h", "Boston Celtics", None)
        == "Boston Celtics — Vainqueur du match (prolongations incluses)"
    )


def test_totals_over_and_under():
    """totals → « +/- de X,5 points dans le match » avec virgule décimale."""
    assert traduire_selection("totals", "Over", 224.5) == "+ de 224,5 points dans le match"
    assert traduire_selection("totals", "Under", 224.5) == "- de 224,5 points dans le match"


def test_unknown_market_falls_back_to_raw_selection():
    """Marché inconnu ou ligne manquante : on renvoie la sélection brute (repli)."""
    assert traduire_selection("spreads", "Boston", None) == "Boston"
    assert traduire_selection("inconnu", "Boston", -4.5) == "Boston"


# ─────────────────── Intégration dans le message de verdict ───────────────────

def _verdict_row(**over) -> sqlite3.Row:
    """Construit une ligne façon `verdicts` jointe à `matches` (accès par nom)."""
    base = {
        "verdict": "SIGNAL",
        "selection": "Boston Celtics",
        "market": "spreads",
        "line": -5.0,
        "odds_at_verdict": 1.91,
        "rationale": "SIGNAL sur Boston (score 6).",
        "home_team": "Boston Celtics",
        "away_team": "Miami Heat",
        "tipoff_utc": "2026-07-17T00:20:00Z",
    }
    base.update(over)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cols = ", ".join(base)
    ph = ", ".join("?" * len(base))
    conn.execute(f"CREATE TABLE t ({cols})")
    conn.execute(f"INSERT INTO t ({cols}) VALUES ({ph})", tuple(base.values()))
    return conn.execute("SELECT * FROM t").fetchone()


def test_verdict_message_shows_french_main_and_us_secondary():
    """Le message porte la ligne FR (principale) et la ligne US (secondaire) avec la mention médiane."""
    msg = format_verdict(_verdict_row(), tz_name="Europe/Paris")
    # Ligne principale traduite (ligne entière -5,0 → push).
    assert "Boston Celtics gagne de 6+ (remboursé si écart = 5)" in msg
    # Ligne secondaire US + rappel sur la nature de la cote.
    assert "↳ US : Boston Celtics (spreads -5) @ 1,91 (médiane des books US, pas une cote FR)" in msg
