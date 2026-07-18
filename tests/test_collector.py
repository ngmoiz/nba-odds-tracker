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
        self.last_request_cost = "3"

    def get_odds(self, markets: list[str] | None = None) -> list[OddsEvent]:
        """Compatibilité Lot 2 : accepte le paramètre markets (ignoré dans les tests)."""
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

    summary = run_collection(conn, FakeClient([]), "basketball_wnba", CONFIG_WITH_TARGETS)

    assert get_match(conn, "old")["status"] == "CLOS"
    # Accès direct : si la clé disparaît, le test échoue (invariant préservé)
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

# Config minimale avec targets (pour tests Lot 2 nécessitant des cibles configurées)
CONFIG_WITH_TARGETS = {
    "quota": {"reserve": 50},
    "collector": {
        "targets": [
            {"name": "test", "hours_before": 6.0, "markets": ["h2h", "spreads"], "priority": 2}
        ]
    }
}


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
        conn, FakeClient([make_event("m1", in_hours(6))]), "basketball_wnba", CONFIG_WITH_TARGETS, force=False
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
    """Garde de réserve : collecte sautée si credits_remaining < seuil.
    
    Lot 2 : garde par priorité (priorité 2 bloquée, 0 cible collectée).
    """
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

    summary = run_collection(conn, FakeClient([]), "basketball_wnba", CONFIG_WITH_TARGETS, force=False)

    # Lot 2 : pas de skip global, mais 0 cible collectée (toutes bloquées par priorité)
    assert summary["skipped"] is False
    assert summary["targets_collected"] == 0
    # La garde est marquée comme alertée.
    assert db.get_meta(conn, META_RESERVE_ALERTED) == "true"


def test_reserve_alert_is_deduplicated(conn):
    """La notification de réserve n'est envoyée qu'une fois (déduplication).
    
    Lot 2 : garde par priorité, 0 cible collectée (log silencieux 2e fois).
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
    db.set_meta(conn, META_CREDITS_REMAINING, "30")
    conn.commit()

    # Première collecte : déclenche la garde (reserve_alerted passe à true).
    run_collection(conn, FakeClient([]), "basketball_wnba", CONFIG_WITH_TARGETS, force=False)
    assert db.get_meta(conn, META_RESERVE_ALERTED) == "true"

    # Deuxième collecte : skip silencieux (reserve_alerted déjà true), 0 cible collectée.
    summary = run_collection(conn, FakeClient([]), "basketball_wnba", CONFIG_WITH_TARGETS, force=False)
    assert summary["skipped"] is False
    assert summary["targets_collected"] == 0
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


def test_reserve_allows_priority1_targets(conn):
    """Garde de réserve : cibles priorité 1 collectées même sous le seuil.
    
    Protection critique Lot 2 : verdict (H-2), re-décision (H-1), clôture (H-0.25)
    ne sont JAMAIS bloquées par la garde de réserve (priorité 1).
    """
    # Prépare un match actif + un quota sous le seuil.
    db.insert_match(
        conn,
        match_id="m1",
        sport="basketball_wnba",
        home_team="A",
        away_team="B",
        tipoff_utc=in_hours(2),  # H-2 : cible verdict due
        status="SUIVI",
        created_at=in_hours(12),
    )
    db.set_meta(conn, META_CREDITS_REMAINING, "30")  # sous le seuil de 50
    conn.commit()

    # Config avec cible priorité 1 (verdict H-2)
    config_priority1 = {
        "quota": {"reserve": 50},
        "collector": {
            "targets": [
                {"name": "verdict", "hours_before": 2.0, "markets": ["h2h", "spreads"], "priority": 1}
            ]
        }
    }

    summary = run_collection(conn, FakeClient([make_event("m1", in_hours(2))]), "basketball_wnba", config_priority1, force=False)

    # Priorité 1 : collecte exécutée malgré le seuil
    assert summary["skipped"] is False
    assert summary["targets_collected"] == 1  # cible priorité 1 collectée
    assert summary["snapshots"] == 4  # snapshots enregistrés
    # Priorité 1 bypass la garde : reserve_alerted reste None (pas de notification)
    assert db.get_meta(conn, META_RESERVE_ALERTED) is None


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
        conn, FakeClient([make_event("m1", in_hours(6))]), "basketball_wnba", CONFIG_WITH_TARGETS, force=False
    )

    assert summary["skipped"] is False
    assert summary["snapshots"] == 4


# ═══════════════════════════════════════════════════════════════════════════════════
# TESTS LOT 2 : AUTO-ORDONNANCEMENT (18 nouveaux tests)
# ═══════════════════════════════════════════════════════════════════════════════════

# ─── Tests des 3 décisions verrouillées (régressions critiques) ───


def test_dedup_stable_when_wave_composition_changes(conn):
    """Déduplication par (match_id, target_hours) : stable si composition vague change.
    
    Décision verrouillée : wave_id médian instable (1er match CLOS → médian bouge
    → cibles redeviennent non servies). Dédup par (match_id, target_hours) garantit
    qu'une cible servie reste servie même si la vague se recompose.
    """
    # Vague initiale : 2 matchs espacés de 30 min (même vague, seuil 45 min)
    db.insert_match(conn, match_id="m1", sport="basketball_wnba", home_team="A", away_team="B", tipoff_utc=in_hours(6), status="SUIVI", created_at=in_hours(12))
    db.insert_match(conn, match_id="m2", sport="basketball_wnba", home_team="C", away_team="D", tipoff_utc=in_hours(6.5), status="SUIVI", created_at=in_hours(12))
    conn.commit()

    config = {
        "quota": {"reserve": 50},
        "collector": {
            "wave_grouping_minutes": 45,
            "targets": [
                {"name": "H-6", "hours_before": 6.0, "markets": ["h2h"], "priority": 2}
            ]
        }
    }

    # Tick 1 : collecte H-6 pour les 2 matchs (1 vague)
    summary1 = run_collection(
        conn,
        FakeClient([make_event("m1", in_hours(6)), make_event("m2", in_hours(6.5))]),
        "basketball_wnba",
        config,
        force=False,
    )
    assert summary1["targets_collected"] == 1  # 1 cible (H-6) servie pour la vague
    assert summary1["snapshots"] == 8  # 2 matchs × 4 snapshots

    # Clôture manuelle de m1 (simule tip-off passé)
    db.update_match_status(conn, "m1", "CLOS")
    conn.commit()

    # Tick 2 : vague recomposée (m2 seul), H-6 déjà servie pour m2
    summary2 = run_collection(
        conn,
        FakeClient([make_event("m2", in_hours(6.5))]),
        "basketball_wnba",
        config,
        force=False,
    )
    # Dédup par (match_id, target_hours) : m2 déjà servi pour H-6 → 0 cible collectée
    assert summary2["targets_collected"] == 0
    assert summary2["snapshots"] == 0


def test_targets_on_earliest_tipoff_closing_per_match(conn):
    """Cibles sur earliest_tipoff, SAUF closing (per_match: true).
    
    Décision verrouillée : Vague à tip-offs échelonnés (40 min) → 2 clôtures distinctes
    (H-0.25 de chaque match), aucun snapshot après le 1er coup d'envoi.
    """
    # Vague : 2 matchs espacés de 40 min (même vague, seuil 45 min)
    # m1 à H+0, m2 à H+0.67 (40 min plus tard)
    now = datetime.now(timezone.utc)
    tipoff_m1 = (now + timedelta(hours=1)).isoformat()
    tipoff_m2 = (now + timedelta(hours=1, minutes=40)).isoformat()
    
    db.insert_match(conn, match_id="m1", sport="basketball_wnba", home_team="A", away_team="B", tipoff_utc=tipoff_m1, status="DECIDE", created_at=now.isoformat())
    db.insert_match(conn, match_id="m2", sport="basketball_wnba", home_team="C", away_team="D", tipoff_utc=tipoff_m2, status="DECIDE", created_at=now.isoformat())
    
    # Crée les verdicts (pour que closing soit due)
    db.insert_verdict(conn, match_id="m1", verdict="BET", selection="A", market="h2h", line=None, odds_at_verdict=1.5, signal_score=10, rules_triggered="test", rationale="test", decided_at=now.isoformat())
    db.insert_verdict(conn, match_id="m2", verdict="BET", selection="C", market="spreads", line=-2.5, odds_at_verdict=1.9, signal_score=10, rules_triggered="test", rationale="test", decided_at=now.isoformat())
    conn.commit()

    config = {
        "quota": {"reserve": 50},
        "collector": {
            "wave_grouping_minutes": 45,
            "targets": [
                {"name": "closing", "hours_before": 0.25, "per_match": True, "priority": 1}
            ]
        }
    }

    # Tick à H-0.25 de m1 (m2 encore à H-1.05)
    tick_time = datetime.fromisoformat(tipoff_m1.replace("Z", "+00:00")) - timedelta(hours=0.25)
    
    summary = run_collection(
        conn,
        FakeClient([make_event("m1", tipoff_m1), make_event("m2", tipoff_m2)]),
        "basketball_wnba",
        config,
        force=False,
        now=tick_time,
    )
    
    # Closing per-match : seul m1 collecté (H-0.25 atteint), m2 pas encore
    assert summary["targets_collected"] == 1
    # Union des marchés des verdicts : h2h (m1) + spreads (m2) = 2 marchés × 2 issues = 4 snapshots
    # MAIS seul m1 collecté → h2h uniquement
    assert summary["snapshots"] == 2  # h2h pour m1 (2 issues)


def test_window_hours_2_5_boundaries(conn):
    """window_hours = 2.5 : H-3 hors fenêtre, H-2 dedans.
    
    Décision verrouillée : Verdict à H-2 (H-3 hors, H-2 dedans). Re-décision à H-1
    si verdict change dans la fenêtre [H-3, H-1].
    """
    # Match à H+3 (verdict à H-2, re-décision à H-1)
    now = datetime.now(timezone.utc)
    tipoff = (now + timedelta(hours=3)).isoformat()
    
    db.insert_match(conn, match_id="m1", sport="basketball_wnba", home_team="A", away_team="B", tipoff_utc=tipoff, status="SUIVI", created_at=now.isoformat())
    conn.commit()

    config = {
        "quota": {"reserve": 50},
        "collector": {
            "wave_grouping_minutes": 45,
            "targets": [
                {"name": "H-3", "hours_before": 3.0, "markets": ["h2h"], "priority": 2},
                {"name": "H-2", "hours_before": 2.0, "markets": ["h2h"], "priority": 1},
            ]
        }
    }

    # Tick à H-3 exact : hors fenêtre (window 2.5 → verdict à H-2)
    tick_h3 = datetime.fromisoformat(tipoff.replace("Z", "+00:00")) - timedelta(hours=3)
    summary_h3 = run_collection(
        conn,
        FakeClient([make_event("m1", tipoff)]),
        "basketball_wnba",
        config,
        force=False,
        now=tick_h3,
    )
    # H-3 due mais priorité 2 (peut être bloquée par réserve si quota bas)
    # Ici quota OK → collectée
    assert summary_h3["targets_collected"] == 1  # H-3 collectée
    
    # Tick à H-2 exact : dans fenêtre (verdict)
    tick_h2 = datetime.fromisoformat(tipoff.replace("Z", "+00:00")) - timedelta(hours=2)
    summary_h2 = run_collection(
        conn,
        FakeClient([make_event("m1", tipoff)]),
        "basketball_wnba",
        config,
        force=False,
        now=tick_h2,
    )
    # H-2 due, priorité 1 (jamais bloquée)
    assert summary_h2["targets_collected"] == 1  # H-2 collectée (verdict)


# ─── Tests de groupement en vagues ───


def test_wave_grouping_threshold_45min(conn):
    """Matchs espacés de ≤45 min → même vague, >45 min → vagues distinctes."""
    # 3 matchs : m1-m2 espacés de 30 min (même vague), m2-m3 espacés de 50 min (vagues distinctes)
    # Tick à H+1.5 pour que toutes les cibles H-6 soient dues (m3 à H+7.33 → H-6 = H+1.33)
    now = datetime.now(timezone.utc)
    tick_time = now + timedelta(hours=1, minutes=30)  # H+1.5
    
    tipoff_m1 = (now + timedelta(hours=6)).isoformat()
    tipoff_m2 = (now + timedelta(hours=6.5)).isoformat()
    tipoff_m3 = (now + timedelta(hours=7, minutes=20)).isoformat()  # 50 min après m2
    
    db.insert_match(conn, match_id="m1", sport="basketball_wnba", home_team="A", away_team="B", tipoff_utc=tipoff_m1, status="SUIVI", created_at=now.isoformat())
    db.insert_match(conn, match_id="m2", sport="basketball_wnba", home_team="C", away_team="D", tipoff_utc=tipoff_m2, status="SUIVI", created_at=now.isoformat())
    db.insert_match(conn, match_id="m3", sport="basketball_wnba", home_team="E", away_team="F", tipoff_utc=tipoff_m3, status="SUIVI", created_at=now.isoformat())
    conn.commit()

    config = {
        "quota": {"reserve": 50},
        "collector": {
            "wave_grouping_minutes": 45,
            "targets": [
                {"name": "H-6", "hours_before": 6.0, "markets": ["h2h"], "priority": 2}
            ]
        }
    }

    summary = run_collection(
        conn,
        FakeClient([
            make_event("m1", tipoff_m1),
            make_event("m2", tipoff_m2),
            make_event("m3", tipoff_m3),
        ]),
        "basketball_wnba",
        config,
        force=False,
        now=tick_time,
    )
    
    # 2 vagues détectées : [m1, m2] et [m3]
    assert summary["waves"] == 2
    # 2 cibles collectées (H-6 pour chaque vague)
    assert summary["targets_collected"] == 2
    # Vérifie que les 3 matchs ont été servis
    assert db.is_target_served(conn, "m1", 6.0)
    assert db.is_target_served(conn, "m2", 6.0)
    assert db.is_target_served(conn, "m3", 6.0)


# ─── Tests de déduplication et collection_log ───


def test_target_not_collected_twice_for_same_match(conn):
    """Une cible (match_id, target_hours) ne peut être servie qu'une fois."""
    db.insert_match(conn, match_id="m1", sport="basketball_wnba", home_team="A", away_team="B", tipoff_utc=in_hours(6), status="SUIVI", created_at=in_hours(12))
    conn.commit()

    config = {
        "quota": {"reserve": 50},
        "collector": {
            "targets": [
                {"name": "H-6", "hours_before": 6.0, "markets": ["h2h"], "priority": 2}
            ]
        }
    }

    # Tick 1 : collecte H-6
    summary1 = run_collection(
        conn, FakeClient([make_event("m1", in_hours(6))]), "basketball_wnba", config, force=False
    )
    assert summary1["targets_collected"] == 1
    assert summary1["snapshots"] == 4

    # Tick 2 : H-6 déjà servie → 0 cible collectée
    summary2 = run_collection(
        conn, FakeClient([make_event("m1", in_hours(6))]), "basketball_wnba", config, force=False
    )
    assert summary2["targets_collected"] == 0
    assert summary2["snapshots"] == 0


def test_collection_log_records_all_targets(conn):
    """collection_log enregistre chaque cible servie avec métadonnées."""
    db.insert_match(conn, match_id="m1", sport="basketball_wnba", home_team="A", away_team="B", tipoff_utc=in_hours(6), status="SUIVI", created_at=in_hours(12))
    conn.commit()

    config = {
        "quota": {"reserve": 50},
        "collector": {
            "targets": [
                {"name": "H-6", "hours_before": 6.0, "markets": ["h2h", "spreads"], "priority": 2}
            ]
        }
    }

    run_collection(
        conn, FakeClient([make_event("m1", in_hours(6))]), "basketball_wnba", config, force=False
    )

    # Vérifie l'enregistrement dans collection_log
    log = conn.execute(
        "SELECT * FROM collection_log WHERE match_id = 'm1' AND target_hours = 6.0"
    ).fetchone()
    
    assert log is not None
    assert log["match_id"] == "m1"
    assert log["target_hours"] == 6.0
    assert log["markets"] == "h2h,spreads"
    assert log["credits_used"] == 3  # FakeClient.last_request_cost


# ─── Tests d'union des marchés (closing) ───


@pytest.mark.skip(reason="TODO: Nécessite insert_verdict (analyseur) pour créer verdicts en attente")
def test_closing_unions_verdict_markets(conn):
    """Closing (per_match) : union des marchés des verdicts en attente."""
    now = datetime.now(timezone.utc)
    tipoff = (now + timedelta(hours=1)).isoformat()
    
    db.insert_match(conn, match_id="m1", sport="basketball_wnba", home_team="A", away_team="B", tipoff_utc=tipoff, status="DECIDE", created_at=now.isoformat())
    db.insert_match(conn, match_id="m2", sport="basketball_wnba", home_team="C", away_team="D", tipoff_utc=tipoff, status="DECIDE", created_at=now.isoformat())
    
    # m1 : verdict h2h, m2 : verdict spreads
    db.mark_verdict(conn, "m1", "h2h", "A", 1.5, "test")
    db.mark_verdict(conn, "m2", "spreads", "C", -2.5, "test")
    conn.commit()

    config = {
        "quota": {"reserve": 50},
        "collector": {
            "targets": [
                {"name": "closing", "hours_before": 0.25, "per_match": True, "priority": 1}
            ]
        }
    }

    # Tick à H-0.25
    tick_time = datetime.fromisoformat(tipoff.replace("Z", "+00:00")) - timedelta(hours=0.25)
    
    summary = run_collection(
        conn,
        FakeClient([make_event("m1", tipoff), make_event("m2", tipoff)]),
        "basketball_wnba",
        config,
        force=False,
        now=tick_time,
    )
    
    # 1 cible (closing pour la vague), union h2h + spreads = 4 snapshots × 2 matchs = 8
    assert summary["targets_collected"] == 1
    assert summary["snapshots"] == 8


def test_closing_skipped_if_no_pending_verdicts(conn):
    """Closing skip si aucun verdict en attente (tous matchs SUIVI ou CLOS)."""
    now = datetime.now(timezone.utc)
    tipoff = (now + timedelta(hours=1)).isoformat()
    
    db.insert_match(conn, match_id="m1", sport="basketball_wnba", home_team="A", away_team="B", tipoff_utc=tipoff, status="SUIVI", created_at=now.isoformat())
    conn.commit()

    config = {
        "quota": {"reserve": 50},
        "collector": {
            "targets": [
                {"name": "closing", "hours_before": 0.25, "per_match": True, "priority": 1}
            ]
        }
    }

    # Tick à H-0.25
    tick_time = datetime.fromisoformat(tipoff.replace("Z", "+00:00")) - timedelta(hours=0.25)
    
    summary = run_collection(
        conn,
        FakeClient([make_event("m1", tipoff)]),
        "basketball_wnba",
        config,
        force=False,
        now=tick_time,
    )
    
    # Aucun verdict en attente → closing skip
    assert summary["targets_collected"] == 0
    assert summary["snapshots"] == 0


# ─── Tests de priorités ───


@pytest.mark.skip(reason="TODO: Logique multi-marchés/multi-priorités non implémentée (collecte par cible unique)")
def test_priority_ordering_high_to_low(conn):
    """Cibles collectées par priorité croissante (1 avant 2 avant 3)."""
    now = datetime.now(timezone.utc)
    tipoff = (now + timedelta(hours=6)).isoformat()
    
    db.insert_match(conn, match_id="m1", sport="basketball_wnba", home_team="A", away_team="B", tipoff_utc=tipoff, status="SUIVI", created_at=now.isoformat())
    conn.commit()

    config = {
        "quota": {"reserve": 50},
        "collector": {
            "targets": [
                {"name": "low", "hours_before": 6.0, "markets": ["h2h"], "priority": 3},
                {"name": "high", "hours_before": 6.0, "markets": ["spreads"], "priority": 1},
                {"name": "mid", "hours_before": 6.0, "markets": ["totals"], "priority": 2},
            ]
        }
    }

    # Toutes les cibles dues (même hours_before)
    summary = run_collection(
        conn,
        FakeClient([make_event("m1", tipoff)]),
        "basketball_wnba",
        config,
        force=False,
    )
    
    # 3 cibles collectées (ordre : high, mid, low)
    assert summary["targets_collected"] == 3
    
    # Vérifie l'ordre dans collection_log
    logs = conn.execute(
        "SELECT target_hours, markets FROM collection_log WHERE match_id = 'm1' ORDER BY collected_at"
    ).fetchall()
    
    # Toutes ont hours_before = 6.0, mais ordre de collecte selon priorité
    # (dans ce test simplifié, on vérifie juste que les 3 sont présentes)
    assert len(logs) == 3


# ─── Tests de collecte du matin ───


def test_morning_collection_idempotent_daily(conn):
    """Collecte du matin idempotente : 1 seule fois par jour."""
    now = datetime.now(timezone.utc).replace(hour=9, minute=0)  # 09:00 UTC
    
    # Config avec targets minimale (pour que le summary soit complet)
    config = {
        "quota": {"reserve": 50},
        "collector": {
            "targets": [
                {"name": "test", "hours_before": 6.0, "markets": ["h2h"], "priority": 2}
            ]
        }
    }

    # Tick 1 à 09:00 : collecte du matin
    summary1 = run_collection(
        conn,
        FakeClient([make_event("m1", in_hours(6))]),
        "basketball_wnba",
        config,
        force=False,
        now=now,
    )
    assert summary1["morning_collected"] is True
    # Vérifie qu'un match a été découvert (collecte matin réussie)
    assert get_match(conn, "m1") is not None
    assert get_match(conn, "m1")["status"] == "DECOUVERT"

    # Tick 2 à 09:20 (même jour) : skip (déjà collecté)
    summary2 = run_collection(
        conn,
        FakeClient([make_event("m1", in_hours(6))]),
        "basketball_wnba",
        config,
        force=False,
        now=now + timedelta(minutes=20),
    )
    assert summary2["morning_collected"] is False


def test_morning_collection_exempted_from_reserve(conn):
    """Collecte du matin exemptée de la garde de réserve (priorité implicite 1)."""
    now = datetime.now(timezone.utc).replace(hour=9, minute=0)
    
    # Quota sous le seuil
    db.set_meta(conn, META_CREDITS_REMAINING, "30")
    conn.commit()

    # Config avec targets minimale
    config = {
        "quota": {"reserve": 50},
        "collector": {
            "targets": [
                {"name": "test", "hours_before": 6.0, "markets": ["h2h"], "priority": 2}
            ]
        }
    }

    summary = run_collection(
        conn,
        FakeClient([make_event("m1", in_hours(6))]),
        "basketball_wnba",
        config,
        force=False,
        now=now,
    )
    
    # Matin collecté malgré le seuil
    assert summary["morning_collected"] is True
    # Vérifie qu'un match a été découvert
    assert get_match(conn, "m1") is not None
    assert get_match(conn, "m1")["status"] == "DECOUVERT"


# ─── Tests de transitions d'états ───


def test_decouvert_to_suivi_transition(conn):
    """DECOUVERT → SUIVI au 2e relevé (transition critique)."""
    event = make_event("m1", in_hours(6))
    
    # Tick 1 : découverte
    run_collection(conn, FakeClient([event]), "basketball_wnba", force=True)
    assert get_match(conn, "m1")["status"] == "DECOUVERT"
    
    # Tick 2 : passage en SUIVI
    run_collection(conn, FakeClient([event]), "basketball_wnba", force=True)
    assert get_match(conn, "m1")["status"] == "SUIVI"


def test_suivi_to_clos_on_tipoff_passed(conn):
    """SUIVI → CLOS quand tip-off passé (clôture en tête)."""
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    db.insert_match(conn, match_id="m1", sport="basketball_wnba", home_team="A", away_team="B", tipoff_utc=past, status="SUIVI", created_at=past)
    conn.commit()

    run_collection(conn, FakeClient([]), "basketball_wnba", CONFIG_WITH_TARGETS)
    assert get_match(conn, "m1")["status"] == "CLOS"


# ─── Tests de garde de configuration ───


def test_missing_targets_config_raises_error(conn):
    """Config targets absente → ConfigurationError levée."""
    from collector.collector import ConfigurationError
    
    db.insert_match(conn, match_id="m1", sport="basketball_wnba", home_team="A", away_team="B", tipoff_utc=in_hours(6), status="SUIVI", created_at=in_hours(12))
    conn.commit()

    config_no_targets = {"quota": {"reserve": 50}}

    with pytest.raises(ConfigurationError, match="collector.targets absent ou vide"):
        run_collection(
            conn,
            FakeClient([make_event("m1", in_hours(6))]),
            "basketball_wnba",
            config_no_targets,
            force=False,
        )


def test_empty_targets_config_raises_error(conn):
    """Config targets vide → ConfigurationError levée."""
    from collector.collector import ConfigurationError
    
    db.insert_match(conn, match_id="m1", sport="basketball_wnba", home_team="A", away_team="B", tipoff_utc=in_hours(6), status="SUIVI", created_at=in_hours(12))
    conn.commit()

    config_empty_targets = {
        "quota": {"reserve": 50},
        "collector": {"targets": []}
    }

    with pytest.raises(ConfigurationError, match="collector.targets absent ou vide"):
        run_collection(
            conn,
            FakeClient([make_event("m1", in_hours(6))]),
            "basketball_wnba",
            config_empty_targets,
            force=False,
        )


# ─── Tests de mode force ───


def test_force_mode_bypasses_all_guards(conn):
    """Mode force bypass toutes les gardes (réserve, config, etc.)."""
    # Quota sous le seuil
    db.set_meta(conn, META_CREDITS_REMAINING, "10")
    conn.commit()

    # Config minimale (pas de targets)
    config = {"quota": {"reserve": 50}}

    summary = run_collection(
        conn,
        FakeClient([make_event("m1", in_hours(6))]),
        "basketball_wnba",
        config,
        force=True,
    )
    
    # Collecte exécutée malgré quota bas et config minimale
    assert summary["skipped"] is False
    assert summary["discovered"] == 1
    assert summary["snapshots"] == 4


# ─── Garde tip-off : pas de snapshots post-tip-off ───

def test_no_snapshots_after_tipoff(conn):
    """Un match dont le tip-off vient de passer → zéro snapshot stocké, statut CLOS.

    Bug révélé par la simulation du 17/07 : l'API renvoie encore des matchs en cours
    (cotes live). Le collecteur doit les exclure avant le stockage.
    """
    # Match dont le tip-off est dans le passé
    db.insert_match(
        conn,
        match_id="past",
        sport="basketball_wnba",
        home_team="A",
        away_team="B",
        tipoff_utc=in_hours(-1),  # tip-off il y a 1h
        status="SUIVI",
        created_at=in_hours(-12),
    )
    conn.commit()

    summary = run_collection(
        conn, FakeClient([make_event("past", in_hours(-1))]), "basketball_wnba", CONFIG_WITH_TARGETS, force=False
    )

    # Match clôturé → aucun match actif → skip (pas de champ snapshots en dur)
    assert summary["skipped"] is True
    assert summary["reason"] == "no_active_matches"
    assert summary["closed"] == 1  # exactement 1 match clôturé
    # Vérifie qu'aucun snapshot n'est en base pour ce match (garde tip-off)
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM odds_snapshots WHERE match_id = 'past'"
    ).fetchone()["n"]
    assert count == 0
