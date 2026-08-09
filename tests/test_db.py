"""Tests de la couche base de données (`common/db`).

Priorité de l'étape 1.1 : verrouiller la garantie **append-only** sur
`odds_snapshots` (règle 0.4.2), imposée au niveau de la base par des triggers SQLite.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from common.db import (
    _normalized_team_key,
    count_rating_history,
    get_connection,
    get_team_rating,
    get_team_ratings,
    init_db,
    insert_rating_history,
    upsert_team_rating,
)


@pytest.fixture
def db(tmp_path: Path):
    """Base SQLite temporaire, initialisée, avec un match et un relevé de référence.

    `tmp_path` est une fixture pytest fournissant un dossier temporaire unique par
    test : chaque test travaille sur sa propre base, isolée et jetable.
    """
    db_path = tmp_path / "test.db"
    init_db(db_path)
    conn = get_connection(db_path)
    conn.execute(
        "INSERT INTO matches VALUES "
        "('m1','basketball_nba','Boston','Miami','2026-07-16T23:00:00Z',"
        "'DECOUVERT','2026-07-15T09:00:00Z')"
    )
    conn.execute(
        "INSERT INTO odds_snapshots (match_id,bookmaker,market,selection,line,odds,snapshot_at) "
        "VALUES ('m1','pinnacle','spreads','Boston',-7.5,1.91,'2026-07-15T09:00:00Z')"
    )
    conn.commit()
    yield conn
    conn.close()


def test_init_db_creates_all_tables(db):
    """L'initialisation crée bien les 6 tables du modèle (section 5)."""
    rows = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    tables = {r["name"] for r in rows}
    attendues = {
        "matches",
        "odds_snapshots",
        "alerts",
        "verdicts",
        "positions",
        "evaluations",
    }
    assert attendues.issubset(tables)


def test_insert_snapshot_is_allowed(db):
    """Un INSERT dans odds_snapshots est autorisé (ajout d'un nouveau relevé)."""
    db.execute(
        "INSERT INTO odds_snapshots (match_id,bookmaker,market,selection,line,odds,snapshot_at) "
        "VALUES ('m1','pinnacle','spreads','Boston',-6.5,1.95,'2026-07-15T12:00:00Z')"
    )
    db.commit()
    count = db.execute("SELECT COUNT(*) AS n FROM odds_snapshots").fetchone()["n"]
    assert count == 2


def test_update_snapshot_is_blocked(db):
    """Un UPDATE sur odds_snapshots est rejeté par le trigger append-only."""
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE odds_snapshots SET odds = 1.50 WHERE id = 1")
    # La cote d'origine doit rester intacte.
    odds = db.execute("SELECT odds FROM odds_snapshots WHERE id = 1").fetchone()["odds"]
    assert odds == 1.91


def test_delete_snapshot_is_blocked(db):
    """Un DELETE sur odds_snapshots est rejeté par le trigger append-only."""
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM odds_snapshots WHERE id = 1")
    count = db.execute("SELECT COUNT(*) AS n FROM odds_snapshots").fetchone()["n"]
    assert count == 1


def test_foreign_key_is_enforced(db):
    """Un relevé rattaché à un match inexistant est refusé (clé étrangère active)."""
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO odds_snapshots (match_id,bookmaker,market,selection,odds,snapshot_at) "
            "VALUES ('ghost','x','h2h','Boston',1.9,'2026-07-15T09:00:00Z')"
        )


# ───────────── Notes de force Elo (chantier B5, lot 2) ─────────────
#
# Tables dérivées : ni append-only, ni protégées par trigger. Ce qui est verrouillé
# ici, c'est la clé `(sport, team)`, les invariants (None explicite, échec bruyant)
# et le contrat de normalisation.

WNBA, NBA = "basketball_wnba", "basketball_nba"


def _rating(db, **kwargs):
    """Écrit une note avec des valeurs par défaut plausibles."""
    upsert_team_rating(db, **{
        "sport": WNBA, "team": "las vegas aces", "display_name": "Las Vegas Aces",
        "rating": 1500.0, "games_played": 0, "last_game_date": None,
        "updated_at": "2026-08-09T09:00:00Z", **kwargs,
    })


def test_init_db_creates_rating_tables(db):
    """`init_db` crée les deux tables du modèle de force."""
    tables = {r["name"] for r in
              db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"team_ratings", "rating_history"}.issubset(tables)


def test_rating_tables_are_created_on_an_existing_database(tmp_path: Path):
    """Migration : sur une base ANTÉRIEURE aux tables, `init_db` les ajoute.

    C'est le vrai chemin de la base de production, qui ne les a pas aujourd'hui —
    simulé en les supprimant d'une base déjà initialisée, puis en relançant.
    """
    db_path = tmp_path / "legacy.db"
    init_db(db_path)
    conn = get_connection(db_path)
    conn.executescript("DROP TABLE team_ratings; DROP TABLE rating_history;")
    conn.commit()
    conn.close()

    init_db(db_path)                                  # re-migration
    conn = get_connection(db_path)
    try:
        tables = {r["name"] for r in
                  conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"team_ratings", "rating_history"}.issubset(tables)
    finally:
        conn.close()


def test_init_db_is_idempotent_on_rating_tables(tmp_path: Path):
    """Deux `init_db` successifs ne lèvent pas et ne dupliquent aucune donnée."""
    db_path = tmp_path / "twice.db"
    init_db(db_path)
    conn = get_connection(db_path)
    _rating(conn, rating=1523.0)
    conn.commit()
    conn.close()

    init_db(db_path)                                  # second passage
    conn = get_connection(db_path)
    try:
        rows = get_team_ratings(conn, WNBA)
        assert len(rows) == 1 and rows[0]["rating"] == 1523.0
    finally:
        conn.close()


def test_upsert_team_rating_updates_in_place(db):
    """Deux écritures sur la même clé `(sport, team)` → une seule ligne, la seconde."""
    _rating(db, rating=1500.0, games_played=0)
    _rating(db, rating=1544.5, games_played=3, last_game_date="2026-08-08")

    rows = get_team_ratings(db, WNBA)
    assert len(rows) == 1
    assert rows[0]["rating"] == 1544.5
    assert rows[0]["games_played"] == 3
    assert rows[0]["last_game_date"] == "2026-08-08"


def test_ratings_are_partitioned_by_sport(db):
    """Un même nom d'équipe garde des notes SÉPARÉES par ligue.

    Toute la raison d'être de la clé composite : la base est partagée entre ligues
    (§6.5), et deux échelles Elo indépendantes ne doivent jamais se mélanger.
    """
    _rating(db, sport=WNBA, team="atlanta dream", display_name="Atlanta Dream", rating=1480.0)
    _rating(db, sport=NBA, team="atlanta dream", display_name="Atlanta Dream", rating=1610.0)

    assert get_team_rating(db, WNBA, "atlanta dream")["rating"] == 1480.0
    assert get_team_rating(db, NBA, "atlanta dream")["rating"] == 1610.0
    assert len(get_team_ratings(db, WNBA)) == 1


def test_get_team_rating_returns_none_for_unknown_team(db):
    """Équipe jamais notée → `None`, jamais une note par défaut (invariant 5).

    Renvoyer 1500 d'office masquerait la différence entre « jamais vue » et
    « exactement à la note de départ ».
    """
    assert get_team_rating(db, WNBA, "equipe inconnue") is None


def test_last_game_date_none_is_preserved(db):
    """`last_game_date` absent reste NULL, distinct d'une date (invariant 5)."""
    _rating(db, last_game_date=None)
    assert get_team_rating(db, WNBA, "las vegas aces")["last_game_date"] is None


def test_write_rejects_a_non_normalized_team_key(db):
    """Clé non normalisée → erreur bruyante, à l'écriture.

    Le contrat ne peut pas rester une simple docstring : si le backfill et
    l'évaluateur normalisaient différemment, la base contiendrait deux lignes pour
    la même équipe et `UNIQUE(sport, team)` ne les rattraperait pas — une note
    scindée en deux, fausse des deux côtés, sans aucun signal.
    """
    for mauvaise in ("Las Vegas Aces", " las vegas aces", "las  vegas  aces"):
        with pytest.raises(ValueError, match="non normalisée"):
            _rating(db, team=mauvaise)
        with pytest.raises(ValueError, match="non normalisée"):
            insert_rating_history(
                db, sport=WNBA, team=mauvaise, game_date="2026-08-08", source="backfill",
                source_game_id="1", match_id=None, opponent="Seattle Storm", is_home=True,
                rating_before=1500.0, rating_after=1512.0, expected_win=0.55,
                created_at="2026-08-09T09:00:00Z",
            )


def test_db_normalization_matches_reconcile_implementation():
    """La normalisation dupliquée dans `db` doit rester identique à celle d'`evaluator`.

    `common/` ne peut pas importer `evaluator/` sans inverser les couches, d'où une
    duplication volontaire — ce test est le verrou qui l'empêche de dériver.
    """
    from evaluator.reconcile import normalize_team

    for value in ("Las Vegas Aces", "  Seattle   Storm ", "GOLDEN STATE VALKYRIES",
                  "toronto tempo", "New\tYork Liberty", ""):
        assert _normalized_team_key(value) == normalize_team(value)


def test_insert_rating_history_rejects_double_counting(db):
    """Deux fois le même match pour la même équipe → `IntegrityError`, jamais avalée.

    Contrairement à `mark_target_served` : re-servir une cible est normal, appliquer
    deux fois un match au même Elo signifie que le rejeu a double-compté — un défaut
    qui fausserait toutes les notes en aval (invariant 6).
    """
    ligne = dict(sport=WNBA, team="las vegas aces", game_date="2026-08-08",
                 source="backfill", source_game_id="18447401", match_id=None,
                 opponent="Seattle Storm", is_home=True, rating_before=1500.0,
                 rating_after=1512.0, expected_win=0.55,
                 created_at="2026-08-09T09:00:00Z")
    insert_rating_history(db, **ligne)
    with pytest.raises(sqlite3.IntegrityError):
        insert_rating_history(db, **ligne)

    assert count_rating_history(db, WNBA) == 1
    assert count_rating_history(db, WNBA, source="backfill") == 1
    assert count_rating_history(db, WNBA, source="evaluator") == 0


def test_rating_history_check_constraints(db):
    """`source` hors liste et `is_home` hors 0/1 sont rejetés par la base."""
    base = dict(sport=WNBA, team="las vegas aces", game_date="2026-08-08",
                source_game_id="1", match_id=None, opponent="Seattle Storm",
                is_home=True, rating_before=1500.0, rating_after=1512.0,
                expected_win=0.55, created_at="2026-08-09T09:00:00Z")
    with pytest.raises(sqlite3.IntegrityError):
        insert_rating_history(db, **{**base, "source": "inconnue"})
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO rating_history (sport, team, game_date, source, opponent, "
            "is_home, rating_before, rating_after, expected_win, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (WNBA, "las vegas aces", "2026-08-08", "backfill", "Seattle Storm",
             7, 1500.0, 1512.0, 0.55, "2026-08-09T09:00:00Z"),
        )
