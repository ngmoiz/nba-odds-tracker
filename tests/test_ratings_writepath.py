"""Tests du write path des notes Elo (chantier B5, lot 4).

Deux propriétés dominent ce fichier :

1. **la reconstruction de `recent_dates` depuis `rating_history` est exacte** — c'est
   elle qui autorise le write path à exister sans changer le schéma (T1) ;
2. **l'alimentation continue produit les mêmes notes que le rejeu de saison** — sans
   quoi les deux chemins divergeraient en silence (T4).

Les autres tests verrouillent les gardes : idempotence, exclusions nommées, atomicité
par match, et surtout l'innocuité vis-à-vis des évaluations (T7).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from analyzer.model import params_from_config
from analyzer.ratings import apply_game, rest_context
from analyzer.ratings_store import load_team_state
from common import db
from common.config import Settings, load_config
from common.db import get_connection, init_db
from common.results_api_client import GameResult
from evaluator.ratings_update import (
    detect_league_gap,
    detect_team_gap,
    update_ratings_from_games,
)
from evaluator.reconcile import normalize_team

CFG = load_config()
SPORT = "basketball_wnba"
PARAMS = params_from_config(CFG, sport=SPORT)
NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)

TEAMS = ("Las Vegas Aces", "Seattle Storm", "Phoenix Mercury", "Chicago Sky")
# Les alias de la vraie `config.yaml` visent Portland Fire / Toronto Tempo, et
# `build_canonical_resolver` refuse un alias mort à la construction. Les équipes
# suivies doivent donc les inclure : c'est le contrat du lot 3 qui s'exerce, pas une
# contrainte de test — un environnement où ces franchises n'existent pas est
# effectivement mal configuré.
SEEDED = TEAMS + ("Portland Fire", "Toronto Tempo")


def _game(day: str, home: str, away: str, hs: int, as_: int, game_id: str) -> GameResult:
    return GameResult(game_date=day, status="Final", home_team=home, away_team=away,
                      home_score=hs, away_score=as_, game_id=game_id, season=2026)


@pytest.fixture
def conn(tmp_path: Path):
    """Base initialisée, avec un match par affiche pour peupler `known_teams`."""
    db_path = tmp_path / "test.db"
    init_db(db_path)
    connection = get_connection(db_path)
    for index, team in enumerate(SEEDED):
        connection.execute(
            "INSERT INTO matches VALUES (?,?,?,?,?,?,?)",
            (f"seed{index}", SPORT, team, SEEDED[(index + 1) % len(SEEDED)],
             "2026-08-01T23:00:00Z", "EVALUE", "2026-08-01T09:00:00Z"),
        )
    connection.commit()
    yield connection
    connection.close()


def _apply(conn, games: list[GameResult], *, window_start="2026-08-01") -> dict:
    return update_ratings_from_games(
        conn, CFG, games, sport=SPORT, window_start=window_start, now=NOW
    )


# ───────────────────────── T1 : la propriété fondatrice ─────────────────────────


def test_recent_dates_reconstruction_matches_the_in_memory_tuple(conn):
    """T1 — `recent_dates` reconstruit depuis la base == celui que `replay` tient en RAM.

    C'est la propriété qui rend le lot possible : `team_ratings` ne stocke que
    `last_game_date`, et pourtant le contexte de repos (B2B, 3-en-4) est intégralement
    reconstructible. On le vérifie là où il compte — sur `rest_context`, c'est-à-dire
    sur ce que le modèle consomme réellement — et sur un calendrier volontairement
    serré, où un jour d'écart changerait le malus.
    """
    calendrier = [
        _game("2026-08-02", "Las Vegas Aces", "Seattle Storm", 90, 80, "g1"),
        _game("2026-08-03", "Seattle Storm", "Las Vegas Aces", 88, 85, "g2"),   # B2B des deux
        _game("2026-08-05", "Las Vegas Aces", "Phoenix Mercury", 95, 91, "g3"),  # 3-en-4
        _game("2026-08-06", "Chicago Sky", "Las Vegas Aces", 70, 99, "g4"),      # 3-en-4 + B2B
    ]

    # Chemin A : rejeu en mémoire, référence.
    memoire: dict = {}
    for game in calendrier:
        memoire, _ = apply_game(memoire, game, PARAMS, normalize=normalize_team)

    # Chemin B : write path, puis relecture depuis la base.
    _apply(conn, calendrier)

    for jour in ("2026-08-07", "2026-08-08", "2026-08-10"):
        cible = date.fromisoformat(jour)
        for team in TEAMS:
            cle = normalize_team(team)
            relu = load_team_state(conn, SPORT, cle, as_of=cible, params=PARAMS)
            assert rest_context(relu, cible) == rest_context(memoire[cle], cible), (
                f"contexte de repos divergent pour {team} au {jour}"
            )


# ───────────────────── T4 : équivalence des deux chemins ─────────────────────


def test_write_path_produces_the_same_ratings_as_a_full_replay(conn):
    """T4 — alimentation match par match == rejeu de saison, notes et compteurs."""
    calendrier = [
        _game("2026-08-02", "Las Vegas Aces", "Seattle Storm", 90, 80, "g1"),
        _game("2026-08-03", "Phoenix Mercury", "Chicago Sky", 77, 75, "g2"),
        _game("2026-08-05", "Seattle Storm", "Phoenix Mercury", 101, 88, "g3"),
        _game("2026-08-06", "Chicago Sky", "Las Vegas Aces", 70, 99, "g4"),
        _game("2026-08-08", "Las Vegas Aces", "Phoenix Mercury", 84, 82, "g5"),
    ]
    memoire: dict = {}
    for game in calendrier:
        memoire, _ = apply_game(memoire, game, PARAMS, normalize=normalize_team)

    # Le write path les reçoit en DEUX passes, comme deux exécutions de cron.
    assert _apply(conn, calendrier[:3])["applied"] == 3
    assert _apply(conn, calendrier[3:])["applied"] == 2

    for team in TEAMS:
        cle = normalize_team(team)
        row = db.get_team_rating(conn, SPORT, cle)
        assert row["rating"] == pytest.approx(memoire[cle].rating, abs=1e-9)
        assert row["games_played"] == memoire[cle].games_played
        assert row["last_game_date"] == memoire[cle].last_game_date.isoformat()


def test_ratings_stay_zero_sum_through_the_write_path(conn):
    """La somme des notes reste celle du départ : aucune création de valeur."""
    _apply(conn, [
        _game("2026-08-02", "Las Vegas Aces", "Seattle Storm", 90, 80, "g1"),
        _game("2026-08-04", "Phoenix Mercury", "Chicago Sky", 77, 75, "g2"),
    ])
    notes = [row["rating"] for row in db.get_team_ratings(conn, SPORT)]
    assert sum(notes) == pytest.approx(PARAMS.initial_rating * len(notes), abs=1e-6)


# ─────────────────────────── T2/T3 : idempotence ───────────────────────────


def test_replaying_the_same_game_changes_nothing(conn):
    """T2 — un match déjà intégré n'est ni recompté, ni ré-historisé."""
    games = [_game("2026-08-02", "Las Vegas Aces", "Seattle Storm", 90, 80, "g1")]
    assert _apply(conn, games)["applied"] == 1
    avant = {r["team"]: (r["rating"], r["games_played"]) for r in db.get_team_ratings(conn, SPORT)}

    compteurs = _apply(conn, games)

    assert compteurs == {**compteurs, "applied": 0, "already_applied": 1}
    apres = {r["team"]: (r["rating"], r["games_played"]) for r in db.get_team_ratings(conn, SPORT)}
    assert apres == avant
    assert db.count_rating_history(conn, SPORT, "evaluator") == 2  # deux équipes, une fois


def test_unique_index_still_raises_if_the_precheck_is_bypassed(conn):
    """T3 — le filet de dernier recours reste bruyant.

    La pré-vérification est du contrôle de flux ; la contrainte, elle, existe pour
    signaler un double comptage. Si on écrit directement, elle doit lever — sans quoi
    un défaut du write path fausserait toutes les notes en aval, sans aucun signal.
    """
    import sqlite3

    _apply(conn, [_game("2026-08-02", "Las Vegas Aces", "Seattle Storm", 90, 80, "g1")])
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_rating_history(
            conn, sport=SPORT, team=normalize_team("Las Vegas Aces"),
            game_date="2026-08-02", source="evaluator", source_game_id="g1",
            match_id=None, opponent="Seattle Storm", is_home=True,
            rating_before=1500.0, rating_after=1510.0, expected_win=0.6,
            created_at=NOW.isoformat(),
        )


def test_backfill_and_evaluator_share_the_idempotence_key(conn):
    """Un match déjà posé par le backfill n'est jamais réappliqué par l'évaluateur.

    Les deux sources partagent l'espace de noms des identifiants balldontlie : la
    pré-vérification ne filtre donc pas sur `source`, délibérément.
    """
    db.insert_rating_history(
        conn, sport=SPORT, team=normalize_team("Las Vegas Aces"),
        game_date="2026-08-02", source="backfill", source_game_id="g1",
        match_id=None, opponent="Seattle Storm", is_home=True,
        rating_before=1500.0, rating_after=1510.0, expected_win=0.6,
        created_at=NOW.isoformat(),
    )
    conn.commit()

    compteurs = _apply(conn, [_game("2026-08-02", "Las Vegas Aces", "Seattle Storm", 90, 80, "g1")])

    assert compteurs["applied"] == 0 and compteurs["already_applied"] == 1


# ──────────────────── T5/T6 : exclusions comptées et nommées ────────────────────


@pytest.mark.parametrize(
    "game, compteur",
    [
        (_game("2026-08-02", "Las Vegas Aces", "Seattle Storm", 0, 0, "g1"), "unusable"),
        (_game("2026-08-02", "Las Vegas Aces", "Seattle Storm", 77, 77, "g1"), "unusable"),
        (_game("2026-08-02", "TEAM COOP", "TEAM SPOON", 100, 90, "g1"), "unresolved"),
        (GameResult(game_date="2026-08-02", status="Final", home_team="Las Vegas Aces",
                    away_team="Seattle Storm", home_score=90, away_score=80,
                    game_id=None), "no_game_id"),
    ],
    ids=["score 0-0", "match nul", "All-Star (hors franchises)", "sans identifiant"],
)
def test_unusable_games_are_excluded_and_counted(conn, game, compteur):
    """T5/T6 — chaque exclusion est comptée sous son propre motif, jamais silencieuse."""
    compteurs = _apply(conn, [game])

    assert compteurs["applied"] == 0
    assert compteurs[compteur] == 1
    assert db.count_rating_history(conn, SPORT) == 0


def test_a_game_without_id_is_never_applied_twice(conn):
    """Le refus du `game_id` absent n'est pas cosmétique : sinon la note gonflerait.

    En SQLite deux `NULL` sont distincts dans un index unique — un match sans
    identifiant repasserait donc la pré-vérification à chaque tour de fenêtre.
    """
    sans_id = GameResult(game_date="2026-08-02", status="Final", home_team="Las Vegas Aces",
                         away_team="Seattle Storm", home_score=90, away_score=80, game_id=None)
    _apply(conn, [sans_id])
    _apply(conn, [sans_id])

    assert db.get_team_rating(conn, SPORT, normalize_team("Las Vegas Aces")) is None


def test_out_of_order_game_is_refused_not_absorbed(conn):
    """Un match arrivé après un match ultérieur de la même équipe est écarté.

    Refuser plutôt qu'intégrer le futur dans le passé : les notes resteraient
    plausibles et fausses. Le match est compté `out_of_order`, donc visible.
    """
    _apply(conn, [_game("2026-08-06", "Las Vegas Aces", "Seattle Storm", 90, 80, "g2")])

    compteurs = _apply(conn, [_game("2026-08-02", "Las Vegas Aces", "Seattle Storm", 88, 85, "g1")])

    assert compteurs["applied"] == 0 and compteurs["out_of_order"] == 1
    assert db.get_team_rating(conn, SPORT, normalize_team("Las Vegas Aces"))["games_played"] == 1


def test_a_failing_game_does_not_block_the_others(conn):
    """Un match illisible est écarté, les autres entrent quand même."""
    compteurs = _apply(conn, [
        _game("2026-08-02", "Las Vegas Aces", "Seattle Storm", 0, 0, "bad"),
        _game("2026-08-02", "Phoenix Mercury", "Chicago Sky", 77, 75, "ok"),
    ])

    assert compteurs["applied"] == 1 and compteurs["unusable"] == 1
    assert db.get_team_rating(conn, SPORT, normalize_team("Phoenix Mercury")) is not None


def test_a_partially_written_game_is_rolled_back_whole(conn, monkeypatch):
    """Atomicité par match : jamais une seule des deux équipes en base.

    Sans la borne transactionnelle, une moitié écrite serait ensuite prise pour un
    match déjà intégré par la pré-vérification — figeant l'autre moitié à jamais.
    """
    vrai = db.upsert_team_rating
    appels = {"n": 0}

    def echoue_a_la_seconde(*args, **kwargs):
        appels["n"] += 1
        if appels["n"] == 2:
            raise RuntimeError("panne simulée entre les deux équipes")
        return vrai(*args, **kwargs)

    monkeypatch.setattr(db, "upsert_team_rating", echoue_a_la_seconde)

    compteurs = _apply(conn, [_game("2026-08-02", "Las Vegas Aces", "Seattle Storm", 90, 80, "g1")])

    assert compteurs["applied"] == 0 and compteurs["failed"] == 1
    assert db.count_rating_history(conn, SPORT) == 0
    assert db.get_team_ratings(conn, SPORT) == []


# ──────────────────────── Détection des trous d'intégration ────────────────────────


def test_league_gap_is_proven_not_guessed():
    """Le trou de ligue se déduit de la fenêtre, il ne s'estime pas."""
    assert detect_league_gap("2026-08-09", "2026-08-17") is not None
    assert "2026-08-10 au 2026-08-16" in detect_league_gap("2026-08-09", "2026-08-17")
    # Fenêtre contiguë ou recouvrante : aucun trou.
    assert detect_league_gap("2026-08-17", "2026-08-17") is None
    assert detect_league_gap("2026-08-18", "2026-08-17") is None
    # Base neuve : pas d'historique n'est pas un trou.
    assert detect_league_gap(None, "2026-08-17") is None


def test_league_gap_is_reported_on_a_real_run(conn, caplog):
    """Le trou est signalé avant toute application, même si la fenêtre est vide."""
    _apply(conn, [_game("2026-08-02", "Las Vegas Aces", "Seattle Storm", 90, 80, "g1")])

    with caplog.at_level("WARNING"):
        compteurs = _apply(conn, [], window_start="2026-08-14")

    assert compteurs["league_gap"] == 1
    assert "TROU D'INTÉGRATION" in caplog.text
    assert "2026-08-03 au 2026-08-13" in caplog.text


def test_team_gap_flags_a_suspicious_history():
    """Écart par équipe : au-delà du seuil, on demande à aller voir."""
    assert detect_team_gap(4, threshold=3) is True
    assert detect_team_gap(3, threshold=3) is False
    assert detect_team_gap(None, threshold=3) is False  # premier match, pas un écart


def test_team_gap_is_logged_with_the_team_name(conn, caplog):
    """L'avertissement nomme l'équipe : un trou anonyme ne se répare pas."""
    _apply(conn, [_game("2026-08-02", "Las Vegas Aces", "Seattle Storm", 90, 80, "g1")])

    with caplog.at_level("WARNING"):
        _apply(conn, [_game("2026-08-12", "Las Vegas Aces", "Seattle Storm", 88, 85, "g2")])

    assert "Historique suspect" in caplog.text
    assert "las vegas aces" in caplog.text


# ───────────────── T7/T8 : innocuité vis-à-vis de l'évaluation ─────────────────


def _settings() -> Settings:
    return Settings(odds_api_key="", balldontlie_api_key="k", telegram_bot_token="",
                    telegram_chat_id="", database_path=Path("x.db"), log_level="INFO")


def _evaluator_config() -> dict:
    return {
        "api": {"sport": SPORT},
        "display": {"timezone": "Europe/Paris"},
        "results": {"calendar_timezone": "America/New_York"},
        "evaluator": {"lookback_days": 3},
        "model": {"ratings_write_path": True},
    }


@pytest.fixture
def eval_conn(tmp_path: Path):
    """Un match clos avec un verdict prêt à être évalué."""
    db_path = tmp_path / "eval.db"
    init_db(db_path)
    connection = get_connection(db_path)
    connection.execute(
        "INSERT INTO matches VALUES ('m1',?,'Las Vegas Aces','Seattle Storm',"
        "'2026-08-03T00:00:00Z','CLOS','2026-08-02T09:00:00Z')", (SPORT,),
    )
    for index, team in enumerate(SEEDED):  # cibles des alias : cf. commentaire sur SEEDED
        connection.execute(
            "INSERT INTO matches VALUES (?,?,?,?,?,?,?)",
            (f"seed{index}", SPORT, team, SEEDED[(index + 1) % len(SEEDED)],
             "2026-08-01T23:00:00Z", "EVALUE", "2026-08-01T09:00:00Z"),
        )
    db.insert_verdict(
        connection, match_id="m1", verdict="SIGNAL", selection="Las Vegas Aces",
        market="h2h", line=None, odds_at_verdict=1.80, signal_score=6,
        rules_triggered="[]", rationale="test", decided_at="2026-08-02T22:00:00Z",
        logic_version=2,
    )
    connection.commit()
    yield connection
    connection.close()


def test_a_broken_write_path_costs_no_evaluation(eval_conn, monkeypatch):
    """T7 — la propriété la plus importante du lot.

    Une défaillance des notes ne doit coûter ni une évaluation, ni un CLV, ni le bilan
    quotidien. C'est la leçon du micro-lot §0 : une garde bien intentionnée au mauvais
    endroit du flux supprime le service qu'elle prétend protéger.
    """
    from evaluator import evaluator as module

    def explose(*args, **kwargs):
        raise RuntimeError("panne totale du modèle")

    monkeypatch.setattr(module, "update_ratings_from_games", explose)

    summary = module.evaluate_pending(
        eval_conn, _settings(), _evaluator_config(),
        results_client=_fake_client([
            _game("2026-08-02", "Las Vegas Aces", "Seattle Storm", 90, 80, "g1")
        ]),
        telegram_client=None,
        now=datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc),
    )

    assert summary["evaluated"] == 1
    evaluation = eval_conn.execute("SELECT * FROM evaluations").fetchone()
    assert evaluation["outcome"] == "won"
    assert eval_conn.execute(
        "SELECT status FROM matches WHERE match_id='m1'"
    ).fetchone()["status"] == "EVALUE"


def test_summary_keeps_exactly_its_four_keys(eval_conn):
    """T8 — la forme du résumé est un marqueur de non-régression, on n'y touche pas.

    Le journal compare cette valeur exacte de part et d'autre des déploiements pour
    détecter une régression d'appariement. Y ajouter des compteurs détruirait le témoin
    au moment précis où il sert.
    """
    from evaluator import evaluator as module

    summary = module.evaluate_pending(
        eval_conn, _settings(), _evaluator_config(),
        results_client=_fake_client([
            _game("2026-08-02", "Las Vegas Aces", "Seattle Storm", 90, 80, "g1")
        ]),
        telegram_client=None,
        now=datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc),
    )

    assert set(summary) == {"evaluated", "given_up", "skipped", "ungradable"}


def test_the_evaluator_run_actually_feeds_the_ratings(eval_conn):
    """Bout en bout : le run de l'évaluateur met bien à jour les notes."""
    from evaluator import evaluator as module

    module.evaluate_pending(
        eval_conn, _settings(), {**_evaluator_config(), **_model_config()},
        results_client=_fake_client([
            _game("2026-08-02", "Las Vegas Aces", "Seattle Storm", 90, 80, "g1")
        ]),
        telegram_client=None,
        now=datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc),
    )

    gagnant = db.get_team_rating(eval_conn, SPORT, normalize_team("Las Vegas Aces"))
    assert gagnant is not None and gagnant["rating"] > PARAMS.initial_rating
    assert db.count_rating_history(eval_conn, SPORT, "evaluator") == 2


def _model_config() -> dict:
    return {"model": {**CFG["model"], "ratings_write_path": True}}


def _fake_client(games):
    class _Fake:
        def get_games(self, start, end):
            return games

        def close(self):
            pass

    return _Fake()


# ──────────────────── Réparation après trou d'intégration ────────────────────


def test_replace_is_refused_once_the_evaluator_has_contributed(conn):
    """`delete_backfill_ratings` refuse dès qu'une ligne `evaluator` existe.

    Comportement voulu, et c'est précisément ce qui rend une primitive de
    reconstruction nécessaire : après le lot 4, remplacer le seul backfill laisserait
    la base dans un état que le rejeu seul ne reconstruit pas.
    """
    _apply(conn, [_game("2026-08-02", "Las Vegas Aces", "Seattle Storm", 90, 80, "g1")])

    with pytest.raises(ValueError, match="proviennent de l'évaluateur"):
        db.delete_backfill_ratings(conn, SPORT)


def test_rebuild_purges_every_source_so_the_replay_can_start_clean(conn):
    """La réparation efface tout, backfill compris — sinon le rejeu double-compterait."""
    db.insert_rating_history(
        conn, sport=SPORT, team=normalize_team("Phoenix Mercury"),
        game_date="2026-08-01", source="backfill", source_game_id="old",
        match_id=None, opponent="Chicago Sky", is_home=True,
        rating_before=1500.0, rating_after=1512.0, expected_win=0.5,
        created_at=NOW.isoformat(),
    )
    conn.commit()
    _apply(conn, [_game("2026-08-02", "Las Vegas Aces", "Seattle Storm", 90, 80, "g1")])

    history, ratings = db.delete_all_ratings(conn, SPORT)
    conn.commit()

    assert history == 3 and ratings > 0
    assert db.count_rating_history(conn, SPORT) == 0
    assert db.get_team_ratings(conn, SPORT) == []


def test_rebuild_is_scoped_to_one_league(conn):
    """La reconstruction d'une ligue ne touche jamais les notes d'une autre."""
    db.insert_rating_history(
        conn, sport="basketball_nba", team="boston celtics",
        game_date="2026-01-05", source="backfill", source_game_id="nba1",
        match_id=None, opponent="Miami Heat", is_home=True,
        rating_before=1500.0, rating_after=1515.0, expected_win=0.55,
        created_at=NOW.isoformat(),
    )
    conn.commit()
    _apply(conn, [_game("2026-08-02", "Las Vegas Aces", "Seattle Storm", 90, 80, "g1")])

    db.delete_all_ratings(conn, SPORT)
    conn.commit()

    assert db.count_rating_history(conn, SPORT) == 0
    assert db.count_rating_history(conn, "basketball_nba") == 1


def test_the_repair_restores_exactly_what_the_write_path_had_built(conn):
    """Reconstruction == alimentation continue : la réparation ne dégrade rien.

    C'est ce qui rend la procédure acceptable : reconstruire n'est pas un pis-aller,
    c'est produire le même résultat par un autre chemin — les tables sont dérivées.
    """
    calendrier = [
        _game("2026-08-02", "Las Vegas Aces", "Seattle Storm", 90, 80, "g1"),
        _game("2026-08-04", "Phoenix Mercury", "Chicago Sky", 77, 75, "g2"),
        _game("2026-08-05", "Seattle Storm", "Phoenix Mercury", 101, 88, "g3"),
    ]
    _apply(conn, calendrier)
    avant = {r["team"]: (r["rating"], r["games_played"]) for r in db.get_team_ratings(conn, SPORT)}

    db.delete_all_ratings(conn, SPORT)
    conn.commit()
    _apply(conn, calendrier)

    apres = {r["team"]: (r["rating"], r["games_played"]) for r in db.get_team_ratings(conn, SPORT)}
    assert apres == avant


# ──────────── Stabilité de la clé de cache (horizon des matchs connus) ────────────


def test_replay_range_is_identical_on_two_different_days(conn):
    """La plage rejouée — donc la clé de cache — ne bouge pas d'un jour à l'autre.

    Défaut réel du 2026-08-10 : la borne haute valait « aujourd'hui + 1 », si bien que
    l'URL interrogée changeait toutes les 24 h. `--offline` ratait dès le lendemain sur
    un cache pourtant complet, et la procédure de réparation documentée en héritait —
    au pire moment, celui de l'incident.
    """
    import analyzer.ratings_store as store

    _apply(conn, [_game("2026-08-02", "Las Vegas Aces", "Seattle Storm", 90, 80, "g1")])

    veille = store.replay_range(conn, CFG, SPORT, today=date(2026, 8, 10))
    quinze_jours_plus_tard = store.replay_range(conn, CFG, SPORT, today=date(2026, 8, 25))

    assert veille == quinze_jours_plus_tard


def test_replay_range_does_depend_on_the_clock_when_no_match_is_known(conn):
    """Calibration du test précédent : l'horloge EST atteignable par cette fonction.

    Sans ce contre-exemple, l'égalité ci-dessus pourrait tenir simplement parce que
    `today` n'est jamais lu — un test vacant. Ici la ligue est vide, il n'y a donc
    aucun horizon, et la plage doit suivre l'horloge : les deux jours diffèrent.
    """
    import analyzer.ratings_store as store

    a = store.replay_range(conn, CFG, "basketball_nba", today=date(2026, 8, 10))
    b = store.replay_range(conn, CFG, "basketball_nba", today=date(2026, 8, 25))

    assert a != b


def test_replay_range_covers_the_last_known_match(conn):
    """La borne haute dépasse d'un jour le dernier match connu (matchs du soir en UTC)."""
    import analyzer.ratings_store as store

    conn.execute(
        "INSERT INTO matches VALUES ('tard',?,'Chicago Sky','Phoenix Mercury',"
        "'2026-08-30T23:00:00Z','SUIVI','2026-08-29T09:00:00Z')", (SPORT,),
    )
    conn.commit()

    _, fin = store.replay_range(conn, CFG, SPORT, today=date(2026, 8, 10))

    assert fin == "2026-08-31"


def test_known_horizon_covers_matches_never_integrated(conn):
    """L'horizon vient du CALENDRIER, pas de ce qui est déjà intégré.

    Point de conception décisif : une borne dérivée du seul `rating_history`
    couvrirait exactement ce qui est en base, et la comparaison de population du
    vérificateur deviendrait tautologique — un match joué mais jamais intégré
    tomberait hors plage, c'est-à-dire précisément le trou qu'on cherche.
    """
    import analyzer.ratings_store as store

    _apply(conn, [_game("2026-08-02", "Las Vegas Aces", "Seattle Storm", 90, 80, "g1")])
    # Un match programmé bien plus tard, découvert par le collecteur, jamais intégré.
    conn.execute(
        "INSERT INTO matches VALUES ('futur',?,'Chicago Sky','Phoenix Mercury',"
        "'2026-08-30T23:00:00Z','SUIVI','2026-08-29T09:00:00Z')", (SPORT,),
    )
    conn.commit()

    assert store.known_match_horizon(conn, SPORT) == "2026-08-30"


def test_known_horizon_is_none_on_an_empty_league(conn):
    """Ligue vide : pas d'horizon inventé, l'appelant décide du repli."""
    import analyzer.ratings_store as store

    assert store.known_match_horizon(conn, "basketball_nba") is None
