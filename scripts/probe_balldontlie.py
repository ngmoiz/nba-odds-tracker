#!/usr/bin/env python3
"""Sonde le format réel de l'API balldontlie pour la ligue configurée, en lecture seule.

Porte d'entrée du backfill Elo (B5 lot 3). Le téléchargement de la saison complète ne
part **qu'après** lecture de cette sortie : il vaut mieux casser une hypothèse en trois
requêtes qu'à mi-implémentation.

CE QU'ELLE VÉRIFIE, ET POURQUOI CHAQUE POINT COMPTE.
  1. Le chemin routé par `results.games_paths[api.sport]` répond bien (HTTP 200).
  2. Convention de scores : la WNBA expose `home_score`/`away_score`, la NBA
     `home_team_score`/`visitor_team_score`. C'est la divergence qui a produit le bug
     du 2026-08-07 — un évaluateur mort à la bascule d'octobre.
  3. Format de date : la WNBA renvoie un date-**time** UTC. La date brute et la date
     convertie par la fonction de production sont affichées côte à côte ; c'est la
     prémisse du correctif de fuseau du lot 1b.
  4. Vocabulaire des statuts, confronté à la liste BLANCHE `_FINAL_STATUSES`. `pre`
     n'avait été anticipé par personne avant d'apparaître en production le 2026-08-08.
  5. Présence de `id` : c'est la clé d'idempotence du rejeu. SQLite considère chaque
     NULL comme distinct dans un index UNIQUE — sans `id`, l'index
     `(sport, team, source_game_id)` ne protégerait RIEN du double comptage.
  6. Valeur de `season` pour la saison visée, avant de s'en servir comme filtre.
  7. Pagination : le paramètre `cursor` est-il honoré, et `meta.next_cursor` est-il
     non nul sur une page terminale (anomalie observée en session 2) ?
  8. Appariement des noms d'équipes balldontlie ↔ The Odds API (table `matches`).
     LE POINT LE PLUS IMPORTANT POUR LA SUITE : le read path lira les notes par nom
     Odds API via `get_team_rating`, qui fait une égalité STRICTE, alors que
     `find_result` tolère l'inclusion. Un seul nom divergent = note introuvable.
  9. Profondeur d'historique servie par le tier gratuit. C'est l'hypothèse la plus
     risquée du lot : si l'API ne sert que les jours récents, le backfill est
     impossible et tout le reste du plan tombe.

SÛRETÉ. Lecture seule : aucune écriture en base, aucun message Telegram, et **zéro
crédit The Odds API** (balldontlie est une source distincte et gratuite). Les réponses
sont mises en cache disque : elles seront réutilisées gratuitement par le backfill.

COÛT RÉSEAU. 3 requêtes brutes, plus les pages de pagination des deux passes de
contrôle (≤ 5 au total selon le comportement du curseur). Le compte réellement émis
est imprimé en fin de rapport — il est mesuré, pas promis.

Usage :
    uv run python scripts/probe_balldontlie.py
    uv run python scripts/probe_balldontlie.py --offline   # rejoue depuis le cache seul
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date
from pathlib import Path

from common.config import load_config, load_settings
from common.http_cache import CacheMissOffline, CachingTransport
from common.logging_config import configure_logging
from common.results_api_client import (
    _FINAL_STATUSES,
    _game_date,
    ResultsApiClient,
    ResultsApiError,
)
from evaluator.reconcile import (
    TeamContractError,
    build_canonical_resolver,
    canonical_team_key,
    check_team_contract,
    normalize_team,
    team_aliases,
)

# Porte, pas affichage : un format inattendu doit arrêter la chaîne.
EXIT_OK, EXIT_MISMATCH = 0, 1

# Journée de référence : le journal du 2026-08-08 y atteste des matchs terminés
# (Toronto Tempo @ Golden State Valkyries), dont un tip-off de soirée.
DEFAULT_PROBE_DAY = "2026-08-05"
# Mois de début de saison : sert à mesurer la profondeur d'historique servie.
DEFAULT_HISTORY_START = "2026-05-01"
DEFAULT_HISTORY_END = "2026-05-31"

WIDTH = 96


class Report:
    """Accumule les constats et retient le pire d'entre eux.

    Un contrôle qui ne peut pas échouer ne prouve rien : `fail` fait sortir le script
    en erreur, `warn` signale sans bloquer, et la distinction entre les deux est le
    seul vrai contenu de ce script.
    """

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.warnings: list[str] = []

    def ok(self, label: str, detail: str = "") -> None:
        print(f"  ✓ {label}{('  — ' + detail) if detail else ''}")

    def warn(self, label: str, detail: str = "") -> None:
        self.warnings.append(label)
        print(f"  ⚠ {label}{('  — ' + detail) if detail else ''}")

    def fail(self, label: str, detail: str = "") -> None:
        self.failures.append(label)
        print(f"  ✗ {label}{('  — ' + detail) if detail else ''}")

    def exit_code(self) -> int:
        return EXIT_MISMATCH if self.failures else EXIT_OK


def _section(title: str) -> None:
    print(f"\n{title}")
    print("─" * WIDTH)


def _raw_games(payload: dict) -> list[dict]:
    return payload.get("data") or []


# ───────────────────────────── contrôles ─────────────────────────────


def check_scores_and_dates(report: Report, games: list[dict], calendar_tz: str) -> None:
    """Points 2 et 3 : convention de scores, et format de date + conversion."""
    _section("2. Convention de scores")
    finals = [g for g in games if str(g.get("status", "")).strip().lower() in _FINAL_STATUSES]
    if not finals:
        report.fail("Aucun match terminé dans l'échantillon", "impossible de lire une convention")
        return

    sample = finals[0]
    wnba_keys = {"home_score", "away_score"} <= sample.keys()
    nba_keys = {"home_team_score", "visitor_team_score"} <= sample.keys()
    print(f"  Clés reçues : {sorted(sample.keys())}")
    if wnba_keys or nba_keys:
        report.ok(
            "Convention de scores reconnue",
            "home_score/away_score (WNBA)" if wnba_keys else
            "home_team_score/visitor_team_score (NBA)",
        )
    else:
        report.fail("Aucune convention de scores connue", "le parsing de production échouerait")

    missing_scores = [g for g in finals if all(g.get(k) is None for k in
                                               ("home_score", "home_team_score"))]
    if missing_scores:
        report.fail(f"{len(missing_scores)} match(s) terminé(s) sans score",
                    "donnée incohérente côté source")

    _section("3. Format de date et conversion de fuseau")
    print(f"  {'date brute (source)':<32} {'date convertie':<14} {'fuseau : ' + calendar_tz}")
    shifted = 0
    for game in finals[:8]:
        raw = str(game["date"])
        converted = _game_date(raw, calendar_tz)
        moved = len(raw) > 10 and converted != raw[:10]
        shifted += 1 if moved else 0
        print(f"  {raw:<32} {converted:<14} {'← décalé d’un jour' if moved else ''}")

    has_time = any(len(str(g['date'])) > 10 for g in finals)
    if has_time:
        report.ok("La date porte une composante horaire",
                  "la conversion de fuseau est bien sur le chemin (correctif lot 1b)")
        if shifted:
            report.ok(f"{shifted} match(s) de soirée décalé(s) par la conversion",
                      "c'est exactement le cas que le correctif traite")
        else:
            report.warn("Aucun match de soirée dans l'échantillon",
                        "la conversion n'est pas discriminée ici")
    else:
        report.ok("Date déjà calendaire (schéma NBA)", "aucune conversion nécessaire")


def check_fields(report: Report, games: list[dict], expected_season: int) -> None:
    """Points 5 et 6 : champs du lot 1a, et valeur de `season`."""
    _section("5. Champs requis par le rejeu (id / season / postseason)")
    finals = [g for g in games if str(g.get("status", "")).strip().lower() in _FINAL_STATUSES]

    without_id = [g for g in finals if g.get("id") is None]
    if without_id:
        report.fail(f"{len(without_id)} match(s) sans `id`",
                    "l'index unique ne protège pas du double comptage (NULL distincts en SQLite)")
    else:
        report.ok("`id` présent sur tous les matchs terminés", "clé d'idempotence du rejeu")

    seasons = sorted({g.get("season") for g in finals if g.get("season") is not None})
    postseasons = sorted({g.get("postseason") for g in finals if g.get("postseason") is not None})
    print(f"  season observées     : {seasons or '—'}")
    print(f"  postseason observées : {postseasons or '—'}")

    if not seasons:
        report.warn("`season` absent", "le filtre de saison du backfill sera inopérant")
    elif expected_season in seasons:
        report.ok(f"Saison attendue {expected_season} observée", "le filtre de saison est valide")
    else:
        report.fail(f"Saison attendue {expected_season} absente",
                    f"observé : {seasons} — corriger backfill.seasons dans config.yaml")

    if not postseasons:
        report.warn("`postseason` absent", "impossible de distinguer les playoffs")


def check_statuses(report: Report, games: list[dict]) -> None:
    """Point 4 : inventaire des statuts, confronté à la liste blanche."""
    _section("4. Vocabulaire des statuts")
    counts: dict[str, int] = {}
    for game in games:
        counts[str(game.get("status", "(absent)"))] = counts.get(
            str(game.get("status", "(absent)")), 0) + 1

    for status, count in sorted(counts.items(), key=lambda item: -item[1]):
        final = status.strip().lower() in _FINAL_STATUSES
        print(f"  {status:<24} ×{count:<5} {'terminé' if final else 'non terminé (ignoré)'}")

    finals_seen = [s for s in counts if s.strip().lower() in _FINAL_STATUSES]
    if finals_seen:
        report.ok(f"Statut terminal observé : {finals_seen}",
                  f"couvert par la liste blanche {list(_FINAL_STATUSES)}")
    else:
        report.fail("Aucun statut terminal observé",
                    f"la liste blanche {list(_FINAL_STATUSES)} ne couvre pas ces données")

    unknown = [s for s in counts if s.strip().lower() not in _FINAL_STATUSES]
    if unknown:
        report.warn(f"Statuts non terminaux rencontrés : {unknown}",
                    "ignorés par conception — listés pour figer le vocabulaire")


def check_pagination(report: Report, single_page: dict) -> None:
    """Point 7 : le paramètre `cursor` est-il honoré, le curseur ment-il sur la fin ?"""
    _section("7. Pagination (per_page=1)")
    data = _raw_games(single_page)
    meta = single_page.get("meta") or {}
    print(f"  matchs renvoyés : {len(data)}")
    print(f"  meta            : {meta}")

    if len(data) == 1:
        report.ok("`per_page` honoré", "1 match demandé, 1 rendu")
    else:
        report.fail(f"`per_page=1` a rendu {len(data)} match(s)",
                    "les paramètres de pagination ne sont pas honorés comme supposé")

    cursor = meta.get("next_cursor")
    if cursor:
        report.ok("Curseur non nul sur une page d'un élément",
                  "confirme l'anomalie de session 2 — les gardes de `get_games` sont nécessaires")
    else:
        report.warn("Curseur nul ici", "l'anomalie de session 2 ne se reproduit pas sur ce cas")


def check_team_names(report: Report, games: list[dict], conn: sqlite3.Connection | None,
                     aliases: dict[str, str]) -> None:
    """Point 8 : le contrat de clé d'équipe, vérifié **dans les deux sens**.

    Ce n'est pas un contrôle des deux cas connus (Fire/Tempo) mais de l'inventaire
    complet : à la bascule NBA, d'autres divergences de `city` apparaîtront et le même
    filet doit les attraper sans qu'on y pense.
    """
    _section("8. Contrat de clé d'équipe (balldontlie → The Odds API)")
    source_names: dict[str, str] = {}
    for game in games:
        for side in ("home_team", "visitor_team"):
            full = (game.get(side) or {}).get("full_name")
            if full:
                source_names[normalize_team(full)] = full

    if conn is None:
        report.fail("Base introuvable", "le contrat de clé d'équipe n'est pas vérifiable")
        return

    rows = conn.execute(
        "SELECT DISTINCT home_team AS team FROM matches "
        "UNION SELECT DISTINCT away_team FROM matches ORDER BY team"
    ).fetchall()
    known_teams = [row["team"] for row in rows]

    print(f"  Noms distincts vus côté balldontlie : {len(source_names)}")
    print(f"  Équipes suivies en base             : {len(known_teams)}")
    print(f"  Alias déclarés                      : {len(aliases)}\n")
    print(f"  {'The Odds API (clé canonique)':<32} {'balldontlie (source)':<28} via alias")

    resolved: dict[str, str] = {}
    for key, name in source_names.items():
        resolved.setdefault(canonical_team_key(name, aliases), name)

    for team in known_teams:
        key = normalize_team(team)
        source = resolved.get(key)
        aliased = source is not None and normalize_team(source) != key
        marker = "✓ alias" if aliased else ("" if source else "✗ jamais produite")
        print(f"  {team:<32} {(source or '—'):<28} {marker}")

    contract = check_team_contract(
        source_names=list(source_names.values()),
        aliases=aliases,
        known_teams=known_teams,
    )
    for warning in contract.warnings:
        report.warn("Contrat de clé d'équipe", warning)
    for error in contract.errors:
        report.fail("Contrat de clé d'équipe", error)

    if contract.ok:
        report.ok(
            f"Contrat vérifié dans les deux sens sur {len(known_teams)} équipes",
            "aucun nom orphelin, aucune équipe jamais produite, aucun alias mort",
        )

    # La résolution doit aussi tenir au point d'écriture, pas seulement en inventaire :
    # c'est cette fonction exacte que le rejeu recevra.
    try:
        resolve = build_canonical_resolver(aliases=aliases, known_teams=known_teams)
        for name in source_names.values():
            resolve(name)
    except TeamContractError as exc:
        report.fail("Résolveur de clé rejeté à la construction ou à l'appel", str(exc))
    else:
        report.ok("Le résolveur de clé accepte tous les noms observés",
                  "c'est la fonction que `replay()` recevra telle quelle")


def check_history_depth(report: Report, count: int, start: str, end: str) -> None:
    """Point 9 : le tier gratuit sert-il l'historique profond ?"""
    _section("9. Profondeur d'historique servie par le tier gratuit")
    print(f"  Matchs terminés retournés entre {start} et {end} : {count}")
    if count > 0:
        report.ok("L'historique de début de saison est servi", "le backfill est réalisable")
    else:
        report.fail(
            "Aucun match retourné sur un mois de début de saison",
            "soit la saison n'avait pas commencé, soit le tier limite la profondeur — "
            "dans les deux cas le backfill ne peut pas partir en l'état",
        )


# ───────────────────────────── exécution ─────────────────────────────


def probe(conn: sqlite3.Connection | None, config: dict, settings, args) -> int:
    sport = config["api"]["sport"]
    calendar_tz = config["results"]["calendar_timezone"]
    backfill = config["backfill"]
    season_config = backfill["seasons"][sport]
    cache_dir = Path(args.cache_dir or backfill["cache_dir"])

    print("═" * WIDTH)
    print(f"SONDE balldontlie — {sport}")
    print("═" * WIDTH)
    print(f"  chemin      : {config['results']['games_paths'][sport]}")
    print(f"  fuseau      : {calendar_tz}")
    print(f"  saison visée: {season_config['season']}")
    print(f"  cache       : {cache_dir}{'  (mode HORS-LIGNE)' if args.offline else ''}")

    transport = CachingTransport(cache_dir, offline=args.offline)
    client = ResultsApiClient.from_config(settings, config, transport=transport)
    report = Report()

    try:
        # L'étranglement du lot 1a doit être actif : sans lui, une saison entière
        # dépasserait les 5 req/min du tier gratuit. On le prouve plutôt que de le
        # supposer — un throttle neutre et un throttle actif se ressemblent en silence.
        _section("1. Client et étranglement")
        print(f"  chemin appelé          : {client._games_path}")
        print(f"  intervalle minimal     : {client._min_interval} s")
        print(f"  tentatives max         : {client._max_retries}")
        print(f"  borne de pagination    : {client._max_pages} pages")
        if client._games_path != config["results"]["games_paths"][sport]:
            report.fail("Le chemin appelé ne correspond pas au sport configuré")
        elif client._min_interval <= 0 and not args.offline:
            report.fail("Étranglement inactif", "le tier gratuit plafonne à 5 req/min")
        else:
            report.ok("Client construit par `from_config`",
                      "routage par ligue et étranglement du lot 1a effectivement en jeu")

        # ── requêtes brutes : c'est le payload non parsé qu'on vient inspecter ──
        # `_get` est privé, et c'est délibéré : la sonde doit voir exactement ce que
        # la production reçoit, pas une seconde implémentation de la requête.
        day_payload = client._get(
            {"start_date": args.day, "end_date": args.day, "per_page": 100}
        )
        single_payload = client._get(
            {"start_date": args.day, "end_date": args.day, "per_page": 1}
        )
        history_payload = client._get(
            {"start_date": args.history_start, "end_date": args.history_end, "per_page": 100}
        )

        day_games = _raw_games(day_payload)
        history_games = _raw_games(history_payload)
        all_raw = day_games + history_games

        if not day_games:
            report.fail(f"Aucun match retourné pour le {args.day}",
                        "journée de référence mal choisie : la sonde ne discrimine rien")
            _print_footer(report, transport)
            return report.exit_code()

        report.ok(f"HTTP 200 sur {client._games_path}",
                  f"{len(day_games)} match(s) le {args.day}, {len(history_games)} sur la plage "
                  f"{args.history_start} → {args.history_end}")

        check_scores_and_dates(report, day_games, calendar_tz)
        check_statuses(report, all_raw)
        check_fields(report, all_raw, int(season_config["season"]))
        check_pagination(report, single_payload)
        check_team_names(report, all_raw, conn, team_aliases(config, sport))

        # ── passes parsées : la chaîne de production digère-t-elle ce payload ? ──
        # C'est la leçon du 2026-08-07 : le routage était juste, le parsing non.
        # Ces appels réutilisent les pages déjà en cache.
        _section("6. Chaîne de parsing de production")
        parsed_day = client.get_games(args.day, args.day)
        parsed_history = client.get_games(args.history_start, args.history_end)
        print(f"  get_games({args.day}) → {len(parsed_day)} GameResult")
        print(f"  get_games({args.history_start} → {args.history_end}) "
              f"→ {len(parsed_history)} GameResult")
        if parsed_day:
            sample = parsed_day[0]
            print(f"  échantillon : {sample.away_team} @ {sample.home_team} "
                  f"{sample.away_score}-{sample.home_score} "
                  f"le {sample.game_date} (status={sample.status!r}, id={sample.game_id!r}, "
                  f"season={sample.season!r}, postseason={sample.postseason!r})")
            report.ok("Le parsing de production digère le payload réel",
                      "aucune levée sur les champs, scores et dates")
        else:
            report.fail("Le parsing de production ne rend aucun match",
                        "des matchs bruts existent pourtant sur cette journée")

        if not all(game.is_final for game in parsed_day + parsed_history):
            report.fail("Un GameResult non terminal a franchi le filtre de statut")

        check_history_depth(report, len(parsed_history), args.history_start, args.history_end)

    except CacheMissOffline as exc:
        # Attendu quand on rejoue --offline sur un cache vide : ce n'est pas un défaut
        # de format, c'est l'absence de la donnée. Le distinguer évite de conclure à
        # tort que l'API a changé.
        print(f"\n✗ Cache incomplet en mode hors-ligne : {exc}", file=sys.stderr)
        report.fail("Donnée absente du cache", "relancer sans --offline pour la télécharger")
    except ResultsApiError as exc:
        print(f"\n✗ Erreur API : {exc}", file=sys.stderr)
        report.fail("Appel balldontlie en échec", str(exc))
    finally:
        client.close()

    _print_footer(report, transport)
    return report.exit_code()


def _print_footer(report: Report, transport: CachingTransport) -> None:
    print("\n" + "═" * WIDTH)
    print(f"{'Requêtes réseau réellement émises':<40} : {transport.network_calls}")
    print(f"{'Pages servies depuis le cache':<40} : {transport.hits}")
    print(f"{'Entrées écrites en cache':<40} : {transport.stored}")
    print(f"{'Crédits The Odds API consommés':<40} : 0  (source distincte et gratuite)")
    print(f"{'Écritures en base':<40} : 0  (lecture seule)")
    print("═" * WIDTH)

    if report.failures:
        print(f"\n✗ ÉCHEC — {len(report.failures)} contrôle(s) en échec :")
        for failure in report.failures:
            print(f"    · {failure}")
        print("\n  NE PAS lancer le téléchargement de la saison avant résolution.")
        return

    if report.warnings:
        print(f"\n⚠ {len(report.warnings)} avertissement(s), aucun bloquant :")
        for warning in report.warnings:
            print(f"    · {warning}")
    print("\n✓ Format confirmé. Le téléchargement de la saison peut être lancé.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--day", default=DEFAULT_PROBE_DAY,
                        help=f"journée de référence (défaut : {DEFAULT_PROBE_DAY})")
    parser.add_argument("--history-start", default=DEFAULT_HISTORY_START,
                        help=f"début de la plage historique (défaut : {DEFAULT_HISTORY_START})")
    parser.add_argument("--history-end", default=DEFAULT_HISTORY_END,
                        help=f"fin de la plage historique (défaut : {DEFAULT_HISTORY_END})")
    parser.add_argument("--cache-dir", default=None,
                        help="répertoire de cache (défaut : backfill.cache_dir)")
    parser.add_argument("--offline", action="store_true",
                        help="interdit tout accès réseau : échoue sur un défaut de cache")
    args = parser.parse_args()

    for value in (args.day, args.history_start, args.history_end):
        date.fromisoformat(value)   # échec immédiat sur une date malformée

    settings = load_settings()
    configure_logging(settings.log_level)

    if not settings.balldontlie_api_key:
        print("BALLDONTLIE_API_KEY absente de l'environnement (.env).", file=sys.stderr)
        sys.exit(EXIT_MISMATCH)

    db_path = Path(settings.database_path)
    conn: sqlite3.Connection | None = None
    if db_path.exists():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
    try:
        sys.exit(probe(conn, load_config(), settings, args))
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    main()
