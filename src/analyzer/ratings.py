"""Rejeu chronologique des notes Elo (chantier B5, lot 3).

Raison d'être. `model.py` sait appliquer **un** résultat à **deux** notes. Il ne sait
rien du calendrier : ni combien de jours une équipe a eu pour récupérer, ni combien de
matchs elle a déjà joués. Ce module tient cet état et le fait avancer match après match,
dans l'ordre. C'est le moteur du backfill de saison, et ce sera le même moteur que
l'évaluateur appellera pour un match unique quand le write path sera branché (spec §5.3)
— d'où sa place ici, dans `src/`, plutôt que dans le script qui le consomme aujourd'hui :
une seconde implémentation dériverait de la première.

**Module pur.** Aucune base, aucun réseau, aucun état global, aucune horloge. Tout ce
qui entre est un argument, tout ce qui sort est une valeur.

Trois choix de conception qui méritent d'être vus plutôt que subis
------------------------------------------------------------------
**La clé d'équipe est injectée.** `replay()` reçoit une fonction `normalize`. Le moteur
ne décide donc jamais sous quel nom une équipe est comptabilisée : c'est l'appelant qui
l'affirme, au même endroit où il écrira en base. La normalisation canonique du projet
vit dans `evaluator.reconcile.normalize_team`, et `analyzer/` n'a pas à en dépendre pour
faire de l'arithmétique.

**Le facteur K suit la moins mûre des deux équipes** (`min` des `games_played`). Le
burn-in existe parce qu'une note fraîche vaut peu ; prendre le `max` ferait sortir du
régime accéléré une équipe qui n'a joué que deux matchs, au motif que son adversaire en
a joué trente.

**Les notes stockées restent brutes.** L'ajustement de repos ne vit que dans
`contextual_home_advantage`, le temps d'un match, et n'est jamais ajouté à un `rating`.
Une note en base mesure une force, pas un état de fatigue — sans quoi le malus se
propagerait à tous les matchs suivants.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta

from analyzer.model import (
    EloParams,
    contextual_home_advantage,
    expected_home_win,
    k_for,
    update_ratings,
)
from common.results_api_client import GameResult

# Fenêtre du malus « 3 matchs en 4 nuits » : le match courant plus les 3 nuits qui
# le précèdent (cf. `model.rest_adjustment`, où le match courant est inclus).
_FOUR_NIGHT_WINDOW = 3


class ReplayError(Exception):
    """Donnée de calendrier incohérente rencontrée pendant le rejeu."""


@dataclass(frozen=True)
class TeamState:
    """État d'une équipe à un instant du rejeu.

    `recent_dates` ne conserve que les dates encore utiles au calcul du 3-en-4 : la
    liste est élaguée à chaque match, elle ne grandit donc pas avec la saison.
    """

    rating: float
    games_played: int
    last_game_date: date | None
    recent_dates: tuple[date, ...]

    @classmethod
    def initial(cls, params: EloParams) -> TeamState:
        """État d'une équipe jamais vue : note de départ, aucun historique.

        `last_game_date=None` est un « jamais joué » explicite, pas une date par
        défaut — c'est ce qui fait rendre `None` au repos plutôt qu'un nombre inventé.
        """
        return cls(
            rating=params.initial_rating,
            games_played=0,
            last_game_date=None,
            recent_dates=(),
        )


@dataclass(frozen=True)
class TeamOutcome:
    """Ce qu'un match a fait à **une** équipe — la forme d'une ligne `rating_history`."""

    key: str                    # nom normalisé : la clé d'écriture
    display_name: str           # nom réel, tel que rendu par la source
    opponent: str               # nom réel de l'adversaire (informatif)
    is_home: bool
    rating_before: float
    rating_after: float
    expected_win: float         # probabilité AVANT le match, du point de vue de l'équipe
    days_rest: int | None
    games_in_four_nights: int | None
    # Matchs déjà intégrés AVANT celui-ci. Sert au filtre de maturité des mesures :
    # les premières semaines, où tout le monde est encore à 1500, ne mesureraient que
    # l'avantage du terrain et pollueraient la log-loss.
    games_played_before: int = 0


@dataclass(frozen=True)
class GameApplication:
    """Trace complète de l'application d'un match, prête à être écrite ou mesurée."""

    game_date: date
    game_id: str | None
    home: TeamOutcome
    away: TeamOutcome
    home_advantage: float
    k: float
    mov_multiplier: float
    home_score: int
    away_score: int

    @property
    def expected_home(self) -> float:
        """Probabilité de victoire à domicile prédite avant le match."""
        return self.home.expected_win

    @property
    def home_won(self) -> bool:
        return self.home_score > self.away_score

    def outcomes(self) -> tuple[TeamOutcome, TeamOutcome]:
        """Les deux perspectives, dans un ordre stable (domicile puis extérieur)."""
        return (self.home, self.away)


def parse_game_date(value: str) -> date:
    """Convertit la date calendaire d'un `GameResult` ('YYYY-MM-DD').

    La conversion de fuseau a déjà eu lieu dans le client (`_game_date`, correctif du
    lot 1b) : ce module ne tronque ni ne convertit jamais une date lui-même, sous peine
    de réintroduire le décalage d'un jour des matchs du soir.
    """
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ReplayError(f"Date de match illisible : {value!r}.") from exc


def unusable_reason(game: GameResult) -> str | None:
    """Raison pour laquelle un match ne peut pas alimenter les notes, ou `None`.

    Un match déclaré terminé dont les deux scores sont **égaux** est une donnée
    corrompue, pas un cas limite : il n'y a pas de match nul en basket. Le cas dominant
    est le `0-0` d'un match annoncé `post` que la source n'a jamais renseigné — c'est le
    **bug d'origine du projet** (invariant 5 : « un score 0-0 gradé *push* au lieu
    d'être traité comme résultat absent »), et l'évaluateur porte déjà une garde
    équivalente.

    Le filtre est ici volontairement **plus large** que celui de l'évaluateur, qui ne
    regarde que le `0-0` : un hypothétique `55-55` déclaré terminé serait tout aussi
    inutilisable, et cadrer la garde sur la propriété métier (« pas de nul ») plutôt
    que sur la valeur observée évite de refaire le tour du problème au prochain
    symptôme.

    Rendre une raison plutôt qu'un booléen : une exclusion doit pouvoir être **comptée
    et nommée** dans un rapport, jamais silencieuse (invariant 6).
    """
    if not game.is_final:
        return f"statut non terminal ({game.status!r})"
    if game.home_score == game.away_score:
        return (
            f"score nul {game.home_score}-{game.away_score} sur un match déclaré "
            f"terminé — donnée corrompue, aucun match nul n'existe en basket"
        )
    return None


def sort_games(games: Iterable[GameResult]) -> list[GameResult]:
    """Trie les matchs pour le rejeu : par date, puis par identifiant.

    `game_date` est une date calendaire sans heure : l'ordre à l'intérieur d'une même
    journée est arbitraire. `game_id` ne le rend pas *juste*, il le rend
    **déterministe** — deux exécutions produisent exactement les mêmes notes.

    Cet arbitraire est sans effet sur la séquence propre à une équipe (elle ne joue
    jamais deux fois le même jour) ; il ne joue qu'à travers la note d'un adversaire
    mise à jour le même soir. Effet du second ordre, documenté plutôt que faussement
    corrigé.
    """
    return sorted(games, key=lambda game: (game.game_date, game.game_id or ""))


def rest_context(state: TeamState, game_date: date) -> tuple[int | None, int | None]:
    """Jours de repos et nombre de matchs sur 4 nuits, pour un match à `game_date`.

    Rend `(None, None)` quand l'équipe n'a aucun historique : on ne sait pas si elle
    a joué avant le début de la fenêtre observée, et fabriquer « bien reposée » ou
    « 1 match en 4 nuits » serait affirmer ce qu'on ignore (invariant 5). Le modèle
    traite ce `None` comme l'élément neutre, jamais comme une valeur de repli.

    Le comptage 4 nuits **inclut le match courant**, conformément au contrat de
    `model.rest_adjustment` — c'est donc 3 qui déclenche le malus.
    """
    if state.last_game_date is None:
        return (None, None)

    days_rest = (game_date - state.last_game_date).days
    if days_rest < 0:
        raise ReplayError(
            f"Match daté du {game_date} alors que le précédent est daté du "
            f"{state.last_game_date} : les matchs doivent être rejoués dans l'ordre "
            f"chronologique, sans quoi les notes intègrent le futur."
        )

    window_start = game_date - timedelta(days=_FOUR_NIGHT_WINDOW)
    previous = sum(1 for played in state.recent_dates if window_start <= played <= game_date)
    return (days_rest, previous + 1)


def _advance(state: TeamState, game_date: date, rating_after: float) -> TeamState:
    """État de l'équipe après le match : note, compteur, et fenêtre élaguée."""
    window_start = game_date - timedelta(days=_FOUR_NIGHT_WINDOW)
    kept = tuple(played for played in state.recent_dates if played >= window_start)
    return replace(
        state,
        rating=rating_after,
        games_played=state.games_played + 1,
        last_game_date=game_date,
        recent_dates=kept + (game_date,),
    )


def apply_game(
    states: Mapping[str, TeamState],
    game: GameResult,
    params: EloParams,
    *,
    normalize: Callable[[str], str],
) -> tuple[dict[str, TeamState], GameApplication]:
    """Applique un match aux notes et rend les nouveaux états plus la trace.

    `states` n'est pas modifié : un dictionnaire neuf est renvoyé. Le rejeu reste ainsi
    rejouable et testable pas à pas, et une exception en cours de route ne laisse pas
    un état à moitié avancé.

    Une équipe inconnue démarre à `params.initial_rating`. Un match nul lève, via
    `model.mov_multiplier` : il n'y a pas de match nul en basket, c'est une donnée
    corrompue — même famille que le garde-fou des scores 0-0 de l'évaluateur.
    """
    game_date = parse_game_date(game.game_date)
    home_key = normalize(game.home_team)
    away_key = normalize(game.away_team)
    if home_key == away_key:
        raise ReplayError(
            f"Match du {game.game_date} opposant une équipe à elle-même après "
            f"normalisation : {game.home_team!r} et {game.away_team!r} donnent tous "
            f"deux la clé {home_key!r}."
        )

    home_state = states.get(home_key) or TeamState.initial(params)
    away_state = states.get(away_key) or TeamState.initial(params)

    home_rest, home_four = rest_context(home_state, game_date)
    away_rest, away_four = rest_context(away_state, game_date)

    advantage = contextual_home_advantage(
        home_days_rest=home_rest,
        home_games_in_four_nights=home_four,
        away_days_rest=away_rest,
        away_games_in_four_nights=away_four,
        params=params,
    )
    # Le régime accéléré vaut tant que l'une des deux notes est encore immature.
    k = k_for(min(home_state.games_played, away_state.games_played), params)

    update = update_ratings(
        rating_home=home_state.rating,
        rating_away=away_state.rating,
        home_score=game.home_score,
        away_score=game.away_score,
        home_advantage=advantage,
        k=k,
    )

    updated = dict(states)
    updated[home_key] = _advance(home_state, game_date, update.home_after)
    updated[away_key] = _advance(away_state, game_date, update.away_after)

    application = GameApplication(
        game_date=game_date,
        game_id=game.game_id,
        home=TeamOutcome(
            key=home_key,
            display_name=game.home_team,
            opponent=game.away_team,
            is_home=True,
            rating_before=home_state.rating,
            rating_after=update.home_after,
            expected_win=update.expected_home,
            days_rest=home_rest,
            games_in_four_nights=home_four,
            games_played_before=home_state.games_played,
        ),
        away=TeamOutcome(
            key=away_key,
            display_name=game.away_team,
            opponent=game.home_team,
            is_home=False,
            rating_before=away_state.rating,
            rating_after=update.away_after,
            # Complément exact : les deux probabilités somment à 1 par construction.
            expected_win=1.0 - update.expected_home,
            days_rest=away_rest,
            games_in_four_nights=away_four,
            games_played_before=away_state.games_played,
        ),
        home_advantage=advantage,
        k=k,
        mov_multiplier=update.mov_multiplier,
        home_score=game.home_score,
        away_score=game.away_score,
    )
    return updated, application


def predict_only(
    states: Mapping[str, TeamState],
    game: GameResult,
    params: EloParams,
    *,
    normalize: Callable[[str], str],
) -> tuple[dict[str, TeamState], float]:
    """Prédit un match **sans apprendre** : les notes sont gelées, le calendrier avance.

    Sert au test hold-out. Geler aussi le calendrier serait infidèle : en production, au
    moment de prédire le match de ce soir, on connaît parfaitement la date du dernier
    match de chaque équipe — le repos est une donnée *observable*, pas un paramètre
    appris. Seules les notes doivent rester à leur valeur de la coupure ; c'est elles
    qu'on met à l'épreuve.

    Rend les états avancés (date, fenêtre 4 nuits, compteur) et la probabilité prédite
    pour l'équipe à domicile.
    """
    game_date = parse_game_date(game.game_date)
    home_key = normalize(game.home_team)
    away_key = normalize(game.away_team)
    home_state = states.get(home_key) or TeamState.initial(params)
    away_state = states.get(away_key) or TeamState.initial(params)

    home_rest, home_four = rest_context(home_state, game_date)
    away_rest, away_four = rest_context(away_state, game_date)
    advantage = contextual_home_advantage(
        home_days_rest=home_rest,
        home_games_in_four_nights=home_four,
        away_days_rest=away_rest,
        away_games_in_four_nights=away_four,
        params=params,
    )
    expected = expected_home_win(home_state.rating, away_state.rating,
                                 home_advantage=advantage)

    advanced = dict(states)
    advanced[home_key] = _advance(home_state, game_date, home_state.rating)
    advanced[away_key] = _advance(away_state, game_date, away_state.rating)
    return advanced, expected


def replay(
    games: Sequence[GameResult],
    params: EloParams,
    *,
    normalize: Callable[[str], str],
    states: Mapping[str, TeamState] | None = None,
) -> tuple[dict[str, TeamState], list[GameApplication]]:
    """Rejoue une séquence de matchs dans l'ordre chronologique.

    Le tri est fait **ici**, pas laissé à l'appelant : un rejeu dans le désordre
    produirait des notes qui intègrent le futur, sans qu'aucune erreur ne le signale.

    `states` permet de repartir d'un état existant (rejeu incrémental). Par défaut le
    rejeu part de zéro, toutes les équipes à `initial_rating`.
    """
    current: dict[str, TeamState] = dict(states or {})
    applications: list[GameApplication] = []
    for game in sort_games(games):
        current, application = apply_game(current, game, params, normalize=normalize)
        applications.append(application)
    return current, applications
