"""Tests du mode shadow (chantier B5, lot 4) : le modèle observe, il ne décide pas.

Deux familles de garanties :

- **ce que le shadow refuse de calculer** — une note absente ou immature ne produit
  jamais un chiffre, mais une raison lisible (T9/T10). C'est l'invariant 5 appliqué à
  un chemin de lecture : une valeur fausse mais plausible est pire qu'une valeur absente ;
- **ce qu'il ne peut pas casser** — une défaillance du modèle laisse le verdict, le
  statut du match et les alertes intacts (T11).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from analyzer import shadow
from analyzer.analyzer import analyze_open_matches
from analyzer.preprocessing import preprocess
from common import db
from common.config import load_config
from common.db import get_connection, init_db
from evaluator.reconcile import normalize_team
from tests import fixtures as fx

CFG = load_config()
SPORT = "basketball_wnba"
NOW = datetime(2026, 8, 20, 22, 0, tzinfo=timezone.utc)
TIPOFF = (NOW + timedelta(hours=1)).isoformat()
HOME, AWAY = "Las Vegas Aces", "Seattle Storm"
SEEDED = (HOME, AWAY, "Portland Fire", "Toronto Tempo")  # cibles des alias de config.yaml


@pytest.fixture
def conn(tmp_path: Path):
    """Match en fenêtre de décision, avec un mouvement fort et un marché h2h coté."""
    db_path = tmp_path / "test.db"
    init_db(db_path)
    connection = get_connection(db_path)
    db.insert_match(connection, match_id="m1", sport=SPORT, home_team=HOME,
                    away_team=AWAY, tipoff_utc=TIPOFF, status="SUIVI", created_at=fx.T[0])
    for index, team in enumerate(SEEDED):
        connection.execute(
            "INSERT INTO matches VALUES (?,?,?,?,?,?,?)",
            (f"seed{index}", SPORT, team, SEEDED[(index + 1) % len(SEEDED)],
             "2026-08-01T23:00:00Z", "EVALUE", "2026-08-01T09:00:00Z"),
        )
    rows = []
    for book in ("a", "b", "c", "d"):
        rows += fx.spreads(book, HOME, AWAY, -2.0, 1.91, 1.91, fx.T[0])
        rows += fx.spreads(book, HOME, AWAY, -5.0, 1.91, 1.91, fx.T[1])
        rows += fx.h2h(book, HOME, AWAY, 1.90, 1.90, fx.T[0])
        rows += fx.h2h(book, HOME, AWAY, 1.70, 2.15, fx.T[1])
    for row in rows:
        db.insert_snapshot(connection, match_id="m1", **row)
    connection.commit()
    yield connection
    connection.close()


def _rate(conn, team: str, rating: float, games: int, last: str | None = "2026-08-18") -> None:
    db.upsert_team_rating(conn, sport=SPORT, team=normalize_team(team), display_name=team,
                          rating=rating, games_played=games, last_game_date=last,
                          updated_at=NOW.isoformat())
    conn.commit()


def _observe(conn) -> str:
    match = conn.execute("SELECT * FROM matches WHERE match_id='m1'").fetchone()
    return shadow.observe(conn, match, preprocess(conn, "m1"), CFG, verdict_id=7, verdict="SIGNAL")


# ────────────────────── T9/T10 : ce que le shadow refuse ──────────────────────


def test_unrated_team_yields_a_reason_never_an_implicit_1500(conn):
    """T9 — une équipe jamais notée ne produit pas d'edge, elle produit une raison.

    Substituer `initial_rating` calculerait un edge sur une force inventée : c'est
    exactement la famille du bug d'origine du projet (un défaut qui masque l'absence).
    """
    _rate(conn, HOME, 1560.0, 30)  # seule l'équipe à domicile est notée

    ligne = _observe(conn)

    assert "reason=" in ligne and "note absente" in ligne
    assert normalize_team(AWAY) in ligne
    assert "p_model=" not in ligne and "edge=" not in ligne


def test_immature_ratings_yield_a_reason_not_a_number(conn):
    """T10 — sous `min_games_for_edge`, aucun edge n'est produit.

    Garde à tester plutôt qu'à observer : les 15 franchises WNBA portent 30+ matchs,
    elle ne se déclenchera jamais en production cette saison.
    """
    seuil = CFG["model"]["decision"]["min_games_for_edge"]
    _rate(conn, HOME, 1560.0, 30)
    _rate(conn, AWAY, 1480.0, seuil - 1)

    ligne = _observe(conn)

    assert f"notes immatures ({seuil - 1} < {seuil})" in ligne
    assert "edge=" not in ligne
    assert f"games=30/{seuil - 1}" in ligne  # le compteur reste visible, lui


def test_missing_h2h_consensus_yields_a_reason(tmp_path):
    """Pas de marché h2h coté → pas de `p_market`, donc pas d'edge inventé."""
    db_path = tmp_path / "t.db"
    init_db(db_path)
    connection = get_connection(db_path)
    db.insert_match(connection, match_id="m1", sport=SPORT, home_team=HOME, away_team=AWAY,
                    tipoff_utc=TIPOFF, status="SUIVI", created_at=fx.T[0])
    for index, team in enumerate(SEEDED):
        connection.execute("INSERT INTO matches VALUES (?,?,?,?,?,?,?)",
                           (f"s{index}", SPORT, team, SEEDED[(index + 1) % len(SEEDED)],
                            "2026-08-01T23:00:00Z", "EVALUE", "2026-08-01T09:00:00Z"))
    for row in fx.spreads("a", HOME, AWAY, -5.0, 1.91, 1.91, fx.T[1]):
        db.insert_snapshot(connection, match_id="m1", **row)
    connection.commit()
    _rate(connection, HOME, 1560.0, 30)
    _rate(connection, AWAY, 1480.0, 30)

    ligne = _observe(connection)

    assert "p_market indisponible" in ligne
    connection.close()


# ────────────────────── Le chemin nominal : l'observation ──────────────────────


def test_observation_carries_every_field_needed_to_read_it_later(conn):
    """La ligne doit se suffire à elle-même : sans contexte, elle n'est pas exploitable."""
    _rate(conn, HOME, 1560.0, 30)
    _rate(conn, AWAY, 1480.0, 28)

    ligne = _observe(conn)

    assert ligne.startswith("SHADOW ")
    for champ in ("verdict_id=7", "sport=basketball_wnba", "p_model=", "p_market=",
                  "edge=", "games=30/28", "ratings=1560.0/1480.0", "rest=", "n_books=4"):
        assert champ in ligne, f"champ manquant : {champ}"
    assert "reason=" not in ligne


def test_edge_is_the_signed_gap_between_model_and_market_on_the_same_side(conn):
    """`p_model` et `p_market` sont tous deux du côté domicile — sinon l'edge n'a aucun sens.

    L'équipe à domicile est ici bien plus forte (1700 vs 1400) et le marché la sous-cote :
    l'edge doit être franchement positif, et son signe s'inverser quand on inverse les notes.
    """
    _rate(conn, HOME, 1700.0, 30)
    _rate(conn, AWAY, 1400.0, 30)
    fort = _observe(conn)

    _rate(conn, HOME, 1400.0, 30)
    _rate(conn, AWAY, 1700.0, 30)
    faible = _observe(conn)

    edge_fort = float(fort.split("edge=")[1].split()[0])
    edge_faible = float(faible.split("edge=")[1].split()[0])
    assert edge_fort > 0 > edge_faible


def test_back_to_back_lowers_the_model_probability(conn):
    """Le repos entre dans le calcul : un B2B à domicile doit faire baisser `p_model`.

    Vérifie que le shadow consomme bien le contexte reconstruit, et pas seulement les
    notes brutes — sans quoi la reconstruction de `recent_dates` serait décorative.
    """
    _rate(conn, AWAY, 1500.0, 30, last="2026-08-15")
    _rate(conn, HOME, 1500.0, 30, last="2026-08-15")
    repose = float(_observe(conn).split("p_model=")[1].split()[0])

    veille = (datetime.fromisoformat(TIPOFF).date() - timedelta(days=1)).isoformat()
    _rate(conn, HOME, 1500.0, 30, last=veille)
    fatigue = float(_observe(conn).split("p_model=")[1].split()[0])

    assert fatigue < repose


# ─────────────────────── T11 : innocuité sur la décision ───────────────────────


def test_shadow_failure_leaves_the_verdict_untouched(conn, monkeypatch):
    """T11 — un modèle en panne ne casse pas un outil qui décide.

    Le shadow est placé APRÈS l'écriture du verdict : son innocuité est structurelle.
    Ce test vérifie que le filet tient quand même si le calcul explose.
    """
    def explose(*args, **kwargs):
        raise RuntimeError("panne du modèle")

    monkeypatch.setattr(shadow, "observe", explose)

    summary = analyze_open_matches(conn, CFG, NOW)

    assert summary["verdicts"] == 1
    verdict = conn.execute("SELECT verdict, selection FROM verdicts WHERE match_id='m1'").fetchone()
    assert verdict["verdict"] == "SIGNAL" and verdict["selection"] == HOME
    assert conn.execute(
        "SELECT status FROM matches WHERE match_id='m1'"
    ).fetchone()["status"] == "DECIDE"
    assert summary["alerts"] > 0


def test_shadow_writes_nothing_at_all(conn):
    """Le mode shadow est en lecture seule : aucune table ne bouge de son fait."""
    _rate(conn, HOME, 1560.0, 30)
    _rate(conn, AWAY, 1480.0, 28)
    tables = ("team_ratings", "rating_history", "verdicts", "alerts", "evaluations")
    avant = {t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"] for t in tables}

    _observe(conn)

    apres = {t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"] for t in tables}
    assert apres == avant


def test_shadow_is_logged_during_a_real_analysis(conn, caplog):
    """Bout en bout : l'analyse d'un match en fenêtre émet la ligne, avec son verdict_id."""
    _rate(conn, HOME, 1560.0, 30)
    _rate(conn, AWAY, 1480.0, 28)

    with caplog.at_level("INFO"):
        analyze_open_matches(conn, CFG, NOW)

    lignes = [ligne for ligne in caplog.text.splitlines() if "SHADOW " in ligne]
    assert len(lignes) == 1
    verdict_id = conn.execute("SELECT id FROM verdicts WHERE match_id='m1'").fetchone()["id"]
    assert f"verdict_id={verdict_id}" in lignes[0]
    assert "verdict=SIGNAL" in lignes[0]
