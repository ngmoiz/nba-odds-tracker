"""Tests de la couche base de données (`common/db`).

Priorité de l'étape 1.1 : verrouiller la garantie **append-only** sur
`odds_snapshots` (règle 0.4.2), imposée au niveau de la base par des triggers SQLite.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from common.db import get_connection, init_db


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
