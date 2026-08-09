#!/usr/bin/env python3
"""Rejoue une saison complète pour mûrir les notes Elo (B5 lot 3).

POURQUOI. La base ne contient que quelques semaines de matchs alors qu'on est à
mi-saison. Activer le modèle sans backfill démarrerait toutes les équipes à 1500 :
`p_model` serait faux pendant des semaines et `min_games_for_edge` ne serait jamais
satisfait — l'edge ne deviendrait jamais exploitable (spec §5.8).

CE QUE LE SCRIPT PROUVE, ET CE QU'IL NE PROUVE PAS. Un rejeu peut s'exécuter sans la
moindre erreur et produire des notes absurdes. Le rapport ne dit donc pas « le script a
tourné » mais mesure si les prédictions valent mieux que le hasard, avec un contrôle
négatif qui vérifie que la mesure elle-même n'est pas complaisante. En revanche, la
comparaison du classement obtenu au **vrai classement WNBA** reste à l'œil humain : le
script n'a pas de source externe pour la faire.

SÛRETÉ. Sans `--commit`, AUCUNE écriture : téléchargement (mis en cache), rejeu en
mémoire, rapport. Avec `--commit`, une sauvegarde horodatée de la base est créée et
**relue** avant toute écriture, et les tables préexistantes sont recomptées après.
Zéro crédit The Odds API : balldontlie est une source distincte et gratuite.

Usage :
    uv run python scripts/backfill_elo.py                     # simulation + rapport
    uv run python scripts/backfill_elo.py --offline           # depuis le cache seul
    uv run python scripts/backfill_elo.py --offline --commit  # écriture en base
    uv run python scripts/backfill_elo.py --replace --commit  # purge puis réécriture
"""
from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from analyzer.metrics import (
    CHANCE_LOG_LOSS,
    Prediction,
    ScoreCard,
    calibration,
    constant_baseline,
    home_win_rate,
    score,
    shuffled,
    spearman,
)
from analyzer.model import params_from_config
from analyzer.ratings import (
    TeamState,
    predict_only,
    replay,
    sort_games,
    unusable_reason,
)
from common import db
from common.config import load_config, load_settings
from common.http_cache import CacheMissOffline, CachingTransport, unthrottled
from common.logging_config import configure_logging
from common.results_api_client import ResultsApiClient, ResultsApiError
from evaluator.reconcile import TeamContractError, build_canonical_resolver, team_aliases

EXIT_OK, EXIT_ABORT = 0, 1
WIDTH = 96

# Bornes de vraisemblance des notes en fin de saison. Avertissement seulement : une
# ligue très déséquilibrée peut légitimement les dépasser, mais un dépassement large
# signale plus souvent un rejeu cassé qu'une saison exceptionnelle.
PLAUSIBLE_RATING_RANGE = (1200.0, 1800.0)
# Corrélation de rang minimale entre classement Elo et classement aux victoires.
MIN_RANK_CORRELATION = 0.6
# Tailles d'échantillon sous lesquelles une log-loss ne veut rien dire.
MIN_WALKFORWARD_SAMPLE = 100
MIN_HOLDOUT_SAMPLE = 40


class Gate:
    """Porte de vraisemblance : verte, avertissement, ou bloquante."""

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.warnings: list[str] = []

    def check(self, passed: bool, label: str, detail: str = "") -> bool:
        (self.ok if passed else self.fail)(label, detail)
        return passed

    def ok(self, label: str, detail: str = "") -> None:
        print(f"  🔒 ✓ {label}{('  — ' + detail) if detail else ''}")

    def warn(self, label: str, detail: str = "") -> None:
        self.warnings.append(label)
        print(f"     ⚠ {label}{('  — ' + detail) if detail else ''}")

    def fail(self, label: str, detail: str = "") -> None:
        self.failures.append(label)
        print(f"  🔒 ✗ {label}{('  — ' + detail) if detail else ''}")


def _section(title: str) -> None:
    print(f"\n{title}")
    print("─" * WIDTH)


def _known_teams(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT home_team AS team FROM matches "
        "UNION SELECT DISTINCT away_team FROM matches ORDER BY team"
    ).fetchall()
    return [row["team"] for row in rows]


def _date_range(config: dict, sport: str, now: datetime) -> tuple[str, str]:
    """Plage à interroger, élargie d'un jour **de chaque côté**.

    Sans la borne haute élargie, les matchs du soir de la dernière journée — indexés au
    lendemain en UTC par balldontlie — seraient systématiquement perdus (réserve du
    journal 2026-08-08). Sans la borne basse, le premier soir de la saison le serait.
    """
    season = config["backfill"]["seasons"][sport]
    calendar_tz = config["results"]["calendar_timezone"]
    start = date.fromisoformat(season["start_date"]) - timedelta(days=1)
    end = now.astimezone(ZoneInfo(calendar_tz)).date() + timedelta(days=1)
    return start.isoformat(), end.isoformat()


# ───────────────────────────── téléchargement ─────────────────────────────


def _unresolvable_reason(game, resolve) -> str | None:
    """Raison si l'une des deux équipes n'est pas une franchise suivie, sinon `None`.

    Cas réel : le **All-Star Game** oppose des sélections (`TEAM COOP`, `TEAM SPOON`,
    ids 34/35) qui ne sont pas des franchises. Un match d'exhibition à effectifs mixtes
    ne dit rien de la force d'un club et n'a pas à entrer dans les notes.

    Écarter plutôt que lever, mais **jamais en silence** : chaque exclusion est nommée
    dans le journal. La garantie que ce filtre ne masque pas une vraie divergence de
    nom — un `Fire`/`Tempo` non aliasé, qui serait lui aussi « non résoluble » — est
    portée par la porte du bloc (a) : **chaque franchise suivie doit finir avec une
    note**. Une franchise renommée par la source verrait tous ses matchs écartés, donc
    n'aurait aucune note, et la porte se fermerait. C'est le second sens du contrat qui
    rend cette exclusion sûre.
    """
    for name in (game.home_team, game.away_team):
        try:
            resolve(name)
        except TeamContractError:
            return f"équipe hors franchises suivies : {name!r} (exhibition ?)"
    return None


def download(client: ResultsApiClient, config: dict, sport: str, now: datetime, journal: dict,
             resolve):
    """Récupère la saison, déduplique et filtre — chaque exclusion est comptée."""
    start, end = _date_range(config, sport, now)
    journal["range"] = f"{start} → {end}"
    started = time.monotonic()
    games = client.get_games(start, end)
    journal["fetch_seconds"] = time.monotonic() - started
    journal["raw"] = len(games)

    expected_season = int(config["backfill"]["seasons"][sport]["season"])
    by_id: dict[str, object] = {}
    duplicates = 0
    wrong_season = Counter()
    unknown_season = 0
    postseason = 0
    unusable: list[str] = []
    kept = []

    for game in games:
        if game.season is not None and game.season != expected_season:
            wrong_season[game.season] += 1
            continue
        if game.season is None:
            # On ne peut pas affirmer qu'un match est hors saison faute de le savoir :
            # on le garde et on le signale (invariant 5).
            unknown_season += 1
        if game.game_id is not None:
            if game.game_id in by_id:
                duplicates += 1
                continue
            by_id[game.game_id] = game
        reason = unusable_reason(game) or _unresolvable_reason(game, resolve)
        if reason is not None:
            unusable.append(
                f"{game.game_date}  {game.away_team} @ {game.home_team}  "
                f"(id={game.game_id})  — {reason}"
            )
            continue
        if game.postseason:
            postseason += 1
        kept.append(game)

    journal.update(duplicates=duplicates, wrong_season=dict(wrong_season),
                   unknown_season=unknown_season, postseason=postseason,
                   unusable=unusable, kept=len(kept))
    return kept


# ───────────────────────── blocs du rapport ─────────────────────────


def block_invariants(gate: Gate, states: dict, applications: list, known_teams: list[str],
                     params) -> None:
    _section("(a) INVARIANTS STRUCTURELS")
    ratings = [state.rating for state in states.values()]
    mean = sum(ratings) / len(ratings)
    total_games = sum(state.games_played for state in states.values())

    print(f"  {'Équipes notées':<44} {len(states)}")
    print(f"  {'Matchs rejoués':<44} {len(applications)}")
    print(f"  {'Moyenne des notes':<44} {mean:.9f}")
    print(f"  {'Étendue':<44} {min(ratings):.1f} → {max(ratings):.1f}")

    gate.check(
        abs(mean - params.initial_rating) < 1e-6,
        "Moyenne des notes égale à la note de départ",
        f"{mean:.9f} vs {params.initial_rating} — le jeu à somme nulle est préservé",
    )
    gate.check(
        total_games == 2 * len(applications),
        "Somme des compteurs = 2 × matchs rejoués",
        f"{total_games} = 2 × {len(applications)}",
    )
    missing = [team for team in known_teams
               if all(state_key != _normalized(team) for state_key in states)]
    gate.check(
        not missing,
        "Chaque équipe suivie possède une note",
        f"{len(known_teams)} équipes" if not missing else f"manquantes : {missing}",
    )
    low, high = PLAUSIBLE_RATING_RANGE
    if min(ratings) < low or max(ratings) > high:
        gate.warn("Étendue des notes hors bornes usuelles",
                  f"{min(ratings):.0f}–{max(ratings):.0f} hors [{low:.0f}, {high:.0f}]")


def _normalized(name: str) -> str:
    return " ".join(name.strip().lower().split())


def block_standings(gate: Gate, states: dict, applications: list,
                    known_teams: list[str]) -> None:
    _section("(b) CLASSEMENT — notes Elo confrontées au bilan victoires/défaites")
    wins, losses = Counter(), Counter()
    for application in applications:
        winner = application.home if application.home_won else application.away
        loser = application.away if application.home_won else application.home
        wins[winner.key] += 1
        losses[loser.key] += 1

    # Noms CANONIQUES (The Odds API), pas ceux de la source : ce tableau est fait pour
    # être comparé au vrai classement WNBA, où l'on cherche « Portland Fire », pas
    # « Fire ».
    display = {_normalized(team): team for team in known_teams}

    ranked = sorted(states.items(), key=lambda item: -item[1].rating)
    by_record = sorted(
        states,
        key=lambda key: -(wins[key] / max(wins[key] + losses[key], 1)),
    )
    record_rank = {key: index + 1 for index, key in enumerate(by_record)}

    print(f"  {'#':<4}{'Équipe':<26}{'Elo':>8}   {'V-D':>8}  {'%V':>6}  {'#V-D':>5}  écart")
    for index, (key, state) in enumerate(ranked, start=1):
        played = wins[key] + losses[key]
        pct = wins[key] / played if played else 0.0
        delta = record_rank[key] - index
        record = f"{wins[key]}-{losses[key]}"
        print(f"  {index:<4}{display.get(key, key):<26}{state.rating:>8.1f}   "
              f"{record:>8}  {pct:>5.1%}  {record_rank[key]:>5}  "
              f"{delta:+d}" if delta else
              f"  {index:<4}{display.get(key, key):<26}{state.rating:>8.1f}   "
              f"{record:>8}  {pct:>5.1%}  {record_rank[key]:>5}      ·")

    elo_values = [state.rating for _, state in ranked]
    win_values = [wins[key] / max(wins[key] + losses[key], 1) for key, _ in ranked]
    rho = spearman(elo_values, win_values)
    print(f"\n  Corrélation de rang de Spearman (Elo ↔ % de victoires) : ρ = {rho:.3f}")

    if rho < MIN_RANK_CORRELATION:
        gate.warn(f"Corrélation de rang faible (ρ = {rho:.3f})",
                  f"attendu ≥ {MIN_RANK_CORRELATION} — rejeu suspect")
    else:
        print(f"     ✓ ρ ≥ {MIN_RANK_CORRELATION} : les deux classements concordent")

    print("\n  ⚠ Ce contrôle n'est PAS une validation externe : les deux classements")
    print("    sortent des mêmes matchs. Il est informatif parce qu'Elo ≠ bilan brut")
    print("    (marge de victoire, avantage du terrain, force du calendrier), mais la")
    print("    confrontation au VRAI classement WNBA reste à faire à l'œil.")


def _print_scorecard(label: str, card: ScoreCard) -> None:
    print(f"  {label:<40}{card.n:>6}   {card.log_loss:>8.4f}  {card.versus_chance:>+7.1f} %"
          f"  {card.brier:>7.4f}  {card.accuracy:>8.1%}")


def block_prediction(gate: Gate, walkforward: list[Prediction], holdout: list[Prediction],
                     min_games: int) -> None:
    _section("(c) TEST PRÉDICTIF — la porte décisive")
    print(f"  Filtre de maturité : les deux équipes ont ≥ {min_games} matchs intégrés")
    print("  (`model.decision.min_games_for_edge`, réutilisé — pas un seuil jumeau)\n")
    print(f"  {'Échantillon':<40}{'N':>6}   {'log-loss':>8}  {'vs hasard':>9}"
          f"  {'Brier':>7}  {'exactitude':>10}")

    walk = score(walkforward)
    _print_scorecard("Walk-forward (notes mûres)", walk)

    hold = score(holdout) if holdout else None
    if hold:
        _print_scorecard("Hold-out gelé (fin de saison)", hold)

    print("  " + "·" * (WIDTH - 4))
    _print_scorecard("Référence — hasard (p = 0,5)",
                     constant_baseline(walkforward, 0.5))
    base_rate = home_win_rate(walkforward)
    _print_scorecard(f"Référence — taux domicile ({base_rate:.1%})",
                     constant_baseline(walkforward, base_rate))
    negative = score(shuffled(walkforward))
    _print_scorecard("Contrôle négatif (permutées)", negative)

    print()
    gate.check(walk.beats_chance,
               "Walk-forward bat le hasard",
               f"{walk.log_loss:.4f} < {CHANCE_LOG_LOSS:.4f}")
    gate.check(walk.n >= MIN_WALKFORWARD_SAMPLE,
               f"Échantillon walk-forward ≥ {MIN_WALKFORWARD_SAMPLE}", f"N = {walk.n}")

    if hold is None:
        gate.fail("Hold-out impossible", "aucun match après la coupure")
    else:
        gate.check(hold.beats_chance, "Hold-out gelé bat le hasard",
                   f"{hold.log_loss:.4f} < {CHANCE_LOG_LOSS:.4f}")
        gate.check(hold.n >= MIN_HOLDOUT_SAMPLE,
                   f"Échantillon hold-out ≥ {MIN_HOLDOUT_SAMPLE}", f"N = {hold.n}")

    gate.check(
        negative.log_loss >= CHANCE_LOG_LOSS,
        "Contrôle négatif au niveau du hasard ou pire",
        f"{negative.log_loss:.4f} ≥ {CHANCE_LOG_LOSS:.4f} — la mesure n'est pas complaisante",
    )

    if not constant_baseline(walkforward, base_rate).log_loss > walk.log_loss:
        gate.warn("Le modèle ne bat pas la référence « taux domicile »",
                  "battre le hasard sans battre un modèle constant ne prouve presque rien")

    print("\n  Calibration (affichage seul — trop peu de cas par tranche pour une porte)")
    print(f"  {'tranche':<14}{'N':>5}   {'annoncé':>8}  {'observé':>8}   écart")
    for bucket in calibration(walkforward):
        gap = bucket.observed - bucket.predicted
        bar = "█" * int(abs(gap) * 100 / 2)
        print(f"  {bucket.low:.0%}–{bucket.high:.0%}{'':<7}{bucket.n:>5}   "
              f"{bucket.predicted:>7.1%}  {bucket.observed:>7.1%}   {gap:>+6.1%} {bar}")


def block_journal(journal: dict, transport: CachingTransport) -> None:
    _section("(d) JOURNAL D'EXÉCUTION")
    print(f"  {'Plage interrogée':<44} {journal['range']}")
    print(f"  {'Matchs terminés reçus':<44} {journal['raw']}")
    print(f"  {'Retenus après filtres':<44} {journal['kept']}")
    print(f"  {'Doublons game_id écartés':<44} {journal['duplicates']}")
    print(f"  {'Écartés (corrompus / hors franchises)':<44} {len(journal['unusable'])}")
    for line in journal["unusable"]:
        print(f"      · {line}")
    print(f"  {'Hors saison attendue':<44} {journal['wrong_season'] or 0}")
    print(f"  {'Saison inconnue (conservés)':<44} {journal['unknown_season']}")
    print(f"  {'Playoffs (conservés, comptés)':<44} {journal['postseason']}")
    print(f"  {'Bornes de dates observées':<44} {journal['first_date']} → {journal['last_date']}")
    print(f"  {'Coupure hold-out':<44} {journal['cutoff']}")
    print(f"  {'Requêtes réseau réellement émises':<44} {transport.network_calls}")
    print(f"  {'Pages servies depuis le cache':<44} {transport.hits}")
    print(f"  {'Durée du téléchargement':<44} {journal['fetch_seconds']:.1f} s")
    print(f"  {'Crédits The Odds API consommés':<44} 0  (source distincte et gratuite)")
    print(f"  {'Écritures en base':<44} {journal['writes']}")


# ───────────────────────────── écriture ─────────────────────────────


def _census(conn: sqlite3.Connection) -> dict[str, int]:
    """Nombre de lignes de chaque table existante, avant/après écriture."""
    names = [row["name"] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    )]
    return {name: conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()["n"]
            for name in names}


def _evaluations_fingerprint(conn: sqlite3.Connection) -> str:
    """Empreinte du CONTENU des évaluations, pas seulement de leur nombre.

    Un compte inchangé ne prouverait pas qu'aucune ligne n'a été modifiée : c'est
    exactement l'angle mort qu'un simple recomptage laisserait ouvert.
    """
    rows = conn.execute(
        "SELECT id, verdict_id, home_score, away_score, outcome, closing_odds, clv, "
        "clv_unit FROM evaluations ORDER BY id"
    ).fetchall()
    material = "|".join(str(tuple(row)) for row in rows)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def backup_database(db_path: Path, gate: Gate) -> Path | None:
    """Sauvegarde horodatée, **relue** avant d'autoriser la moindre écriture.

    Utilise `sqlite3.Connection.backup()` plutôt que `cp` : l'API est atomique et
    cohérente même si le tick collecteur écrit au même instant, là où une copie de
    fichier peut capturer un état intermédiaire.

    Une sauvegarde qu'on n'a pas relue n'est pas une sauvegarde : le contenu est
    recompté et comparé à la source, et l'écriture est refusée en cas d'écart.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    target = db_path.parent / "backups" / f"nba_odds_pre_backfill_{stamp}.db"
    target.parent.mkdir(parents=True, exist_ok=True)

    source = sqlite3.connect(db_path)
    try:
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
        finally:
            destination.close()
        expected = {name: source.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()[0]
                    for name in ("matches", "evaluations", "verdicts", "odds_snapshots")}
    finally:
        source.close()

    check = sqlite3.connect(target)
    try:
        actual = {name: check.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()[0]
                  for name in expected}
    finally:
        check.close()

    size_mb = target.stat().st_size / (1024 * 1024)
    print(f"  Sauvegarde        : {target}")
    print(f"  Taille            : {size_mb:.1f} Mo")
    print("  Contenu relu      : " + ", ".join(f"{k}={v}" for k, v in actual.items()))

    if actual != expected:
        gate.fail("Sauvegarde non conforme à la source",
                  f"attendu {expected}, relu {actual} — écriture refusée")
        return None
    gate.ok("Sauvegarde créée ET relue", "contenu identique à la source")
    return target


def write_ratings(conn: sqlite3.Connection, sport: str, states: dict, applications: list,
                  gate: Gate, journal: dict, *, replace: bool) -> bool:
    """Écrit les deux tables dans une seule transaction, puis vérifie l'intact."""
    before = _census(conn)
    fingerprint_before = _evaluations_fingerprint(conn)
    print("  Recensement avant : " + ", ".join(f"{k}={v}" for k, v in before.items()))

    existing = db.count_rating_history(conn, sport, "backfill")
    if existing and not replace:
        gate.fail(f"Un backfill est déjà présent pour {sport} ({existing} lignes)",
                  "relancer avec --replace pour le remplacer")
        return False

    stamp = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if replace and existing:
            history, ratings = db.delete_backfill_ratings(conn, sport)
            print(f"  Purge préalable   : {history} historique(s), {ratings} note(s)")

        for application in applications:
            for outcome in application.outcomes():
                db.insert_rating_history(
                    conn, sport=sport, team=outcome.key,
                    game_date=application.game_date.isoformat(), source="backfill",
                    source_game_id=application.game_id, match_id=None,
                    opponent=outcome.opponent, is_home=outcome.is_home,
                    rating_before=outcome.rating_before, rating_after=outcome.rating_after,
                    expected_win=outcome.expected_win, created_at=stamp,
                )
        for key, state in states.items():
            db.upsert_team_rating(
                conn, sport=sport, team=key,
                display_name=journal["display"].get(key, key), rating=state.rating,
                games_played=state.games_played,
                last_game_date=state.last_game_date.isoformat()
                if state.last_game_date else None,
                updated_at=stamp,
            )
        conn.commit()
    except (sqlite3.Error, ValueError) as exc:
        conn.rollback()
        gate.fail("Écriture annulée", f"{type(exc).__name__} : {exc}")
        return False

    written = db.count_rating_history(conn, sport, "backfill")
    journal["writes"] = written + len(states)
    gate.check(written == 2 * len(applications),
               "Historique écrit = 2 × matchs rejoués", f"{written} = 2 × {len(applications)}")

    after = _census(conn)
    fingerprint_after = _evaluations_fingerprint(conn)
    print("  Recensement après : " + ", ".join(f"{k}={v}" for k, v in after.items()))

    untouched = {name: count for name, count in before.items()
                 if name not in ("team_ratings", "rating_history")}
    drifted = {name: (count, after.get(name))
               for name, count in untouched.items() if after.get(name) != count}
    gate.check(not drifted, "Tables préexistantes inchangées (comptage)",
               f"{len(untouched)} tables" if not drifted else f"écarts : {drifted}")
    gate.check(fingerprint_before == fingerprint_after,
               "Contenu des évaluations inchangé (empreinte)",
               f"{fingerprint_before} — un comptage seul n'aurait pas prouvé l'absence "
               f"de modification")
    return not gate.failures


# ───────────────────────────── orchestration ─────────────────────────────


def run(conn: sqlite3.Connection, config: dict, settings, args) -> int:
    sport = config["api"]["sport"]
    params = params_from_config(config)
    aliases = team_aliases(config, sport)
    known_teams = _known_teams(conn)
    cache_dir = Path(args.cache_dir or config["backfill"]["cache_dir"])
    now = datetime.now(timezone.utc)
    journal: dict = {"writes": 0}

    print("═" * WIDTH)
    print(f"BACKFILL ELO — {sport}   ({'SIMULATION, aucune écriture' if not args.commit else 'ÉCRITURE'})")
    print("═" * WIDTH)

    try:
        resolve = build_canonical_resolver(aliases=aliases, known_teams=known_teams)
    except TeamContractError as exc:
        print(f"✗ Contrat de clé d'équipe rompu : {exc}", file=sys.stderr)
        return EXIT_ABORT

    transport = CachingTransport(cache_dir, offline=args.offline)
    client = ResultsApiClient.from_config(
        settings, unthrottled(config) if args.offline else config, transport=transport
    )
    try:
        games = download(client, config, sport, now, journal, resolve)
    except CacheMissOffline as exc:
        print(f"✗ Cache incomplet en mode hors-ligne : {exc}", file=sys.stderr)
        return EXIT_ABORT
    except (ResultsApiError, TeamContractError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return EXIT_ABORT
    finally:
        client.close()

    if not games:
        print("✗ Aucun match retenu : rien à rejouer.", file=sys.stderr)
        return EXIT_ABORT

    ordered = sort_games(games)
    journal["first_date"] = ordered[0].game_date
    journal["last_date"] = ordered[-1].game_date

    try:
        states, applications = replay(ordered, params, normalize=resolve)
    except TeamContractError as exc:
        print(f"✗ Rejeu interrompu : {exc}", file=sys.stderr)
        return EXIT_ABORT

    min_games = int(config["model"]["decision"]["min_games_for_edge"])
    walkforward = [
        Prediction(
            probability=application.expected_home,
            home_won=application.home_won,
            label=f"{application.away.display_name} @ {application.home.display_name}",
        )
        for application in applications
        if min(application.home.games_played_before,
               application.away.games_played_before) >= min_games
    ]

    # Hold-out : coupure chronologique, notes gelées, matchs jamais vus.
    fraction = float(config["backfill"]["holdout_fraction"])
    split = int(round(len(ordered) * (1.0 - fraction)))
    cutoff = ordered[split].game_date if split < len(ordered) else None
    journal["cutoff"] = cutoff or "—"
    holdout: list[Prediction] = []
    if cutoff is not None:
        train = [game for game in ordered if game.game_date < cutoff]
        test = [game for game in ordered if game.game_date >= cutoff]
        frozen, _ = replay(train, params, normalize=resolve)
        current: dict[str, TeamState] = frozen
        for game in test:
            current, expected = predict_only(current, game, params, normalize=resolve)
            holdout.append(Prediction(
                probability=expected,
                home_won=game.home_score > game.away_score,
                label=f"{game.away_team} @ {game.home_team}",
            ))

    gate = Gate()
    block_invariants(gate, states, applications, known_teams, params)
    block_standings(gate, states, applications, known_teams)
    block_prediction(gate, walkforward, holdout, min_games)
    block_journal(journal, transport)

    if args.commit and not gate.failures:
        _section("(e) ÉCRITURE EN BASE")
        journal["display"] = {_normalized(team): team for team in known_teams}
        if backup_database(Path(settings.database_path), gate) is not None:
            # `init_db` APRÈS la sauvegarde : c'est lui qui crée les deux tables
            # (les images Docker tournent sur du code antérieur au lot 2, cf. journal),
            # donc c'est déjà une modification de schéma.
            db.init_db(Path(settings.database_path))
            print("  Schéma            : init_db appliqué (tables du lot 2 créées au besoin)")
            write_ratings(conn, sport, states, applications, gate, journal,
                          replace=args.replace)

    print("\n" + "═" * WIDTH)
    if gate.failures:
        print(f"✗ ÉCHEC — {len(gate.failures)} porte(s) fermée(s) :")
        for failure in gate.failures:
            print(f"    · {failure}")
        print("\n  Les notes ne sont pas vraisemblables : NE PAS écrire en base.")
        return EXIT_ABORT

    if gate.warnings:
        print(f"⚠ {len(gate.warnings)} avertissement(s), aucun bloquant :")
        for warning in gate.warnings:
            print(f"    · {warning}")
    print("✓ Toutes les portes de vraisemblance sont vertes.")
    if not args.commit:
        print("\n  SIMULATION — aucune écriture. Relancer avec --commit pour appliquer,")
        print("  après avoir comparé le classement ci-dessus au vrai classement WNBA.")
    print("═" * WIDTH)
    return EXIT_OK


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--commit", action="store_true",
                        help="écrit en base (sauvegarde horodatée créée et relue avant)")
    parser.add_argument("--replace", action="store_true",
                        help="purge le backfill précédent de cette ligue avant de réécrire")
    parser.add_argument("--offline", action="store_true",
                        help="interdit tout accès réseau : échoue sur un défaut de cache")
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
