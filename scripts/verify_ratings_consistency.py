#!/usr/bin/env python3
"""Vérifie que les notes en base valent un rejeu de saison complet (B5 lot 4).

CE QUE LE SCRIPT PROUVE. Les notes vivent désormais de deux mains : le backfill les a
posées une fois, l'évaluateur les entretient chaque matin. Rien ne garantit *a priori*
que ces deux chemins appliquent la même transition à la même population dans le même
ordre — et une divergence ne se verrait pas : des notes fausses restent parfaitement
plausibles à l'inspection.

Ce script **recalcule la référence** au lieu de la relire. Il rejoue la saison entière
depuis zéro, avec le même moteur, le même résolveur de clé et les mêmes paramètres, puis
confronte le résultat à `team_ratings`. L'égalité prouve la cohérence des deux chemins ;
un écart le nomme, équipe par équipe.

Il compare **deux choses**, et la seconde compte autant que la première :
  • les notes (`rating`, `games_played`, `last_game_date`) ;
  • la **population** des matchs intégrés, par identifiant source.
Sans le second contrôle, un filtrage divergent entre ce script et le backfill passerait
pour un accord ; avec lui, il apparaît comme un écart explicite.

CE QU'IL NE PROUVE PAS. Que les notes sont *justes* — seule la confrontation au vrai
classement le dit, et elle reste à l'œil humain (cf. lot 3).

SÛRETÉ. Lecture seule, sans aucune exception : aucune écriture en base, aucun crédit
The Odds API. Hors ligne par défaut (cache disque du lot 3), donc zéro requête réseau.

Usage :
    uv run python scripts/verify_ratings_consistency.py           # depuis le cache
    uv run python scripts/verify_ratings_consistency.py --online  # rafraîchit le cache
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from analyzer.model import params_from_config
from analyzer.ratings import replay, sort_games, unusable_reason
from analyzer.ratings_store import build_resolver, replay_range
from common.config import load_config, load_settings
from common.http_cache import CachingTransport, unthrottled
from common.logging_config import configure_logging
from common.results_api_client import ResultsApiClient, ResultsApiError
from evaluator.reconcile import TeamContractError

EXIT_OK, EXIT_ABORT = 0, 1
WIDTH = 92
TOLERANCE = 1e-9


def _section(title: str) -> None:
    print(f"\n{title}")
    print("─" * WIDTH)


def collect(client, conn, config: dict, sport: str, resolve) -> tuple[list, Counter]:
    """Matchs de la saison retenus pour le rejeu de contrôle, et le décompte des exclusions."""
    start, end = replay_range(conn, config, sport)
    print(f"Plage interrogée      : {start} → {end}")
    games = client.get_games(start, end)
    expected_season = int(config["backfill"]["seasons"][sport]["season"])

    journal: Counter = Counter(raw=len(games))
    by_id: dict[str, object] = {}
    kept = []
    for game in games:
        if game.season is not None and game.season != expected_season:
            journal["wrong_season"] += 1
            continue
        if game.game_id is not None:
            if game.game_id in by_id:
                journal["duplicates"] += 1
                continue
            by_id[game.game_id] = game
        if unusable_reason(game) is not None:
            journal["unusable"] += 1
            continue
        try:
            resolve(game.home_team), resolve(game.away_team)
        except TeamContractError:
            journal["unresolved"] += 1
            continue
        kept.append(game)

    journal["kept"] = len(kept)
    return kept, journal


def compare_population(conn, sport: str, kept: list) -> list[str]:
    """Confronte les identifiants rejoués à ceux réellement intégrés en base."""
    rows = conn.execute(
        "SELECT DISTINCT source_game_id FROM rating_history "
        "WHERE sport = ? AND source_game_id IS NOT NULL",
        (sport,),
    ).fetchall()
    en_base = {row["source_game_id"] for row in rows}
    rejoues = {game.game_id for game in kept if game.game_id is not None}

    ecarts = []
    manquants = sorted(rejoues - en_base)
    intrus = sorted(en_base - rejoues)
    if manquants:
        ecarts.append(
            f"{len(manquants)} match(s) joués mais JAMAIS intégrés aux notes "
            f"(trou d'intégration) : {', '.join(manquants[:10])}"
            + (" …" if len(manquants) > 10 else "")
        )
    if intrus:
        ecarts.append(
            f"{len(intrus)} match(s) intégrés que le rejeu de contrôle ne retient pas "
            f"(filtrage divergent ?) : {', '.join(intrus[:10])}"
            + (" …" if len(intrus) > 10 else "")
        )
    print(f"Matchs rejoués        : {len(rejoues)}")
    print(f"Matchs intégrés (base): {len(en_base)}")
    return ecarts


def compare_ratings(conn, sport: str, states: dict) -> list[str]:
    """Confronte note, compteur et dernière date, équipe par équipe."""
    rows = conn.execute(
        "SELECT team, display_name, rating, games_played, last_game_date "
        "FROM team_ratings WHERE sport = ? ORDER BY rating DESC",
        (sport,),
    ).fetchall()
    en_base = {row["team"]: row for row in rows}

    ecarts = []
    print(f"\n{'équipe':<26}{'note base':>12}{'note rejeu':>13}{'écart':>10}"
          f"{'matchs':>9}{'dernier match':>16}")
    print("─" * WIDTH)
    for key in sorted(set(en_base) | set(states)):
        row, state = en_base.get(key), states.get(key)
        if row is None:
            ecarts.append(f"{key!r} : notée par le rejeu, ABSENTE de la base")
            continue
        if state is None:
            ecarts.append(f"{key!r} : présente en base, jamais notée par le rejeu")
            continue

        delta = row["rating"] - state.rating
        attendu_date = state.last_game_date.isoformat() if state.last_game_date else None
        conforme = (
            abs(delta) <= TOLERANCE
            and row["games_played"] == state.games_played
            and row["last_game_date"] == attendu_date
        )
        marque = " " if conforme else " ⚠"
        print(f"{row['display_name'][:25]:<26}{row['rating']:>12.2f}{state.rating:>13.2f}"
              f"{delta:>+10.4f}{row['games_played']:>6}/{state.games_played:<3}"
              f"{str(row['last_game_date']):>15}{marque}")
        if not conforme:
            ecarts.append(
                f"{row['display_name']!r} : base note={row['rating']:.6f} "
                f"matchs={row['games_played']} dernier={row['last_game_date']} — "
                f"rejeu note={state.rating:.6f} matchs={state.games_played} "
                f"dernier={attendu_date}"
            )
    return ecarts


def run(conn: sqlite3.Connection, config: dict, settings, args) -> int:
    sport = config["api"]["sport"]
    params = params_from_config(config, sport=sport)

    _section(f"Cohérence des notes Elo — {sport}")
    try:
        resolve = build_resolver(conn, config, sport)
    except TeamContractError as exc:
        print(f"Contrat de clé d'équipe rompu : {exc}", file=sys.stderr)
        return EXIT_ABORT

    cache_dir = Path(args.cache_dir or config["backfill"]["cache_dir"])
    transport = CachingTransport(cache_dir, offline=not args.online)
    client = ResultsApiClient.from_config(
        settings, config if args.online else unthrottled(config), transport=transport
    )
    try:
        kept, journal = collect(client, conn, config, sport, resolve)
    except ResultsApiError as exc:
        print(f"Récupération impossible : {exc}", file=sys.stderr)
        return EXIT_ABORT
    finally:
        client.close()

    print(f"Requêtes réseau       : {transport.network_calls}  (cache : {transport.hits})")
    print("Crédits The Odds API  : 0")
    print(f"Matchs bruts / retenus: {journal['raw']} / {journal['kept']}  "
          f"(hors saison {journal['wrong_season']}, doublons {journal['duplicates']}, "
          f"inexploitables {journal['unusable']}, hors franchises {journal['unresolved']})")

    states, applications = replay(sort_games(kept), params, normalize=resolve)

    _section("Population")
    ecarts = compare_population(conn, sport, kept)

    _section("Notes")
    ecarts += compare_ratings(conn, sport, states)

    # Un contrôle qui ne peut pas virer au rouge ne prouve rien : si le rejeu de
    # référence est vide ou plus court que l'historique en base, l'égalité serait
    # obtenue par défaut d'échantillon, pas par cohérence.
    en_base = conn.execute(
        "SELECT COUNT(*) AS n FROM rating_history WHERE sport = ?", (sport,)
    ).fetchone()["n"]
    if not applications:
        ecarts.append("rejeu de contrôle VIDE : la comparaison ne démontrerait rien")
    elif 2 * len(applications) < en_base:
        ecarts.append(
            f"rejeu de contrôle plus court que l'historique en base "
            f"({2 * len(applications)} lignes attendues contre {en_base}) : "
            f"l'échantillon ne peut pas servir de référence"
        )

    _section("Verdict")
    if ecarts:
        for ecart in ecarts:
            print(f"  ⚠ {ecart}")
        print(
            f"\n{len(ecarts)} écart(s). Les deux chemins d'alimentation ont divergé — "
            f"cf. « procédure de réparation après trou » au journal des décisions."
        )
        return EXIT_ABORT

    print(f"  ✓ {len(states)} équipes, {len(applications)} matchs : "
          f"les notes en base valent exactement le rejeu de saison.")
    return EXIT_OK


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--online", action="store_true",
                        help="autorise le réseau (défaut : cache seul, zéro requête)")
    parser.add_argument("--cache-dir", default=None,
                        help="répertoire de cache (défaut : backfill.cache_dir)")
    args = parser.parse_args()

    settings = load_settings()
    configure_logging(settings.log_level)

    db_path = Path(settings.database_path)
    if not db_path.exists():
        print(f"Base introuvable : {db_path}", file=sys.stderr)
        sys.exit(EXIT_ABORT)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        sys.exit(run(conn, load_config(), settings, args))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
