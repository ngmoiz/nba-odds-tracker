"""Tests du notificateur (étape 1.4).

On simule l'API Telegram avec `httpx.MockTransport` : aucun appel réseau, envois
capturés en mémoire. La base est une SQLite temporaire par test (fixture `db`).

Points couverts (Definition of Done) :
- envoi effectif d'une alerte et d'un verdict, avec sélection des seules lignes
  `notified_at IS NULL` ;
- NO_BET jamais envoyé (mais conservé en base) ;
- drapeau R6 présent dans le message de verdict ;
- boutons inline sur un SIGNAL, avec le bon `callback_data` ;
- idempotence : un 2ᵉ passage n'envoie rien ;
- échec d'envoi (HTTP 429) : la ligne reste en attente (non marquée) ;
- no-op propre si Telegram n'est pas configuré ;
- affichage en heure locale (Europe/Paris) ;
- migration `notified_at` idempotente sur une base ancienne.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from common import db
from common.config import Settings
from common.db import get_connection, init_db
from notifier.notifier import notify_pending
from notifier.telegram_client import TelegramClient

CONFIG = {
    "display": {"timezone": "Europe/Paris"},
    "notifier": {
        "verdicts_notified": ["SIGNAL", "ANOMALIE"],
        "verdicts_with_buttons": ["SIGNAL", "ANOMALIE"],
    },
}

SETTINGS = Settings(
    odds_api_key="",
    balldontlie_api_key="",
    telegram_bot_token="tok",
    telegram_chat_id="chat",
    database_path=Path("unused.db"),
    log_level="INFO",
)


# ─────────────────────────── Fixtures & helpers ───────────────────────────

@pytest.fixture
def conn(tmp_path: Path):
    """Base temporaire avec un match en statut DECIDE (tip-off à 02:20 UTC)."""
    db_path = tmp_path / "test.db"
    init_db(db_path)
    connection = get_connection(db_path)
    connection.execute(
        "INSERT INTO matches VALUES "
        "('m1','basketball_nba','Boston Celtics','Miami Heat','2026-07-17T00:20:00Z',"
        "'DECIDE','2026-07-16T09:00:00Z')"
    )
    connection.commit()
    yield connection
    connection.close()


def capturing_client(sent: list[dict], status: int = 200) -> TelegramClient:
    """Client Telegram branché sur un faux transport ; empile les payloads envoyés."""
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(status, json={"ok": status == 200})

    return TelegramClient("tok", "chat", transport=httpx.MockTransport(handler))


def add_alert(conn):
    db.insert_alert(
        conn, match_id="m1", rule="R1",
        details="spread Boston -7.5 → -5.0", created_at="2026-07-16T22:00:00Z",
    )
    conn.commit()


def add_verdict(conn, *, verdict="SIGNAL", rationale="SIGNAL sur Boston (score 6) — R1: ...") -> int:
    vid = db.insert_verdict(
        conn, match_id="m1", verdict=verdict, selection="Boston Celtics",
        market="spreads", line=-5.0, odds_at_verdict=1.91, signal_score=6,
        rules_triggered=json.dumps(["R1", "R4"]), rationale=rationale,
        decided_at="2026-07-16T23:20:00Z",
    )
    conn.commit()
    return vid


def notified_alert_count(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM alerts WHERE notified_at IS NOT NULL"
    ).fetchone()["n"]


def notified_verdict_count(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM verdicts WHERE notified_at IS NOT NULL"
    ).fetchone()["n"]


# ─────────────────────────────── Tests ───────────────────────────────

def test_sends_pending_alert_and_verdict(conn):
    """Une alerte et un verdict en attente sont envoyés puis marqués."""
    add_alert(conn)
    add_verdict(conn)
    sent: list[dict] = []

    summary = notify_pending(conn, SETTINGS, CONFIG, client=capturing_client(sent))

    assert summary == {"alerts": 1, "verdicts": 1}
    assert len(sent) == 2
    assert notified_alert_count(conn) == 1
    assert notified_verdict_count(conn) == 1


def test_no_bet_is_never_sent(conn):
    """Un NO_BET reste en base mais n'est jamais envoyé (absent de verdicts_notified)."""
    add_verdict(conn, verdict="NO_BET", rationale="NO_BET — aucune règle déclenchée (score 0).")
    sent: list[dict] = []

    summary = notify_pending(conn, SETTINGS, CONFIG, client=capturing_client(sent))

    assert summary["verdicts"] == 0
    assert sent == []
    # Non envoyé donc non marqué : la ligne existe toujours, notified_at NULL.
    assert notified_verdict_count(conn) == 0


def test_r6_flag_present_in_verdict_message(conn):
    """Le drapeau R6 rédigé par l'analyseur dans rationale apparaît dans le message."""
    add_verdict(
        conn,
        rationale="SIGNAL sur Boston (score 6) — R1: ... ⚠ divergence bookmaker signalée (R6), signal maintenu.",
    )
    sent: list[dict] = []

    notify_pending(conn, SETTINGS, CONFIG, client=capturing_client(sent))

    assert "R6" in sent[0]["text"]
    assert "divergence bookmaker signalée" in sent[0]["text"]


def test_buttons_on_signal(conn):
    """Un SIGNAL porte les boutons inline avec le callback_data attendu."""
    vid = add_verdict(conn)
    sent: list[dict] = []

    notify_pending(conn, SETTINGS, CONFIG, client=capturing_client(sent))

    markup = sent[0]["reply_markup"]
    buttons = markup["inline_keyboard"][0]
    callbacks = {b["callback_data"] for b in buttons}
    assert callbacks == {f"pos:{vid}", f"skip:{vid}"}


def test_idempotent_second_run_sends_nothing(conn):
    """Après un premier envoi, un second passage n'envoie plus rien."""
    add_alert(conn)
    add_verdict(conn)

    notify_pending(conn, SETTINGS, CONFIG, client=capturing_client([]))
    sent_second: list[dict] = []
    summary = notify_pending(conn, SETTINGS, CONFIG, client=capturing_client(sent_second))

    assert summary == {"alerts": 0, "verdicts": 0}
    assert sent_second == []


def test_failed_send_leaves_row_pending(conn):
    """Sur échec Telegram (429), la ligne n'est pas marquée : renvoi au run suivant."""
    add_verdict(conn)
    sent: list[dict] = []

    summary = notify_pending(conn, SETTINGS, CONFIG, client=capturing_client(sent, status=429))

    assert summary["verdicts"] == 0
    assert notified_verdict_count(conn) == 0  # resté en attente

    # Un passage ultérieur réussi finit par l'envoyer.
    ok: list[dict] = []
    notify_pending(conn, SETTINGS, CONFIG, client=capturing_client(ok))
    assert len(ok) == 1
    assert notified_verdict_count(conn) == 1


def test_no_op_when_not_configured(conn):
    """Sans token/chat_id, aucun envoi n'est tenté (pas de crash)."""
    add_verdict(conn)
    sent: list[dict] = []
    client = TelegramClient("", "", transport=httpx.MockTransport(
        lambda req: sent.append(json.loads(req.content)) or httpx.Response(200, json={})
    ))

    summary = notify_pending(conn, SETTINGS, CONFIG, client=client)

    assert summary == {"alerts": 0, "verdicts": 0}
    assert sent == []
    assert notified_verdict_count(conn) == 0


def test_verdict_message_uses_local_time(conn):
    """Le tip-off UTC 00:20 s'affiche en heure de Paris (02:20 en été, UTC+2)."""
    add_verdict(conn)
    sent: list[dict] = []

    notify_pending(conn, SETTINGS, CONFIG, client=capturing_client(sent))

    assert "17/07 02:20" in sent[0]["text"]


def test_migration_adds_notified_at_column_idempotently(tmp_path: Path):
    """Une base sans notified_at reçoit la colonne, et un 2ᵉ init_db ne casse rien."""
    db_path = tmp_path / "old.db"
    connection = get_connection(db_path)
    # Table alerts « ancienne » sans notified_at.
    connection.execute(
        "CREATE TABLE alerts (id INTEGER PRIMARY KEY, match_id TEXT, rule TEXT, "
        "details TEXT, created_at TEXT)"
    )
    connection.commit()
    connection.close()

    init_db(db_path)  # doit migrer sans erreur
    init_db(db_path)  # idempotent

    connection = get_connection(db_path)
    cols = {r["name"] for r in connection.execute("PRAGMA table_info(alerts)")}
    connection.close()
    assert "notified_at" in cols


# ─────────────────── Supersession / re-décision H-1 (notificateur) ───────────────────

def routing_client(sent: list[dict], edited: list[dict], *,
                   send_status: int = 200, edit_status: int = 200) -> TelegramClient:
    """Client mock qui sépare éditions et envois, et renvoie un message_id croissant."""
    ids = {"n": 500}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path.endswith("/editMessageText"):
            edited.append(body)
            return httpx.Response(edit_status, json={"ok": edit_status == 200})
        sent.append(body)
        mid = ids["n"]
        ids["n"] += 1
        return httpx.Response(send_status, json={"ok": send_status == 200,
                                                 "result": {"message_id": mid}})

    return TelegramClient("tok", "chat", transport=httpx.MockTransport(handler))


def _verdict_row(conn, vid):
    return conn.execute("SELECT * FROM verdicts WHERE id = ?", (vid,)).fetchone()


def test_supersession_edits_old_message_and_resends_updated(conn):
    """Supersession : l'ancien message est édité (« Remplacé »), le nouveau renvoyé (« Mis à jour »)."""
    vid = add_verdict(conn)                       # SIGNAL
    db.set_verdict_notified(conn, vid, 100, "t")  # 1er envoi : message 100
    db.supersede_verdict(conn, vid, 100)          # re-décision matérielle
    conn.commit()
    sent, edited = [], []

    notify_pending(conn, SETTINGS, CONFIG, client=routing_client(sent, edited))

    assert len(edited) == 1 and edited[0]["message_id"] == 100 and "Remplacé" in edited[0]["text"]
    assert len(sent) == 1 and "Mis à jour" in sent[0]["text"] and "reply_markup" in sent[0]
    row = _verdict_row(conn, vid)
    assert row["superseded_message_id"] is None   # effacé après édition réussie
    assert row["notified_at"] is not None
    assert row["telegram_message_id"] == 500       # nouvel id mémorisé


def test_supersession_edit_failure_keeps_superseded_id(conn):
    """Édition en échec : superseded_message_id conservé (réessai), le nouveau message part quand même."""
    vid = add_verdict(conn)
    db.set_verdict_notified(conn, vid, 100, "t")
    db.supersede_verdict(conn, vid, 100)
    conn.commit()
    sent, edited = [], []

    notify_pending(conn, SETTINGS, CONFIG, client=routing_client(sent, edited, edit_status=429))

    assert _verdict_row(conn, vid)["superseded_message_id"] == 100  # non effacé
    assert len(sent) == 1                                            # renvoi tout de même


def test_signal_to_nobet_sends_cancellation(conn):
    """Un signal devenu NO_BET envoie une annulation (malgré NO_BET-silencieux), sans boutons."""
    vid = add_verdict(conn)                        # SIGNAL notifié
    db.set_verdict_notified(conn, vid, 100, "t")
    db.update_verdict_fields(conn, vid, verdict="NO_BET", selection="Boston Celtics",
                             market="spreads", line=-5.0, odds_at_verdict=1.91, signal_score=0,
                             rules_triggered="[]", rationale="…", decided_at="t2", logic_version=2)
    db.supersede_verdict(conn, vid, 100)
    conn.commit()
    sent, edited = [], []

    notify_pending(conn, SETTINGS, CONFIG, client=routing_client(sent, edited))

    assert len(edited) == 1 and "Remplacé" in edited[0]["text"]
    assert len(sent) == 1 and "annulé" in sent[0]["text"].lower()
    assert "reply_markup" not in sent[0]           # une annulation ne se joue pas
    row = _verdict_row(conn, vid)
    assert row["notified_at"] is not None and row["telegram_message_id"] is None


def test_message_id_stored_on_first_send(conn):
    """Le message_id renvoyé par Telegram est mémorisé (base de l'anti-clic-périmé)."""
    vid = add_verdict(conn)
    notify_pending(conn, SETTINGS, CONFIG, client=routing_client([], []))
    assert _verdict_row(conn, vid)["telegram_message_id"] == 500
