#!/usr/bin/env python3
"""Recalcule les 2 évaluations mal appariées avant le correctif de fuseau (B5 lot 1b).

CAUSE. Jusqu'au commit `e90e931`, `_parse_game` tronquait la date balldontlie à
`[:10]`, ce qui décalait d'un jour les matchs WNBA du soir (date-time UTC). Quand
une affiche se répète à deux jours d'écart — la norme en WNBA — et que **les deux**
matchs sont décalés, ils se retrouvent tous deux à 1 jour de la cible ;
`find_result` départage par `gap < best_gap` et retient donc le **premier
rencontré**, c'est-à-dire l'ancien. Le second match a été noté avec le score du
premier. Audit du 2026-08-08 : exactement deux évaluations concernées, nommées
explicitement ci-dessous.

CE QUI EST FAUX, ET SEULEMENT CELA. `home_score`, `away_score`, `outcome`. Les
colonnes `clv`, `closing_odds` et `clv_unit` dérivent des `odds_snapshots`
(append-only, intacts) et n'ont jamais touché balldontlie : elles sont justes, et
le script **prouve** qu'elles ne bougent pas en les relisant après écriture.

CE N'EST PAS UNE VALEUR FABRIQUÉE. L'issue est recalculée par `grade_verdict`
(fonction pure, testée) sur le **vrai** score du **bon** match. Le bon match est
identifiable sans ambiguïté : après correctif, l'appariement se fait à écart de
date **nul**, et deux rencontres d'une même affiche ne peuvent pas partager une
date calendaire américaine — le couple (affiche, date US) est une clé unique.

SÛRETÉ. Aucun DELETE, aucun recalcul en masse, aucune autre colonne touchée.
Idempotent : une ligne déjà corrigée est détectée et sautée. Écriture uniquement
avec `--commit` ; sans lui, le script se contente d'afficher le avant → après.

Usage :
    uv run python scripts/recompute_misgraded_verdicts_20260808.py            # simulation
    uv run python scripts/recompute_misgraded_verdicts_20260808.py --commit   # écriture
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

from common.config import load_config, load_settings
from common.results_api_client import ResultsApiClient
from evaluator.grading import grade_verdict
from evaluator.reconcile import find_result, tipoff_calendar_date

EXIT_OK, EXIT_ABORT = 0, 1

# Les DEUX seules évaluations de l'audit du 2026-08-08, nommées explicitement.
# `sibling_eval_id` est l'évaluation de l'autre rencontre de la même affiche :
# celle dont le score a été hérité par erreur. C'est la référence de la garde
# anti-recollision — après correction, les deux scores doivent DIFFÉRER.
TARGETS = [
    {"eval_id": 15, "sibling_eval_id": 9,
     "label": "Washington Mystics @ Golden State Valkyries (2026-07-21)"},
    {"eval_id": 45, "sibling_eval_id": 41,
     "label": "Toronto Tempo @ Golden State Valkyries (2026-08-05)"},
]


def _load(conn: sqlite3.Connection, eval_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT e.id AS eval_id, e.home_score, e.away_score, e.outcome,
               e.closing_odds, e.clv, e.clv_unit,
               v.verdict, v.selection, v.market, v.line,
               m.match_id, m.home_team, m.away_team, m.tipoff_utc
        FROM evaluations e
        JOIN verdicts v ON v.id = e.verdict_id
        JOIN matches  m ON m.match_id = v.match_id
        WHERE e.id = ?
        """,
        (eval_id,),
    ).fetchone()


def _immutable(row: sqlite3.Row) -> tuple:
    """Colonnes qui ne doivent PAS bouger (elles viennent des snapshots)."""
    return (row["closing_odds"], row["clv"], row["clv_unit"])


def run(conn: sqlite3.Connection, config: dict, settings, *, commit: bool) -> int:
    calendar_tz = config["results"]["calendar_timezone"]

    rows = {}
    for target in TARGETS:
        row = _load(conn, target["eval_id"])
        if row is None:
            print(f"✗ Évaluation {target['eval_id']} introuvable. Arrêt.", file=sys.stderr)
            return EXIT_ABORT
        rows[target["eval_id"]] = row

    # Une seule plage couvrant les deux matchs, élargie d'un jour de chaque côté.
    days = [date.fromisoformat(r["tipoff_utc"][:10]) for r in rows.values()]
    start = (min(days) - timedelta(days=1)).isoformat()
    end = (max(days) + timedelta(days=1)).isoformat()
    print(f"Lecture balldontlie sur {start} → {end} (fuseau {calendar_tz})…\n")

    client = ResultsApiClient.from_config(settings, config)
    try:
        games = client.get_games(start, end)
    finally:
        client.close()

    plans = []
    for target in TARGETS:
        row = rows[target["eval_id"]]
        expected = tipoff_calendar_date(row["tipoff_utc"], calendar_tz)
        found = find_result(games, home_team=row["home_team"], away_team=row["away_team"],
                            tipoff_utc=row["tipoff_utc"], calendar_tz=calendar_tz)

        if found is None:
            print(f"✗ {target['label']} : aucun résultat apparié. Arrêt.", file=sys.stderr)
            return EXIT_ABORT

        # Garde 1 — l'appariement doit être EXACT (écart nul), sinon l'ambiguïté qui
        # a causé le bug n'est pas levée et le recalcul serait aussi douteux que l'original.
        gap = abs((date.fromisoformat(found.game_date) - expected).days)
        if gap != 0:
            print(f"✗ {target['label']} : écart de date {gap} j (attendu 0). "
                  f"Ambiguïté non levée, arrêt.", file=sys.stderr)
            return EXIT_ABORT

        # Garde 2 — anti-recollision : le score corrigé doit DIFFÉRER de celui de
        # l'autre rencontre de la même affiche. S'ils restent identiques, le
        # ré-appariement a ramené le même match : anomalie, on s'arrête.
        sibling = _load(conn, target["sibling_eval_id"])
        if sibling is None:
            print(f"✗ Évaluation sœur {target['sibling_eval_id']} introuvable. Arrêt.",
                  file=sys.stderr)
            return EXIT_ABORT
        if (found.home_score, found.away_score) == (sibling["home_score"], sibling["away_score"]):
            print(f"✗ {target['label']} : score corrigé {found.home_score}-{found.away_score} "
                  f"IDENTIQUE à celui de la rencontre sœur (éval {sibling['eval_id']}). "
                  f"Le ré-appariement a ramené le même match. Arrêt.", file=sys.stderr)
            return EXIT_ABORT

        # Idempotence — une ligne déjà corrigée n'est pas réécrite.
        if (row["home_score"], row["away_score"]) == (found.home_score, found.away_score):
            print(f"• {target['label']} : déjà corrigée "
                  f"({found.home_score}-{found.away_score}), rien à faire.")
            continue

        outcome = grade_verdict(
            market=row["market"], selection=row["selection"], line=row["line"],
            home_team=row["home_team"], away_team=row["away_team"],
            home_score=found.home_score, away_score=found.away_score,
        )
        if outcome is None:
            print(f"✗ {target['label']} : `grade_verdict` rend None (sélection non "
                  f"notable). Arrêt plutôt qu'écrire un NULL métier.", file=sys.stderr)
            return EXIT_ABORT

        plans.append((target, row, found, outcome))

    if not plans:
        print("\nRien à corriger : les deux lignes sont déjà à jour.")
        return EXIT_OK

    _print_plan(plans)

    if not commit:
        print("\nSimulation — aucune écriture. Relancer avec --commit pour appliquer.")
        return EXIT_OK

    for target, row, found, outcome in plans:
        conn.execute(
            "UPDATE evaluations SET home_score = ?, away_score = ?, outcome = ? WHERE id = ?",
            (found.home_score, found.away_score, outcome, row["eval_id"]),
        )
    conn.commit()
    print("\n✓ Écriture appliquée. Relecture de contrôle :\n")
    return _verify_after(conn, plans)


def _print_plan(plans) -> None:
    print("\n" + "═" * 92)
    for target, row, found, outcome in plans:
        print(f"\n  {target['label']}  —  éval {row['eval_id']}  [{row['verdict']}]")
        line = "" if row["line"] is None else f" {row['line']:+g}"
        print(f"    sélection    : {row['selection']} ({row['market']}{line})")
        print(f"    score        : {row['home_score']}-{row['away_score']}"
              f"   →   {found.home_score}-{found.away_score}")
        print(f"    outcome      : {row['outcome']}   →   {outcome}")
        print(f"    date du match: {found.game_date} (écart nul avec le tip-off)")
        print("    ── inchangés (issus des snapshots, jamais de balldontlie) ──")
        print(f"    closing_odds : {row['closing_odds']}")
        print(f"    clv          : {row['clv']}  ({row['clv_unit']})")
    print("\n" + "═" * 92)


def _verify_after(conn: sqlite3.Connection, plans) -> int:
    """Relit les lignes écrites : les colonnes immuables doivent être identiques."""
    status = EXIT_OK
    for target, before, found, outcome in plans:
        after = _load(conn, before["eval_id"])
        ok_scores = (after["home_score"], after["away_score"]) == (found.home_score, found.away_score)
        ok_outcome = after["outcome"] == outcome
        ok_immutable = _immutable(after) == _immutable(before)
        print(f"  éval {after['eval_id']} : score {after['home_score']}-{after['away_score']} "
              f"| outcome {after['outcome']} | closing_odds {after['closing_odds']} "
              f"| clv {after['clv']} ({after['clv_unit']})")
        print(f"      scores écrits {'✓' if ok_scores else '✗'}  "
              f"outcome écrit {'✓' if ok_outcome else '✗'}  "
              f"colonnes CLV inchangées {'✓' if ok_immutable else '✗'}")
        if not (ok_scores and ok_outcome and ok_immutable):
            status = EXIT_ABORT
    return status


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true",
                        help="applique les corrections (sans ce drapeau : simulation)")
    args = parser.parse_args()

    settings = load_settings()
    db_path = Path(settings.database_path)
    if not db_path.exists():
        print(f"Base introuvable : {db_path}", file=sys.stderr)
        sys.exit(EXIT_ABORT)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        sys.exit(run(conn, load_config(), settings, commit=args.commit))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
