#!/usr/bin/env python3
"""Mesure la CALIBRATION du modèle Elo : quand il dit `p`, gagne-t-on ~`p` du temps ?

POURQUOI CE SCRIPT, ET POURQUOI IL N'EST PAS CELUI DU LOT 3. Le backfill a mesuré la
**discrimination** — le modèle classe-t-il les matchs mieux que le hasard (log-loss
−13 %) ? C'est une propriété distincte de la **fiabilité** : un modèle peut classer
parfaitement tout en annonçant 0,81 là où la vraie fréquence est 0,62. C'est exactement
le profil qu'un Elo trop dispersé produit, et celui que les premiers edges shadow
suggèrent (5 à 28 points d'écart avec le marché contre un τ de 0,02).

CE QU'IL MESURE, ET SUR QUOI. L'axe « annoncé » vient de `expected_win`, calculé **avant**
le match par le même `forecast` que la production. L'axe « observé » vient du **score réel**
(`home_score > away_score`), pas d'une propriété dérivée des notes : déduire la victoire du
signe de la variation Elo fonctionne aujourd'hui — le multiplicateur de marge ne module que
l'amplitude — mais lierait la mesure à un invariant du calcul qui pourrait changer sans
bruit. On lit donc le résultat à la source.

CE QU'IL NE FAIT PAS. Il ne recalibre rien : ni `k_factor`, ni `home_advantage_elo`, ni τ.
Il mesure. La décision de recalibrer se prend sur la courbe, pas dans le script qui la
produit.

SÛRETÉ. Lecture seule, hors ligne par défaut (cache disque du lot 3) : aucune écriture en
base, aucun crédit The Odds API.

Usage :
    uv run python scripts/calibrate_model.py                  # depuis le cache
    uv run python scripts/calibrate_model.py --online         # rafraîchit le cache
    uv run python scripts/calibrate_model.py --width 0.1      # tranches plus fines
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from analyzer.metrics import (
    CHANCE_LOG_LOSS,
    Prediction,
    ScoreCard,
    calibration,
    constant_baseline,
    home_win_rate,
    score,
    shuffled,
)
from analyzer.model import params_from_config
from analyzer.ratings import mature_applications, replay, sort_games, unusable_reason
from analyzer.ratings_store import build_resolver, replay_range
from common.config import load_config, load_settings
from common.http_cache import CachingTransport, unthrottled
from common.logging_config import configure_logging
from common.results_api_client import ResultsApiClient, ResultsApiError
from evaluator.reconcile import TeamContractError

EXIT_OK, EXIT_ABORT = 0, 1
WIDTH = 88
DEFAULT_BUCKET_WIDTH = 0.2      # 20 points : effectifs exploitables sur une saison WNBA
DEFAULT_MIN_BUCKET = 20         # sous ce seuil, une tranche s'affiche mais ne conclut pas


def _section(title: str) -> None:
    print(f"\n{title}")
    print("─" * WIDTH)


def gather(client, conn, config: dict, sport: str, resolve, min_games: int):
    """Rejoue la saison et rend `(prédictions mûres, journal)`.

    Le rejeu porte à la fois la prédiction (`expected_win`, calculée avant le match) et
    le résultat réel (`home_won`, dérivé des scores) : aucune jointure n'est nécessaire,
    et les deux axes de la courbe viennent de la même source que la production.
    """
    start, end = replay_range(conn, config, sport)
    print(f"Plage interrogée      : {start} → {end}")
    games = client.get_games(start, end)
    expected_season = int(config["backfill"]["seasons"][sport]["season"])

    seen: set[str] = set()
    kept = []
    ecartes = {"hors_saison": 0, "doublons": 0, "inexploitables": 0, "hors_franchises": 0}
    for game in games:
        if game.season is not None and game.season != expected_season:
            ecartes["hors_saison"] += 1
            continue
        if game.game_id is not None:
            if game.game_id in seen:
                ecartes["doublons"] += 1
                continue
            seen.add(game.game_id)
        if unusable_reason(game) is not None:
            ecartes["inexploitables"] += 1
            continue
        try:
            resolve(game.home_team), resolve(game.away_team)
        except TeamContractError:
            ecartes["hors_franchises"] += 1
            continue
        kept.append(game)

    params = params_from_config(config, sport=sport)
    _, applications = replay(sort_games(kept), params, normalize=resolve)

    # Filtre de maturité — la logique vit dans `analyzer.ratings` et y est testée :
    # c'est elle qui a renversé la lecture du 2026-08-09, elle ne peut pas rester
    # dans un script hors couverture.
    retenues, immatures = mature_applications(applications, min_games=min_games)
    mures = [
        # `home_won` vient du score réel, pas du signe de la variation de note.
        Prediction(probability=app.expected_home, home_won=app.home_won)
        for app in retenues
    ]

    journal = {
        "bruts": len(games), "retenus": len(applications),
        "immatures": immatures, "mures": len(mures), **ecartes,
    }
    return mures, journal


def _print_scorecard(label: str, card: ScoreCard) -> None:
    print(f"  {label:<34} n={card.n:<5} log-loss {card.log_loss:.4f}  "
          f"Brier {card.brier:.4f}  exactitude {card.accuracy:.1%}")


def block_discrimination(predictions: list[Prediction]) -> bool:
    """Rappel de la discrimination + contrôle négatif. Rend False si le harnais est muet."""
    _section("Discrimination (rappel — ce que le lot 3 mesurait déjà)")
    card = score(predictions)
    _print_scorecard("modèle", card)
    print(f"  {'hasard (ln 2)':<34} {'':<7} log-loss {CHANCE_LOG_LOSS:.4f}")

    taux = home_win_rate(predictions)
    _print_scorecard(f"taux domicile constant ({taux:.1%})", constant_baseline(predictions, taux))

    # Contrôle négatif : les mêmes probabilités permutées entre matchs doivent se
    # dégrader nettement. Sans lui, on ne saurait pas si la mesure sait virer au rouge.
    bruit = score(shuffled(predictions))
    _print_scorecard("contrôle négatif (permuté)", bruit)

    sain = bruit.log_loss > card.log_loss and bruit.log_loss > CHANCE_LOG_LOSS
    if not sain:
        print("\n  ⚠ Le contrôle négatif ne se dégrade PAS : la mesure ne discrimine rien, "
              "aucune lecture de la courbe ci-dessous n'est fiable.")
    return sain


def block_reliability(predictions: list[Prediction], *, width: float, min_bucket: int,
                      titre: str) -> list[str]:
    """Courbe de fiabilité. Rend la liste des tranches réellement concluantes."""
    _section(titre)
    print(f"  {'tranche':<12}{'n':>6}{'annoncé':>11}{'observé':>11}{'écart':>10}   lecture")
    print("  " + "─" * (WIDTH - 4))

    concluantes = []
    for bucket in calibration(predictions, width=width):
        if not bucket.n:
            continue
        ecart = bucket.observed - bucket.predicted
        if bucket.n < min_bucket:
            lecture = f"n<{min_bucket} — ne conclut pas"
        else:
            lecture = "cohérent" if abs(ecart) < 0.05 else (
                "SUR-confiant" if ecart < 0 else "sous-confiant")
            concluantes.append(
                f"{bucket.low:.1f}–{bucket.high:.1f} : annoncé {bucket.predicted:.3f}, "
                f"observé {bucket.observed:.3f} ({ecart:+.3f}, n={bucket.n})"
            )
        print(f"  {bucket.low:.1f}–{bucket.high:.1f}     {bucket.n:>6}{bucket.predicted:>11.3f}"
              f"{bucket.observed:>11.3f}{ecart:>+10.3f}   {lecture}")
    return concluantes


def block_home_bias(predictions: list[Prediction]) -> None:
    """Test agrégé de l'avantage terrain — le plus direct sur `home_advantage_elo`.

    La courbe côté extérieur est le miroir exact de celle du domicile (les probabilités
    sont complémentaires) : la séparer n'ajoute pas d'information indépendante, mais
    rend le biais lisible. Ce test-ci, lui, est autonome : il confronte la moyenne des
    probabilités annoncées au taux de victoires à domicile réellement observé.
    """
    _section("Biais domicile (test direct de `home_advantage_elo`)")
    annonce = sum(p.probability for p in predictions) / len(predictions)
    observe = home_win_rate(predictions)
    ecart = observe - annonce
    print(f"  Probabilité moyenne annoncée à domicile : {annonce:.3f}")
    print(f"  Taux de victoires à domicile observé    : {observe:.3f}")
    print(f"  Écart                                    : {ecart:+.3f}  (n={len(predictions)})")
    if abs(ecart) < 0.02:
        print("  → aucun biais agrégé notable.")
    else:
        sens = "SURESTIME" if ecart < 0 else "sous-estime"
        print(f"  → le modèle {sens} l'avantage du terrain sur cet échantillon.")


def run(conn: sqlite3.Connection, config: dict, settings, args) -> int:
    sport = config["api"]["sport"]
    min_games = int(config["model"]["decision"]["min_games_for_edge"])

    _section(f"Calibration du modèle Elo — {sport}")
    try:
        resolve = build_resolver(conn, config, sport)
    except TeamContractError as exc:
        print(f"Contrat de clé d'équipe rompu : {exc}", file=sys.stderr)
        return EXIT_ABORT

    transport = CachingTransport(
        Path(args.cache_dir or config["backfill"]["cache_dir"]), offline=not args.online
    )
    client = ResultsApiClient.from_config(
        settings, config if args.online else unthrottled(config), transport=transport
    )
    try:
        predictions, journal = gather(client, conn, config, sport, resolve, min_games)
    except ResultsApiError as exc:
        print(f"Récupération impossible : {exc}", file=sys.stderr)
        return EXIT_ABORT
    finally:
        client.close()

    print(f"Requêtes réseau       : {transport.network_calls}  (cache : {transport.hits})")
    print("Crédits The Odds API  : 0")
    print(f"Matchs rejoués        : {journal['retenus']}  "
          f"(bruts {journal['bruts']}, hors saison {journal['hors_saison']}, "
          f"doublons {journal['doublons']}, inexploitables {journal['inexploitables']}, "
          f"hors franchises {journal['hors_franchises']})")
    print(f"Prédictions mûres     : {journal['mures']}  "
          f"({journal['immatures']} écartées : une équipe à moins de {min_games} matchs)")

    if not predictions:
        print("\nAucune prédiction mûre : rien à mesurer.", file=sys.stderr)
        return EXIT_ABORT

    harnais_sain = block_discrimination(predictions)

    concluantes = block_reliability(
        predictions, width=args.width, min_bucket=args.min_bucket,
        titre="Courbe de fiabilité — côté DOMICILE",
    )
    # Côté extérieur : miroir exact, affiché parce que le biais domicile est l'hypothèse.
    exterieur = [
        Prediction(probability=1.0 - p.probability, home_won=not p.home_won)
        for p in predictions
    ]
    block_reliability(
        exterieur, width=args.width, min_bucket=args.min_bucket,
        titre="Courbe de fiabilité — côté EXTÉRIEUR (miroir du précédent)",
    )

    block_home_bias(predictions)

    _section("Lecture")
    if not harnais_sain:
        print("  ⚠ Contrôle négatif muet — ne rien conclure de cette exécution.")
        return EXIT_ABORT
    if not concluantes:
        print(f"  Aucune tranche n'atteint n={args.min_bucket} : échantillon trop mince "
              f"pour conclure. Élargir les tranches (--width) ou attendre des matchs.")
    else:
        print(f"  {len(concluantes)} tranche(s) au-dessus de n={args.min_bucket} :")
        for ligne in concluantes:
            print(f"    · {ligne}")
    print("\n  Ce script MESURE. Aucun paramètre n'est modifié : la décision de "
          "recalibrer\n  (`k_factor`, `home_advantage_elo`) se prend sur cette courbe, pas ici.")
    return EXIT_OK


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--online", action="store_true",
                        help="autorise le réseau (défaut : cache seul, zéro requête)")
    parser.add_argument("--width", type=float, default=DEFAULT_BUCKET_WIDTH,
                        help=f"largeur des tranches (défaut {DEFAULT_BUCKET_WIDTH})")
    parser.add_argument("--min-bucket", type=int, default=DEFAULT_MIN_BUCKET,
                        help=f"effectif minimal pour qu'une tranche conclue "
                             f"(défaut {DEFAULT_MIN_BUCKET})")
    parser.add_argument("--cache-dir", default=None)
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
