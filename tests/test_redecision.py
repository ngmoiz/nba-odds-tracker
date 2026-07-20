"""Tests de la re-décision H-1 (correctif) : mise à jour en fenêtre + supersession.

On teste `_redecide` directement avec un verdict « nouveau » fabriqué, sans dérouler
tout le moteur de règles : on valide la mécanique (matériel vs non matériel, gel sur
position, double supersession qui ne perd pas l'identifiant, version de logique).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from analyzer.analyzer import _redecide
from analyzer.verdict import DECISION_LOGIC_VERSION, Verdict
from common import db
from common.db import get_connection, init_db


def _verdict(verdict="SIGNAL", selection="Boston Celtics", line=-5.0, odds=1.91) -> Verdict:
    return Verdict(verdict=verdict, selection=selection, market="spreads", line=line,
                   odds_at_verdict=odds, signal_score=6, rules_triggered=["R1"], rationale="…")


@pytest.fixture
def conn(tmp_path: Path):
    """Base avec un match DECIDE et un verdict SIGNAL déjà notifié (message 100)."""
    db_path = tmp_path / "test.db"
    init_db(db_path)
    c = get_connection(db_path)
    c.execute(
        "INSERT INTO matches VALUES ('m1','basketball_nba','Boston Celtics','Miami Heat',"
        "'2026-07-17T00:20:00Z','DECIDE','2026-07-16T09:00:00Z')"
    )
    # Base neuve → le verdict a l'id 1 de façon déterministe.
    db.insert_verdict(c, match_id="m1", verdict="SIGNAL", selection="Boston Celtics",
                      market="spreads", line=-5.0, odds_at_verdict=1.91, signal_score=6,
                      rules_triggered=json.dumps(["R1"]), rationale="…",
                      decided_at="2026-07-16T23:00:00Z", logic_version=2)
    db.set_verdict_notified(c, 1, 100, "2026-07-16T23:01:00Z")  # simule le 1er envoi
    c.commit()
    yield c
    c.close()


def _row(conn):
    return conn.execute("SELECT * FROM verdicts WHERE id = 1").fetchone()


def test_non_material_redecision_updates_fields_silently(conn):
    """Même type + même sélection : score/justificatif mis à jour, prix ET decided_at figés.

    Correctif 2026-07-20 : `odds_at_verdict` et `decided_at` ne bougent PAS sur une
    re-décision non matérielle — sinon le CLV se mesure contre un prix qui n'est pas
    celui de la vraie décision (bug constaté : verdict et clôture sur le même
    snapshot quand la re-décision tombait au tick de clôture).
    """
    _redecide(conn, "m1", _verdict(odds=1.70), "2026-07-16T23:30:00Z")
    row = _row(conn)
    assert row["odds_at_verdict"] == 1.91                # figé : PAS 1.70
    assert row["decided_at"] == "2026-07-16T23:00:00Z"   # figé : PAS la nouvelle heure
    assert row["superseded_message_id"] is None          # pas de supersession
    assert row["notified_at"] is not None                # reste notifié (silencieux)
    assert row["telegram_message_id"] == 100             # message inchangé


def test_material_redecision_supersedes(conn):
    """Changement de type → supersession : ancien message à éditer, remis en file."""
    _redecide(conn, "m1", _verdict(verdict="NO_BET"), "2026-07-16T23:30:00Z")
    row = _row(conn)
    assert row["verdict"] == "NO_BET"
    assert row["superseded_message_id"] == 100           # ancien message mémorisé
    assert row["telegram_message_id"] is None            # message courant effacé
    assert row["notified_at"] is None                    # remis en file d'envoi


def test_material_change_of_selection_supersedes(conn):
    """Même type mais sélection différente → matériel."""
    _redecide(conn, "m1", _verdict(selection="Miami Heat", line=-3.0), "2026-07-16T23:30:00Z")
    assert _row(conn)["superseded_message_id"] == 100


def test_double_supersession_keeps_original_message_id(conn):
    """Deux re-décisions matérielles sans passage du notificateur : id d'origine préservé."""
    _redecide(conn, "m1", _verdict(verdict="ANOMALIE"), "2026-07-16T23:30:00Z")
    assert _row(conn)["superseded_message_id"] == 100 and _row(conn)["telegram_message_id"] is None
    # 2e re-décision : telegram_message_id est NULL → COALESCE ne doit PAS écraser 100.
    _redecide(conn, "m1", _verdict(verdict="NO_BET"), "2026-07-16T23:45:00Z")
    assert _row(conn)["superseded_message_id"] == 100    # toujours l'id d'origine, pas perdu


def test_frozen_verdict_is_not_touched_after_position(conn):
    """Une position prise gèle le verdict : plus aucune re-décision."""
    db.insert_position(conn, verdict_id=1, action="take",
                       odds_at_click=1.85, clicked_at="2026-07-16T23:10:00Z")
    conn.commit()
    _redecide(conn, "m1", _verdict(verdict="NO_BET"), "2026-07-16T23:30:00Z")
    row = _row(conn)
    assert row["verdict"] == "SIGNAL"                    # inchangé (gelé)
    assert row["superseded_message_id"] is None
    assert row["telegram_message_id"] == 100


def test_redecision_stamps_logic_version(conn):
    """La re-décision estampille la version de logique courante."""
    conn.execute("UPDATE verdicts SET logic_version = 1 WHERE id = 1")
    conn.commit()
    _redecide(conn, "m1", _verdict(odds=1.75), "2026-07-16T23:30:00Z")
    assert _row(conn)["logic_version"] == DECISION_LOGIC_VERSION
