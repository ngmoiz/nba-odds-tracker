"""Tests du collecteur : découverte, transitions d'états, clôture, collectes
conditionnelles et garde de réserve.

On utilise un client factice (`FakeClient`) qui renvoie des matchs fabriqués :
aucun appel réseau, aucun crédit consommé.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from collector.collector import (
    META_CREDITS_REMAINING,
    META_RESERVE_ALERTED,
    run_collection,
)
from common import db
from common.db import get_connection, get_match, init_db
from common.odds_api_client import Bookmaker, Market, OddsEvent, Outcome


class FakeClient:
    """Faux client The Odds API : renvoie une liste d'événements prédéfinie."""

    def __init__(self, events: list[OddsEvent]) -> None:
        self.events = events
        self.credits_remaining = "480"

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


# ─── Tests existants (découverte, transitions, clôture) ───


def test_discovery_creates_match_as_decouvert(conn):
    """Un match inconnu est créé en DECOUVERT avec ses cotes d'ouverture."""
    summary = run_collection(conn, FakeClient([make_event("m1", in_hours(6))]), "basketball_wnba", force=True)

    assert get_match(conn, "m1")["status"] == "DECOUVERT"
    assert _count_snapshots(conn, "m1") == 4  # 2 h2h + 2 spreads
    assert summary["discovered"] == 1
    assert summary["snapshots"] == 4


def test_second_collection_moves_to_suivi(conn):
    """Au 2e relevé, le match passe de DECOUVERT à SUIVI ; les relevés s'accumulent."""
    event = make_event("m1", in_hours(6))
    run_collection(conn, FakeClient([event]), "basketball_wnba", force=True)
    summary = run_collection(conn, FakeClient([event]), "basketball_wnba", force=True)

    assert get_match(conn, "m1")["status"] == "SUIVI"
    assert _count_snapshots(conn, "m1") == 8  # deux relevés cumulés (append-only)
    assert summary["discovered"] == 0
    assert summary["newly_tracked"] == 1


def test_status_stays_suivi_on_third_collection(conn):
    """Un match déjà en SUIVI n'est pas remis en arrière aux relevés suivants."""
    event = make_event("m1", in_hours(6))
    for _ in range(3):
        run_collection(conn, FakeClient([event]), "basketball_wnba", force=True)
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
    run_collection(conn, FakeClient([make_event("m1", in_hours(3))]), "basketball_wnba", force=True)
    assert get_match(conn, "m1")["status"] == "DECOUVERT"


# ─── Tests des collectes conditionnelles (post-1.7) ───

CONFIG = {"quota": {"reserve": 50}}


def test_conditional_skip_when_no_active_matches(conn):
    """Collecte conditionnelle sautée si aucun match actif en base (zéro crédit)."""
    summary = run_collection(conn, FakeClient([]), "basketball_wnba", CONFIG, force=False)

    assert summary["skipped"] is True
    assert summary["reason"] == "no_active_matches"


def test_conditional_collect_when_active_matches_exist(conn):
    """Collecte conditionnelle exécutée s'il y a des matchs actifs en base."""
    # Prépare un match en SUIVI en base.
    db.insert_match(
        conn,
        match_id="m1",
        sport="basketball_wnba",
        home_team="A",
        away_team="B",
        tipoff_utc=in_hours(6),
        status="SUIVI",
        created_at=in_hours(12),
    )
    conn.commit()

    summary = run_collection(
        conn, FakeClient([make_event("m1", in_hours(6))]), "basketball_wnba", CONFIG, force=False
    )

    assert summary["skipped"] is False
    assert summary["snapshots"] == 4


def test_morning_force_collects_even_with_empty_base(conn):
    """Le créneau du matin (force=True) collecte même si la base est vide."""
    summary = run_collection(
        conn, FakeClient([make_event("m1", in_hours(6))]), "basketball_wnba", CONFIG, force=True
    )

    assert summary["skipped"] is False
    assert summary["discovered"] == 1


# ─── Tests de la garde de réserve (post-1.7) ───


def test_reserve_skips_when_credits_below_threshold(conn):
    """Garde de réserve : collecte sautée si credits_remaining < seuil."""
    # Prépare un match actif + un quota sous le seuil.
    db.insert_match(
        conn,
        match_id="m1",
        sport="basketball_wnba",
        home_team="A",
        away_team="B",
        tipoff_utc=in_hours(6),
        status="SUIVI",
        created_at=in_hours(12),
    )
    db.set_meta(conn, META_CREDITS_REMAINING, "30")  # sous le seuil de 50
    conn.commit()

    summary = run_collection(conn, FakeClient([]), "basketball_wnba", CONFIG, force=False)

    assert summary["skipped"] is True
    assert summary["reason"] == "reserve"
    # La garde est marquée comme alertée.
    assert db.get_meta(conn, META_RESERVE_ALERTED) == "true"


def test_reserve_alert_is_deduplicated(conn):
    """La notification de réserve n'est envoyée qu'une fois (déduplication)."""
    db.insert_match(
        conn,
        match_id="m1",
        sport="basketball_wnba",
        home_team="A",
        away_team="B",
        tipoff_utc=in_hours(6),
        status="SUIVI",
        created_at=in_hours(12),
    )
    db.set_meta(conn, META_CREDITS_REMAINING, "30")
    conn.commit()

    # Première collecte : déclenche la garde (reserve_alerted passe à true).
    run_collection(conn, FakeClient([]), "basketball_wnba", CONFIG, force=False)
    assert db.get_meta(conn, META_RESERVE_ALERTED) == "true"

    # Deuxième collecte : skip silencieux (reserve_alerted déjà true).
    summary = run_collection(conn, FakeClient([]), "basketball_wnba", CONFIG, force=False)
    assert summary["skipped"] is True
    assert summary["reason"] == "reserve"
    # Toujours true, pas de re-notification.
    assert db.get_meta(conn, META_RESERVE_ALERTED) == "true"


def test_morning_lifts_reserve_on_quota_refresh(conn):
    """La collecte du matin (force=True) lève la garde si le quota repasse au-dessus du seuil."""
    # Garde active + quota sous le seuil.
    db.set_meta(conn, META_CREDITS_REMAINING, "30")
    db.set_meta(conn, META_RESERVE_ALERTED, "true")
    conn.commit()

    # Collecte du matin : le FakeClient renvoie credits_remaining = "480" (au-dessus du seuil).
    run_collection(
        conn, FakeClient([make_event("m1", in_hours(6))]), "basketball_wnba", CONFIG, force=True
    )

    # Le quota rafraîchi est persisté et la garde est levée.
    assert db.get_meta(conn, META_CREDITS_REMAINING) == "480"
    assert db.get_meta(conn, META_RESERVE_ALERTED) == "false"


def test_reserve_allows_collection_on_fresh_db(conn):
    """Base neuve (meta vide) : la garde de reserve laisse passer la collecte.

    Critique pour l installation EC2 qui part d une base vide : si credits_remaining
    est inconnu (None), on ne bloque pas la premiere collecte conditionnelle.
    """
    db.insert_match(
        conn,
        match_id="m1",
        sport="basketball_wnba",
        home_team="A",
        away_team="B",
        tipoff_utc=in_hours(6),
        status="SUIVI",
        created_at=in_hours(12),
    )
    conn.commit()

    summary = run_collection(
        conn, FakeClient([make_event("m1", in_hours(6))]), "basketball_wnba", CONFIG, force=False
    )

    assert summary["skipped"] is False
    assert summary["snapshots"] == 4
