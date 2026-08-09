"""Pont entre le moteur de notes Elo (pur) et la base (chantier B5, lot 4).

`analyzer/ratings.py` ne connaît ni la base ni la configuration : il reçoit des états
et rend des états. `common/db.py` ne connaît pas l'Elo : il écrit des lignes. Ce module
est le seul à connaître les deux, et il tient trois responsabilités :

1. **construire le résolveur de clé** exigé par le contrat du lot 3 — un producteur de
   notes ne dérive jamais sa clé autrement ;
2. **reconstruire un `TeamState`** depuis la base, `recent_dates` compris ;
3. **écrire une `GameApplication`** (deux lignes d'historique, deux notes).

Il sert **les deux consommateurs** du lot 4 : le write path de l'évaluateur, qui écrit,
et le mode shadow de l'analyseur, qui ne fait que lire. Les faire passer par le même
chargement d'état est ce qui garantit que le modèle observé en production est celui qui
est effectivement entretenu.

Note de couches. Ce module importe `evaluator.reconcile` depuis `analyzer/`. Le graphe
reste acyclique (`reconcile` ne dépend que de `common`), mais le sens est inhabituel :
c'est la dette déjà journalisée au lot 2 (« `normalize_team` gagnerait à vivre dans
`common/` »). La déplacer touche l'évaluateur et le backfill, donc pas ici.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable
from datetime import date

from analyzer.model import EloParams
from analyzer.ratings import GameApplication, TeamState, rest_window_start
from common import db
from evaluator.reconcile import build_canonical_resolver, normalize_team, team_aliases


def build_resolver(
    conn: sqlite3.Connection, config: dict, sport: str
) -> Callable[[str], str]:
    """Résolveur de clé canonique pour une ligue, construit sur les équipes suivies.

    Strictement la même construction que le backfill (`scripts/backfill_elo.py`) : les
    alias viennent de la configuration, l'autorité des noms vient de `matches`. Deux
    producteurs qui construiraient ce résolveur différemment écriraient deux lignes pour
    la même équipe, et `UNIQUE(sport, team)` n'y pourrait rien.

    Lève `TeamContractError` à la construction si un alias vise une équipe inexistante.
    """
    return build_canonical_resolver(
        aliases=team_aliases(config, sport),
        known_teams=db.get_known_teams(conn, sport),
    )


def display_names(conn: sqlite3.Connection, sport: str) -> dict[str, str]:
    """Clé normalisée → nom d'affichage (celui de The Odds API, porté par `matches`).

    Même dérivation que le backfill : sans elle, les deux producteurs écriraient des
    libellés divergents dans `team_ratings.display_name` au gré de qui a mis à jour en
    dernier. Le libellé n'est pas la clé, mais un rapport illisible reste un défaut.
    """
    return {normalize_team(team): team for team in db.get_known_teams(conn, sport)}


def load_team_state(
    conn: sqlite3.Connection,
    sport: str,
    team_key: str,
    *,
    as_of: date,
    params: EloParams,
) -> TeamState:
    """Reconstruit l'état d'une équipe tel que le rejeu l'aurait à la veille de `as_of`.

    Une équipe sans note rend `TeamState.initial(params)` : c'est **l'appelant** qui
    décide qu'une équipe inconnue démarre à `initial_rating`, jamais la base
    (`db.get_team_rating` rend `None`, invariant 5). La nuance compte pour le shadow,
    qui doit distinguer « jamais vue » de « exactement à 1500 » et refuser de calculer
    un edge dans le premier cas.

    `recent_dates` est reconstruit depuis `rating_history` sur la fenêtre de
    `rest_window_start` — reconstruction *exacte*, démontrée dans la docstring de
    `db.get_recent_game_dates`, et verrouillée par un test qui la compare au tuple que
    `replay()` tient en mémoire.
    """
    row = db.get_team_rating(conn, sport, team_key)
    if row is None:
        return TeamState.initial(params)

    dates = db.get_recent_game_dates(
        conn,
        sport,
        team_key,
        since=rest_window_start(as_of).isoformat(),
        until=as_of.isoformat(),
    )
    stored_last = row["last_game_date"]
    return TeamState(
        rating=row["rating"],
        games_played=row["games_played"],
        last_game_date=date.fromisoformat(stored_last) if stored_last else None,
        recent_dates=tuple(date.fromisoformat(value) for value in dates),
    )


def load_states(
    conn: sqlite3.Connection,
    sport: str,
    team_keys: Iterable[str],
    *,
    as_of: date,
    params: EloParams,
) -> dict[str, TeamState]:
    """États de plusieurs équipes, pour alimenter `apply_game` ou `forecast`."""
    return {
        key: load_team_state(conn, sport, key, as_of=as_of, params=params)
        for key in team_keys
    }


def persist_application(
    conn: sqlite3.Connection,
    sport: str,
    application: GameApplication,
    *,
    source: str,
    stamp: str,
    match_id: str | None = None,
) -> None:
    """Écrit un match appliqué : deux lignes d'historique, deux notes mises à jour.

    Mêmes arguments que la boucle d'écriture du backfill — c'est voulu : les deux
    sources doivent produire des lignes indiscernables à la lecture, sans quoi la
    vérification de cohérence des deux chemins n'aurait rien à comparer.

    Ne committe pas (convention de `common/db.py`) : l'appelant décide de la frontière
    transactionnelle.
    """
    display = display_names(conn, sport)
    for outcome in application.outcomes():
        db.insert_rating_history(
            conn,
            sport=sport,
            team=outcome.key,
            game_date=application.game_date.isoformat(),
            source=source,
            source_game_id=application.game_id,
            match_id=match_id,
            opponent=outcome.opponent,
            is_home=outcome.is_home,
            rating_before=outcome.rating_before,
            rating_after=outcome.rating_after,
            expected_win=outcome.expected_win,
            created_at=stamp,
        )
        db.upsert_team_rating(
            conn,
            sport=sport,
            team=outcome.key,
            display_name=display.get(outcome.key, outcome.display_name),
            rating=outcome.rating_after,
            games_played=outcome.games_played_before + 1,
            last_game_date=application.game_date.isoformat(),
            updated_at=stamp,
        )
