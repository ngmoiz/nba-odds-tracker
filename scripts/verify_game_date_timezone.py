#!/usr/bin/env python3
"""Vérifie le correctif de fuseau des dates balldontlie (B5 lot 1b), en lecture seule.

Confronte la date de match renvoyée par **balldontlie** à la date calendaire déduite
du `tipoff_utc` de **The Odds API**, stocké en base locale. Les deux sources sont
indépendantes : c'est ce qui fait du contrôle une preuve et non une auto-validation.

Deux colonnes sont affichées dans la même exécution :

- **avant** — client construit avec `calendar_timezone="UTC"`. Sur un date-time déjà
  exprimé en UTC, convertir vers UTC est l'identité : cette colonne reproduit donc
  **exactement** l'ancienne troncature `[:10]`, sans qu'une seule ligne de code mort
  n'ait été conservée pour la produire.
- **après** — client construit avec le fuseau réel (`results.calendar_timezone`).

Attendu : un nombre significatif d'écarts d'un jour en colonne « avant » (les matchs
du soir US, ~28 sur 55 au 2026-08-08), et **zéro** en colonne « après ». Un contrôle
qui ne peut pas échouer ne prouve rien : celui-ci échoue visiblement sur le code
d'avant, et la colonne « avant » le démontre à côté de l'autre.

Lecture seule : aucune écriture en base, aucun message Telegram, **zéro crédit The
Odds API**. Coût réseau : ~2 pages par passe, deux passes, soit ~4 requêtes
balldontlie, étranglées par le throttle du lot 1a.

Usage :
    uv run python scripts/verify_game_date_timezone.py
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from common.config import load_config, load_settings
from common.results_api_client import ResultsApiClient
from evaluator.reconcile import find_result, tipoff_calendar_date

# Code de sortie non nul si la colonne « après » n'est pas parfaite : le script est
# une porte avant déploiement, pas un affichage informatif.
EXIT_OK, EXIT_MISMATCH = 0, 1


def _matches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Matchs suivis, du plus ancien au plus récent."""
    return list(conn.execute(
        "SELECT match_id, home_team, away_team, tipoff_utc FROM matches ORDER BY tipoff_utc"
    ))


def _fetch(config: dict, settings, timezone_name: str, start: str, end: str):
    """Récupère les matchs balldontlie avec un fuseau de calendrier donné."""
    results = dict(config["results"], calendar_timezone=timezone_name)
    client = ResultsApiClient.from_config(settings, dict(config, results=results))
    try:
        return client.get_games(start, end)
    finally:
        client.close()


def verify(conn: sqlite3.Connection, config: dict, settings) -> int:
    calendar_tz = config["results"]["calendar_timezone"]
    matches = _matches(conn)
    if not matches:
        print("Aucun match en base : rien à vérifier.", file=sys.stderr)
        return EXIT_MISMATCH

    # Plage élargie d'un jour de chaque côté : un match du soir US porte la date du
    # lendemain côté balldontlie, un match tôt peut porter celle de la veille.
    start = (date.fromisoformat(matches[0]["tipoff_utc"][:10]) - timedelta(days=1)).isoformat()
    end = (date.fromisoformat(matches[-1]["tipoff_utc"][:10]) + timedelta(days=1)).isoformat()

    print(f"Matchs en base : {len(matches)}  |  plage interrogée : {start} → {end}")
    print(f"Fuseau du calendrier de la ligue : {calendar_tz}\n")

    print("Passe 1/2 — comportement AVANT correctif (calendar_timezone=UTC)…")
    before = _fetch(config, settings, "UTC", start, end)
    print("Passe 2/2 — comportement APRÈS correctif…")
    after = _fetch(config, settings, calendar_tz, start, end)

    rows, gaps_before, gaps_after = [], 0, 0
    unplayed, missing = [], []
    now = datetime.now(timezone.utc)
    for match in matches:
        expected = tipoff_calendar_date(match["tipoff_utc"], calendar_tz)
        found_after = find_result(
            after, home_team=match["home_team"], away_team=match["away_team"],
            tipoff_utc=match["tipoff_utc"], calendar_tz=calendar_tz,
        )
        found_before = find_result(
            before, home_team=match["home_team"], away_team=match["away_team"],
            tipoff_utc=match["tipoff_utc"], calendar_tz=calendar_tz,
        )
        if found_after is None:
            # Distinguer les deux causes : un match dont le tip-off n'est pas encore
            # passé est absent par construction (`get_games` ne rend que les matchs
            # terminés), ce n'est pas une anomalie. Les confondre ferait crier la
            # porte à chaque exécution en pleine saison, donc la désensibiliserait.
            tipoff = datetime.fromisoformat(match["tipoff_utc"].replace("Z", "+00:00"))
            (unplayed if tipoff > now else missing).append(match)
            continue

        date_before = found_before.game_date if found_before else "—"
        date_after = found_after.game_date
        gap_before = (
            abs((date.fromisoformat(date_before) - expected).days) if found_before else None
        )
        gap_after = abs((date.fromisoformat(date_after) - expected).days)
        gaps_before += 1 if gap_before else 0
        gaps_after += 1 if gap_after else 0
        rows.append((match, expected, date_before, date_after, gap_before, gap_after))

    _print_table(rows)
    return _print_summary(rows, gaps_before, gaps_after, unplayed, missing)


def _print_table(rows) -> None:
    print(f"\n{'Match':<44} {'tip-off (US)':<13} {'avant':<12} {'après':<12} écart")
    print("─" * 96)
    for match, expected, date_before, date_after, gap_before, gap_after in rows:
        if not gap_before and not gap_after:
            continue                                   # déjà juste avant : sans intérêt
        label = f"{match['away_team']} @ {match['home_team']}"[:43]
        flag = "✗" if gap_after else "✓"
        print(f"{label:<44} {expected.isoformat():<13} "
              f"{date_before + (' (+%d)' % gap_before if gap_before else '     '):<12} "
              f"{date_after:<12} {flag}")


def _print_summary(rows, gaps_before: int, gaps_after: int,
                   unplayed: list, missing: list) -> int:
    print("\n" + "═" * 96)
    print(f"Matchs appariés              : {len(rows)}")
    print(f"Écarts AVANT correctif       : {gaps_before}")
    print(f"Écarts APRÈS correctif       : {gaps_after}")
    print(f"Non joués (tip-off à venir)  : {len(unplayed)}   — attendu, pas une anomalie")
    for match in unplayed:
        print(f"    · {match['away_team']} @ {match['home_team']} ({match['tipoff_utc']})")
    print(f"Introuvables (match joué)    : {len(missing)}")
    for match in missing:
        print(f"    · {match['away_team']} @ {match['home_team']} "
              f"({match['tipoff_utc']}) — joué mais absent côté balldontlie")
    print("═" * 96)

    if gaps_after:
        print("\n✗ ÉCHEC : la colonne « après » doit être à zéro écart. Ne pas déployer.")
        return EXIT_MISMATCH
    if missing:
        print("\n✗ ÉCHEC : des matchs joués restent introuvables côté balldontlie. "
              "Un nom orphelin donnerait un rating introuvable au backfill. Diagnostiquer.")
        return EXIT_MISMATCH
    if not gaps_before:
        print("\n⚠ Aucun écart AVANT correctif : le contrôle ne discrimine rien sur ces "
              "données (aucun match du soir US ?). La preuve n'est pas concluante.")
        return EXIT_MISMATCH
    print(f"\n✓ Correctif prouvé : {gaps_before} date(s) fausse(s) avant, 0 après, "
          f"aucun match joué introuvable.")
    return EXIT_OK


def main() -> None:
    settings = load_settings()
    db_path = Path(settings.database_path)
    if not db_path.exists():
        print(f"Base introuvable : {db_path}", file=sys.stderr)
        sys.exit(EXIT_MISMATCH)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        sys.exit(verify(conn, load_config(), settings))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
