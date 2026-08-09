"""Opinion du modèle Elo en lecture seule (chantier B5, lot 4, mode shadow).

Au moment du verdict, le modèle calcule sa probabilité (`p_model`), la confronte au prix
du marché (`p_market`) et journalise l'écart. **Il n'influence rien** : ni le verdict, ni
la sélection, ni les alertes, ni un seuil. L'objectif est d'accumuler des observations
sur de vrais matchs avant de décider si cet edge mérite le moindre pouvoir.

Pourquoi ce module n'écrit rien
-------------------------------
Pas de table dédiée, pas de colonne : une ligne de log suffit à observer, et concevoir un
schéma avant de savoir si la donnée a un sens reviendrait à figer une conception sur une
hypothèse non vérifiée. Si l'observation confirme que l'edge est exploitable, la table
viendra à l'activation, dessinée en connaissance de cause.

Deux invariants portés ici
--------------------------
**Jamais de 1500 implicite.** `db.get_team_rating` rend `None` pour une équipe jamais
notée. Le remplacer par la note de départ produirait un edge calculé sur une force
inventée — exactement la famille du bug d'origine du projet (invariant 5). Une équipe
sans note donne `reason`, pas un chiffre.

**Jamais d'edge sur des notes immatures.** Sous `min_games_for_edge`, la note mesure
surtout l'avantage du terrain et le hasard des premiers matchs. On le dit plutôt que de
produire un nombre auquel personne ne devrait se fier.
"""
from __future__ import annotations

import sqlite3

from analyzer.model import edge_prob, params_from_config
from analyzer.preprocessing import MatchData
from analyzer.ratings import forecast
from analyzer.ratings_store import load_team_state
from common import db
from evaluator.reconcile import normalize_team, tipoff_calendar_date

_PREFIX = "SHADOW"


def _line(fields: dict[str, object]) -> str:
    """Ligne de log à plat, greppable et stable dans l'ordre des champs."""
    return _PREFIX + " " + " ".join(f"{key}={value}" for key, value in fields.items())


def observe(
    conn: sqlite3.Connection,
    match: sqlite3.Row,
    data: MatchData,
    config: dict,
    *,
    verdict_id: int | None = None,
    verdict: str | None = None,
) -> str:
    """Calcule `p_model`, `p_market` et l'edge, et rend la ligne de log correspondante.

    N'écrit rien, ne lève rien de métier : toute impossibilité de conclure est rendue
    sous forme d'un champ `reason`, parce qu'une observation manquante est elle-même une
    observation (savoir *pourquoi* l'edge n'est pas calculable vaut mieux que du silence).
    """
    sport = match["sport"]
    home, away = match["home_team"], match["away_team"]
    base: dict[str, object] = {
        "verdict_id": verdict_id if verdict_id is not None else "-",
        "sport": sport,
        "away": f"'{away}'",
        "home": f"'{home}'",
    }
    if verdict is not None:
        base["verdict"] = verdict

    def refuse(reason: str, **extra: object) -> str:
        return _line({**base, **extra, "reason": f"'{reason}'"})

    params = params_from_config(config, sport=sport)
    min_games = int(config["model"]["decision"]["min_games_for_edge"])
    calendar_tz = config["results"]["calendar_timezone"]

    # Égalité stricte sur la clé canonique : `home`/`away` viennent de `matches`, donc
    # de The Odds API, qui est justement l'autorité de cette clé (contrat du lot 3).
    home_key, away_key = normalize_team(home), normalize_team(away)

    home_row = db.get_team_rating(conn, sport, home_key)
    away_row = db.get_team_rating(conn, sport, away_key)
    missing = [
        key for key, row in ((home_key, home_row), (away_key, away_row)) if row is None
    ]
    if missing:
        return refuse(f"note absente pour {', '.join(repr(k) for k in missing)}")

    games_home, games_away = home_row["games_played"], away_row["games_played"]
    counts = {"games": f"{games_home}/{games_away}"}
    if min(games_home, games_away) < min_games:
        return refuse(
            f"notes immatures ({min(games_home, games_away)} < {min_games})", **counts
        )

    # Date calendaire US du match : même convention que `rating_history.game_date`
    # depuis le correctif de fuseau du lot 1b, sans quoi le repos serait faux d'un jour.
    game_date = tipoff_calendar_date(match["tipoff_utc"], calendar_tz)
    home_state = load_team_state(conn, sport, home_key, as_of=game_date, params=params)
    away_state = load_team_state(conn, sport, away_key, as_of=game_date, params=params)

    # Même primitive que le rejeu et que le hold-out du lot 3 : la probabilité observée
    # en production est produite par le calcul dont la vraisemblance a été mesurée.
    prediction = forecast(home_state, away_state, game_date, params)

    # `p_market` doit être du MÊME côté que `p_model` (domicile) : soustraire deux
    # probabilités de camps opposés donnerait un edge de signe arbitraire.
    times = data.times()
    point = (
        data.consensus_at("h2h", home, times[-1]) if times else None
    )
    if point is None:
        return refuse(
            "p_market indisponible (h2h absent du consensus au dernier relevé)",
            **counts,
        )

    edge = edge_prob(prediction.expected_home, point.prob)
    return _line({
        **base,
        "p_model": f"{prediction.expected_home:.4f}",
        "p_market": f"{point.prob:.4f}",
        "edge": f"{edge:+.4f}",
        **counts,
        "ratings": f"{home_row['rating']:.1f}/{away_row['rating']:.1f}",
        "rest": f"{prediction.home_days_rest}/{prediction.away_days_rest}",
        "n_books": point.n_books,
    })
