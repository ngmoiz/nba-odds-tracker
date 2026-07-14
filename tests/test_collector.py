"""Tests du collecteur : découverte, transitions d'états et clôture.

On utilise un client factice (`FakeClient`) qui renvoie des matchs fabriqués :
aucun appel réseau, aucun crédit consommé.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from collector.collector import run_collection
from common import db
from common.db import get_connection, get_match, init_db
from common.odds_api_client import Bookmaker, Market, OddsEvent, Outcome


class FakeClient:
    """Faux client The Odds API : renvoie une liste d'événements prédéfinie."""

    def __init__(self, events: list[OddsEvent]) -> None:
        self.events = events

    def get_odds(self) -> list[OddsEvent]:
        return self.events


def make_event(match_id: str, tipoff: str) -> OddsEvent:
    """Construit un match avec un bookmaker, marchés h2h + spreads (4 issues)."""
    home, away = "Chicago Sky", "Seattle Storm"
    return OddsEvent(
        id=match_id,
        sport_key="basketball_wnba",
        commence_time=tipoff,
        home_team=home,
        away_team=away,
        bookmakers=[
            Bookmaker(
                key="draftkings",
                title="DraftKings",
                last_update="2026-07-14T22:00:00Z",
                markets=[
                    Market("h2h", [Outcome(home, 1.74, None), Outcome(away, 2.14, None)]),
                    Market("spreads", [Outcome(home, 1.93, -2.5), Outcome(away, 1.89, 2.5)]),
                ],
            )
        ],
    )


def in_hours(hours: int) -> str:
    """Timestamp ISO UTC décalé de `hours` heures par rapport à maintenant."""
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "test.db"
    init_db(db_path)
    connection = get_connection(db_path)
    yield connection
    connection.close()


def _count_snapshots(conn, match_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM odds_snapshots WHERE match_id = ?", (match_id,)
    ).fetchone()["n"]


def test_discovery_creates_match_as_decouvert(conn):
    """Un match inconnu est créé en DECOUVERT avec ses cotes d'ouverture."""
    summary = run_collection(conn, FakeClient([make_event("m1", in_hours(6))]), "basketball_wnba")

    assert get_match(conn, "m1")["status"] == "DECOUVERT"
    assert _count_snapshots(conn, "m1") == 4  # 2 h2h + 2 spreads
    assert summary["discovered"] == 1
    assert summary["snapshots"] == 4


def test_second_collection_moves_to_suivi(conn):
    """Au 2e relevé, le match passe de DECOUVERT à SUIVI ; les relevés s'accumulent."""
    event = make_event("m1", in_hours(6))
    run_collection(conn, FakeClient([event]), "basketball_wnba")
    summary = run_collection(conn, FakeClient([event]), "basketball_wnba")

    assert get_match(conn, "m1")["status"] == "SUIVI"
    assert _count_snapshots(conn, "m1") == 8  # deux relevés cumulés (append-only)
    assert summary["discovered"] == 0
    assert summary["newly_tracked"] == 1


def test_status_stays_suivi_on_third_collection(conn):
    """Un match déjà en SUIVI n'est pas remis en arrière aux relevés suivants."""
    event = make_event("m1", in_hours(6))
    for _ in range(3):
        run_collection(conn, FakeClient([event]), "basketball_wnba")
    assert get_match(conn, "m1")["status"] == "SUIVI"


def test_match_past_tipoff_is_closed(conn):
    """Un match actif dont le tip-off est dépassé passe en CLOS."""
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    db.insert_match(
        conn,
        match_id="old",
        sport="basketball_wnba",
        home_team="A",
        away_team="B",
        tipoff_utc=past,
        status="SUIVI",
        created_at=past,
    )
    conn.commit()

    summary = run_collection(conn, FakeClient([]), "basketball_wnba")

    assert get_match(conn, "old")["status"] == "CLOS"
    assert summary["closed"] == 1


def test_closure_handles_z_suffix_timestamp(conn):
    """La clôture gère le format '...Z' renvoyé par l'API (pas seulement '+00:00')."""
    db.insert_match(
        conn,
        match_id="z1",
        sport="basketball_wnba",
        home_team="A",
        away_team="B",
        tipoff_utc="2020-01-01T00:00:00Z",  # bien dans le passé, suffixe Z
        status="DECOUVERT",
        created_at="2020-01-01T00:00:00Z",
    )
    conn.commit()

    run_collection(conn, FakeClient([]), "basketball_wnba")
    assert get_match(conn, "z1")["status"] == "CLOS"


def test_upcoming_match_is_not_closed(conn):
    """Un match encore à venir ne doit pas être clôturé."""
    run_collection(conn, FakeClient([make_event("m1", in_hours(3))]), "basketball_wnba")
    assert get_match(conn, "m1")["status"] == "DECOUVERT"
