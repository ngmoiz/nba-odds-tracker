"""Tests du moteur de rejeu chronologique des notes Elo (B5 lot 3).

Module pur : aucune base, aucun réseau, aucune horloge. Ce qui est verrouillé ici, ce
sont les propriétés dont dépend la justesse d'un rejeu de saison entière — une note
fausse au 12ᵉ match se propage silencieusement aux 250 suivants, aucun test d'intégration
ne la rattraperait.

La normalisation de clé est **injectée** : ces tests passent `normalize_team`, la même
fonction que le script d'écriture, pour que le contrat vérifié soit celui de production.
"""
from __future__ import annotations

from datetime import date

import pytest

from analyzer.model import params_from_config
from analyzer.ratings import (
    GameApplication,
    ReplayError,
    TeamState,
    apply_game,
    parse_game_date,
    predict_only,
    replay,
    rest_context,
    sort_games,
    forecast,
    rest_window_start,
    unusable_reason,
)
from common.config import load_config
from common.results_api_client import GameResult
from evaluator.reconcile import normalize_team

CFG = load_config()
P = params_from_config(CFG)

HOME, AWAY, THIRD = "Las Vegas Aces", "Seattle Storm", "New York Liberty"


def game(day: str, home: str, away: str, home_score: int, away_score: int,
         game_id: str | None = None) -> GameResult:
    """Match terminé minimal — les champs non pertinents gardent leur valeur neutre."""
    return GameResult(
        game_date=day, status="post", home_team=home, away_team=away,
        home_score=home_score, away_score=away_score,
        game_id=game_id or f"{day}-{home[:3]}-{away[:3]}",
    )


def run(games, states=None):
    return replay(games, P, normalize=normalize_team, states=states)


# ───────────────────────── état initial et invariants ─────────────────────────


def test_unknown_team_starts_at_initial_rating():
    """Une équipe jamais vue démarre à `initial_rating`, avec un historique vide.

    `last_game_date=None` est un « jamais joué » explicite : c'est lui qui fera rendre
    `None` au repos plutôt qu'un nombre inventé (invariant 5).
    """
    state = TeamState.initial(P)
    assert state.rating == P.initial_rating
    assert state.games_played == 0
    assert state.last_game_date is None
    assert state.recent_dates == ()


def test_replay_is_zero_sum_and_preserves_league_mean():
    """Chaque match redistribue les points sans en créer : la moyenne reste à 1500.

    C'est la propriété que le rejeu ne doit jamais violer (`update_ratings` est à somme
    nulle). Si elle casse, toutes les notes dérivent ensemble et l'edge devient faux
    sans qu'aucun symptôme visible n'apparaisse.
    """
    states, _ = run([
        game("2026-05-02", HOME, AWAY, 88, 80),
        game("2026-05-05", AWAY, THIRD, 91, 77),
        game("2026-05-09", THIRD, HOME, 70, 95),
    ])
    assert len(states) == 3
    mean = sum(state.rating for state in states.values()) / len(states)
    assert mean == pytest.approx(P.initial_rating, abs=1e-9)


def test_counters_match_the_number_of_games_played():
    """`games_played` compte les matchs de chaque équipe, pas ceux du rejeu."""
    states, applications = run([
        game("2026-05-02", HOME, AWAY, 88, 80),
        game("2026-05-05", AWAY, THIRD, 91, 77),
        game("2026-05-09", THIRD, HOME, 70, 95),
    ])
    assert len(applications) == 3
    assert sum(state.games_played for state in states.values()) == 2 * len(applications)
    assert states[normalize_team(HOME)].games_played == 2
    assert states[normalize_team(THIRD)].games_played == 2


def test_winner_gains_exactly_what_the_loser_loses():
    _, applications = run([game("2026-05-02", HOME, AWAY, 88, 80)])
    application = applications[0]
    gained = application.home.rating_after - application.home.rating_before
    lost = application.away.rating_before - application.away.rating_after
    assert gained == pytest.approx(lost, abs=1e-12)
    assert gained > 0


def test_expected_probabilities_of_both_sides_sum_to_one():
    _, applications = run([game("2026-05-02", HOME, AWAY, 88, 80)])
    application = applications[0]
    assert application.home.expected_win + application.away.expected_win == pytest.approx(1.0)


def test_states_are_not_mutated_in_place():
    """`apply_game` rend un dictionnaire neuf : un rejeu interrompu ne laisse pas
    d'état à moitié avancé, et le pas à pas reste rejouable."""
    before = {normalize_team(HOME): TeamState.initial(P)}
    after, _ = apply_game(before, game("2026-05-02", HOME, AWAY, 88, 80), P,
                          normalize=normalize_team)
    assert before[normalize_team(HOME)].games_played == 0
    assert after[normalize_team(HOME)].games_played == 1
    assert len(before) == 1


# ───────────────────────────── ordre chronologique ─────────────────────────────


def test_games_are_sorted_before_replay_whatever_the_input_order():
    """Un rejeu dans le désordre intégrerait le futur, sans qu'aucune erreur ne le dise.

    Le tri est donc fait dans `replay`, pas laissé à l'appelant : deux ordres d'entrée
    doivent produire exactement les mêmes notes finales.
    """
    ordered = [
        game("2026-05-02", HOME, AWAY, 88, 80),
        game("2026-05-05", AWAY, THIRD, 91, 77),
        game("2026-05-09", THIRD, HOME, 70, 95),
    ]
    shuffled = [ordered[2], ordered[0], ordered[1]]
    from_ordered, _ = run(ordered)
    from_shuffled, _ = run(shuffled)
    assert {k: v.rating for k, v in from_ordered.items()} == {
        k: v.rating for k, v in from_shuffled.items()
    }


def test_intra_day_order_is_deterministic_via_game_id():
    """À date égale, `game_id` départage — le rejeu doit être reproductible à l'identique."""
    same_day = [
        game("2026-05-02", HOME, AWAY, 88, 80, game_id="b"),
        game("2026-05-02", THIRD, "Chicago Sky", 70, 60, game_id="a"),
    ]
    assert [g.game_id for g in sort_games(same_day)] == ["a", "b"]
    assert [g.game_id for g in sort_games(list(reversed(same_day)))] == ["a", "b"]


def test_out_of_order_replay_fails_loudly():
    """Un match antérieur au dernier appliqué lève au lieu de produire un repos négatif."""
    states, _ = run([game("2026-05-09", HOME, AWAY, 88, 80)])
    with pytest.raises(ReplayError, match="ordre chronologique"):
        apply_game(states, game("2026-05-02", HOME, THIRD, 70, 60), P,
                   normalize=normalize_team)


def test_parse_game_date_rejects_a_malformed_date():
    with pytest.raises(ReplayError, match="illisible"):
        parse_game_date("05/02/2026")


def test_a_team_facing_itself_after_normalization_fails_loudly():
    """Deux noms qui se réduisent à la même clé signalent un problème d'appariement,
    jamais un match : les traiter silencieusement corromprait la note de l'équipe."""
    with pytest.raises(ReplayError, match="elle-même"):
        apply_game({}, game("2026-05-02", "Las Vegas Aces", "  LAS VEGAS   ACES ", 88, 80),
                   P, normalize=normalize_team)


# ───────────────────────────── repos et fatigue ─────────────────────────────


def test_first_game_of_a_team_has_no_rest_information():
    """Sans historique, le repos est `None` — jamais « bien reposée » par défaut.

    On ignore si l'équipe a joué avant le début de la fenêtre observée ; affirmer
    l'un ou l'autre serait fabriquer de la donnée (invariant 5).
    """
    assert rest_context(TeamState.initial(P), date(2026, 5, 2)) == (None, None)


def test_back_to_back_is_detected_across_an_evening_game():
    """Un enchaînement soir → soir donne 1 jour de repos, pas 2.

    C'est exactement le cas que le correctif de fuseau du lot 1b a rendu possible : la
    date balldontlie d'un match de soirée (`T02:00Z`) est convertie vers le calendrier
    US avant d'arriver ici. Si la conversion sautait, ce test verrait 2 jours de repos
    et le malus `back_to_back` (−50) disparaîtrait silencieusement.
    """
    states, _ = run([game("2026-08-04", HOME, AWAY, 88, 80)])
    days_rest, games_in_four = rest_context(states[normalize_team(HOME)], date(2026, 8, 5))
    assert days_rest == 1
    assert games_in_four == 2


def test_three_games_in_four_nights_counts_the_current_game():
    """La fenêtre se termine par le match courant, celui-ci inclus : c'est 3 qui déclenche.

    Contrat de `model.rest_adjustment` — un décalage d'une unité ici appliquerait le
    malus un match trop tôt ou trop tard sur toute la saison.
    """
    states, _ = run([
        game("2026-08-02", HOME, AWAY, 88, 80),
        game("2026-08-04", THIRD, HOME, 70, 90),
    ])
    days_rest, games_in_four = rest_context(states[normalize_team(HOME)], date(2026, 8, 5))
    assert days_rest == 1
    assert games_in_four == 3


def test_rest_window_forgets_games_older_than_four_nights():
    """La fenêtre glissante est élaguée : un match d'il y a une semaine ne compte plus."""
    states, _ = run([
        game("2026-07-20", HOME, AWAY, 88, 80),
        game("2026-08-04", THIRD, HOME, 70, 90),
    ])
    state = states[normalize_team(HOME)]
    assert state.recent_dates == (date(2026, 8, 4),)
    assert rest_context(state, date(2026, 8, 5)) == (1, 2)


def test_rest_penalty_lowers_the_home_advantage_without_touching_the_ratings():
    """Le malus de fatigue vit dans l'avantage contextuel, jamais dans la note stockée.

    Si le rejeu l'ajoutait au `rating`, la fatigue d'un soir se propagerait à tous les
    matchs suivants de l'équipe — le double comptage que la conception du modèle rend
    structurellement impossible.
    """
    # Domicile en back-to-back, extérieur reposé.
    tired, _ = run([game("2026-08-04", HOME, "Chicago Sky", 88, 80)])
    _, with_fatigue = run([game("2026-08-05", HOME, AWAY, 88, 80)], states=tired)
    _, without_fatigue = run([game("2026-08-05", HOME, AWAY, 88, 80)])

    assert with_fatigue[0].home_advantage < without_fatigue[0].home_advantage
    # La note d'entrée du match fatigué est bien la note brute d'après le match
    # précédent : aucun malus ne s'y est glissé.
    assert with_fatigue[0].home.rating_before == pytest.approx(
        tired[normalize_team(HOME)].rating
    )


def test_rest_context_is_reported_on_the_application_for_traceability():
    states, _ = run([game("2026-08-04", HOME, AWAY, 88, 80)])
    _, applications = run([game("2026-08-05", HOME, THIRD, 90, 70)], states=states)
    assert applications[0].home.days_rest == 1
    assert applications[0].home.games_in_four_nights == 2
    assert applications[0].away.days_rest is None


# ───────────────────────────── facteur K ─────────────────────────────


def test_k_follows_the_least_mature_of_the_two_teams():
    """Le régime accéléré vaut tant que l'UNE des deux notes est encore immature.

    Prendre le `max` ferait sortir du burn-in une équipe qui n'a joué que deux matchs
    au motif que son adversaire en a joué trente — sa note resterait fausse longtemps.
    """
    veteran = TeamState(rating=1500.0, games_played=P.burnin_games + 5,
                        last_game_date=None, recent_dates=())
    rookie = TeamState.initial(P)
    states = {normalize_team(HOME): veteran, normalize_team(AWAY): rookie}
    _, application = apply_game(states, game("2026-08-05", HOME, AWAY, 88, 80), P,
                                normalize=normalize_team)
    assert application.k == P.k_factor_burnin


def test_k_settles_once_both_teams_are_mature():
    mature = TeamState(rating=1500.0, games_played=P.burnin_games,
                       last_game_date=None, recent_dates=())
    states = {normalize_team(HOME): mature, normalize_team(AWAY): mature}
    _, application = apply_game(states, game("2026-08-05", HOME, AWAY, 88, 80), P,
                                normalize=normalize_team)
    assert application.k == P.k_factor


# ───────────────────────── forme des traces produites ─────────────────────────


def test_application_exposes_both_perspectives_ready_for_rating_history():
    """Chaque match produit exactement deux lignes d'historique, une par équipe.

    Les clés sont normalisées (c'est ce que `_require_normalized_team` exige au point
    d'écriture) tandis que `display_name` conserve la forme réelle.
    """
    _, applications = run([game("2026-05-02", HOME, AWAY, 88, 80)])
    outcomes = applications[0].outcomes()
    assert len(outcomes) == 2
    assert [outcome.is_home for outcome in outcomes] == [True, False]
    for outcome in outcomes:
        assert outcome.key == normalize_team(outcome.display_name)
        assert 0.0 < outcome.expected_win < 1.0
    assert outcomes[0].opponent == AWAY
    assert outcomes[1].opponent == HOME


def test_application_reports_the_score_and_the_winner():
    _, applications = run([game("2026-05-02", HOME, AWAY, 80, 88)])
    application = applications[0]
    assert isinstance(application, GameApplication)
    assert (application.home_score, application.away_score) == (80, 88)
    assert application.home_won is False
    assert application.expected_home == application.home.expected_win


def test_a_zero_zero_final_is_reported_as_unusable_not_replayed():
    """Le bug d'origine du projet, rencontré pour de vrai dans les données de production.

    balldontlie annonce `status='post'` avec `0-0`, `period=0` sur un match jamais
    renseigné (id 24935, Dallas Wings vs New York Liberty du 2026-07-16). Le rejeu doit
    l'écarter en le NOMMANT, jamais l'appliquer : deux équipes recevraient une mise à
    jour fabriquée à partir d'un résultat qui n'existe pas (invariant 5).
    """
    reason = unusable_reason(game("2026-07-16", "Dallas Wings", "New York Liberty", 0, 0))
    assert reason is not None
    assert "corrompue" in reason


def test_the_tie_guard_is_wider_than_the_zero_zero_case():
    """Cadrée sur la propriété métier (« pas de nul »), pas sur la valeur observée.

    Un `55-55` déclaré terminé est tout aussi inutilisable qu'un `0-0` ; viser la seule
    valeur `0-0` obligerait à refaire le tour du problème au prochain symptôme.
    """
    assert unusable_reason(game("2026-07-16", HOME, AWAY, 55, 55)) is not None


def test_a_normal_final_game_is_usable():
    assert unusable_reason(game("2026-07-16", HOME, AWAY, 88, 80)) is None


def test_a_non_final_game_is_reported_with_its_status():
    unplayed = GameResult(game_date="2026-08-09", status="pre", home_team=HOME,
                          away_team=AWAY, home_score=0, away_score=0, game_id="x")
    reason = unusable_reason(unplayed)
    assert reason is not None and "pre" in reason


def test_prediction_without_learning_freezes_ratings_but_advances_the_calendar():
    """Hold-out fidèle : les notes sont gelées, le repos reste observable.

    Geler aussi le calendrier serait infidèle — en production, au moment de prédire le
    match de ce soir, on connaît parfaitement la date du dernier match de chaque équipe.
    Seules les notes sont mises à l'épreuve.
    """
    states, _ = run([game("2026-08-04", HOME, AWAY, 88, 80)])
    before = states[normalize_team(HOME)]

    advanced, expected = predict_only(states, game("2026-08-05", HOME, THIRD, 90, 70), P,
                                      normalize=normalize_team)
    after = advanced[normalize_team(HOME)]

    assert after.rating == before.rating                 # note gelée
    assert after.games_played == before.games_played + 1  # calendrier avancé
    assert after.last_game_date == date(2026, 8, 5)
    assert 0.0 < expected < 1.0


def test_a_tie_is_rejected_rather_than_graded():
    """Il n'y a pas de match nul en basket : c'est une donnée corrompue, pas un cas limite.

    Même famille que le garde-fou des scores 0-0 de l'évaluateur — le bug d'origine du
    projet était précisément un 0-0 traité comme un résultat valide.
    """
    with pytest.raises(ValueError, match="Marge de victoire nulle"):
        run([game("2026-05-02", HOME, AWAY, 80, 80)])


# ──────────── primitive de pronostic partagée (B5 lot 4) ────────────


def test_forecast_is_the_single_prediction_used_by_replay_and_holdout():
    """`apply_game` et `predict_only` passent tous deux par `forecast`.

    Le mode shadow appelle `forecast` directement : sans cette égalité, la probabilité
    observée en production ne serait pas celle dont le lot 3 a mesuré la vraisemblance,
    et les deux calculs pourraient dériver sans que rien ne le signale.
    """
    states, _ = run([game("2026-08-01", HOME, AWAY, 90, 80)])
    prochain = game("2026-08-04", HOME, THIRD, 95, 90)
    jour = date(2026, 8, 4)

    attendu = forecast(states[normalize_team(HOME)],
                       states.get(normalize_team(THIRD)) or TeamState.initial(P),
                       jour, P)

    _, application = apply_game(states, prochain, P, normalize=normalize_team)
    _, holdout = predict_only(states, prochain, P, normalize=normalize_team)

    assert application.expected_home == attendu.expected_home
    assert application.home_advantage == attendu.home_advantage
    assert holdout == attendu.expected_home


def test_forecast_reports_the_rest_context_that_produced_it():
    """Le contexte accompagne le pronostic : un `expected_home` surprenant reste lisible."""
    states, _ = run([game("2026-08-03", HOME, AWAY, 90, 80)])

    veille = forecast(states[normalize_team(HOME)], states[normalize_team(AWAY)],
                      date(2026, 8, 4), P)
    repos = forecast(states[normalize_team(HOME)], states[normalize_team(AWAY)],
                     date(2026, 8, 10), P)

    assert veille.home_days_rest == 1 and veille.away_days_rest == 1
    assert repos.home_days_rest == 7
    # Les deux équipes enchaînent : le malus s'annule, l'avantage terrain reste nominal.
    assert veille.home_advantage == repos.home_advantage


def test_rest_window_start_is_the_single_definition_of_the_four_night_window():
    """La fenêtre a une seule définition, partagée avec la reconstruction en base."""
    assert rest_window_start(date(2026, 8, 10)) == date(2026, 8, 7)
