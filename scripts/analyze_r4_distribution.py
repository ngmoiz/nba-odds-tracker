#!/usr/bin/env python3
"""Analyse de la distribution des ampleurs de mouvement par book sur les déclenchements R4.

Script de lecture seule (hors gel) : à lancer au jour 7 pour choisir le plancher
d'ampleur R4 sur des données réelles, pas sur l'intuition du ~1 pt.

Pour chaque alerte R4 en base, recalcule l'ampleur du mouvement de chaque book
qui a contribué à la synchro (ouverture → dernier relevé avant l'alerte). Affiche
la distribution (histogramme par tranches) pour spreads (points de ligne) et
h2h/totals (points de proba).

Usage :
    uv run python scripts/analyze_r4_distribution.py
    DATABASE_PATH=/chemin/vers/base.db uv run python scripts/analyze_r4_distribution.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from common.config import load_settings


def _book_movement(
    conn: sqlite3.Connection,
    match_id: str,
    market: str,
    selection: str,
    bookmaker: str,
    before_at: str,
    after_at: str,
) -> float | None:
    """Ampleur du mouvement d'un book entre deux instants. None si données manquantes.

    Retourne la valeur en unités naturelles : points de ligne (spreads) ou
    points de proba ×100 (h2h/totals), pour comparaison directe avec les seuils.
    """
    rows = conn.execute(
        "SELECT line, odds, snapshot_at FROM odds_snapshots "
        "WHERE match_id = ? AND market = ? AND selection = ? AND bookmaker = ? "
        "  AND snapshot_at <= ? "
        "ORDER BY snapshot_at",
        (match_id, market, selection, bookmaker, after_at),
    ).fetchall()

    if not rows:
        return None

    # Premier relevé à ou avant before_at (l'ouverture du match).
    first = None
    for row in rows:
        if row["snapshot_at"] <= before_at:
            first = row
            break
    if first is None:
        first = rows[0]  # repli : le plus ancien disponible

    last = rows[-1]

    if market == "spreads":
        if first["line"] is None or last["line"] is None:
            return None
        return abs(last["line"] - first["line"])
    else:
        # h2h / totals : proba dé-margée approximée (1/odds normalisé à 2 issues).
        # Le prétraitement exact fait la médiane + dé-marge complète ; ici on
        # approxime avec 1/odds pour un ordre de grandeur (suffisant pour un plancher).
        if first["odds"] is None or last["odds"] is None:
            return None
        p_first = 1.0 / first["odds"]
        p_last = 1.0 / last["odds"]
        return abs(p_last - p_first) * 100  # points de proba


def analyze(conn: sqlite3.Connection) -> None:
    """Analyse et affiche la distribution des ampleurs R4 par book."""
    # Toutes les alertes R4, avec le match et l'instant de l'alerte.
    alerts = conn.execute(
        "SELECT a.match_id, a.created_at, m.tipoff_utc "
        "FROM alerts a JOIN matches m ON m.match_id = a.match_id "
        "WHERE a.rule = 'R4' ORDER BY a.created_at"
    ).fetchall()

    if not alerts:
        print("Aucune alerte R4 en base — rien à analyser.")
        return

    print(f"Alertes R4 trouvées : {len(alerts)}\n")

    # Pour chaque alerte, on récupère les snapshots du match et recalcule
    # l'ampleur par book. On regroupe par marché.
    movements_by_market: dict[str, list[float]] = {"spreads": [], "h2h": [], "totals": []}

    for alert in alerts:
        match_id = alert["match_id"]
        alert_at = alert["created_at"]

        # L'ouverture = premier snapshot du match (tous marchés confondus).
        first_snap = conn.execute(
            "SELECT MIN(snapshot_at) AS t FROM odds_snapshots WHERE match_id = ?", (match_id,)
        ).fetchone()
        if not first_snap or not first_snap["t"]:
            continue
        opening_at = first_snap["t"]

        # Pour chaque marché/sélection, recalcule l'ampleur par book.
        for market in ("spreads", "h2h", "totals"):
            selections = conn.execute(
                "SELECT DISTINCT selection FROM odds_snapshots "
                "WHERE match_id = ? AND market = ?", (match_id, market)
            ).fetchall()
            for sel_row in selections:
                selection = sel_row["selection"]
                bookmakers = conn.execute(
                    "SELECT DISTINCT bookmaker FROM odds_snapshots "
                    "WHERE match_id = ? AND market = ? AND selection = ?",
                    (match_id, market, selection),
                ).fetchall()
                for bk_row in bookmakers:
                    bookmaker = bk_row["bookmaker"]
                    move = _book_movement(
                        conn, match_id, market, selection, bookmaker, opening_at, alert_at
                    )
                    if move is not None:
                        movements_by_market[market].append(move)

    # Affichage : histogramme par tranches pour chaque marché.
    tranches = [
        (0.0, 0.1, "< 0,1"),
        (0.1, 0.25, "0,1–0,25"),
        (0.25, 0.5, "0,25–0,5"),
        (0.5, 1.0, "0,5–1,0"),
        (1.0, 2.0, "1,0–2,0"),
        (2.0, 3.0, "2,0–3,0"),
        (3.0, 5.0, "3,0–5,0"),
        (5.0, float("inf"), "5,0+"),
    ]

    for market in ("spreads", "h2h", "totals"):
        moves = movements_by_market[market]
        if not moves:
            continue
        print(f"\n{'='*60}")
        print(f"Marché : {market} ({len(moves)} mouvements de books)")
        print(f"Médiane : {sorted(moves)[len(moves)//2]:.2f} | "
              f"Min : {min(moves):.2f} | Max : {max(moves):.2f}")
        print(f"{'='*60}")
        print(f"{'Tranche':<12} {'Count':>6} {'%':>6}  Bar")
        print("-" * 50)
        total = len(moves)
        for lo, hi, label in tranches:
            count = sum(1 for m in moves if lo <= m < hi)
            pct = count / total * 100
            bar = "█" * int(pct / 2)
            print(f"{label:<12} {count:>6} {pct:>5.1f}%  {bar}")

    print("\n" + "=" * 60)
    print("Interprétation : le plancher R4 doit couper la tranche du bruit")
    print("d'équilibrage (généralement < 0,5 pt) sans tuer les mouvements réels.")
    print("Choisir le seuil au point d'inflexion de la distribution.")
    print("=" * 60)


def main() -> None:
    settings = load_settings()
    db_path = Path(settings.database_path)
    if not db_path.exists():
        print(f"Base introuvable : {db_path}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        analyze(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()