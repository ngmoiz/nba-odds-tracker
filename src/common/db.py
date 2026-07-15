"""Accès à la base SQLite partagée entre tous les composants.

Choix technique : module standard `sqlite3` (pas d'ORM). Le SQL reste explicite et
lisible, ce qui est suffisant pour ce volume ; la couche sera de toute façon réécrite
pour DynamoDB en phase 4.

La table `odds_snapshots` est **append-only** (règle 0.4.2) : cette garantie est
imposée au niveau de la base par des triggers qui rejettent tout UPDATE/DELETE.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from common.logging_config import get_logger

logger = get_logger("db")

# Schéma complet de la base (section 5 du CLAUDE.md).
# Tables, index, puis triggers append-only sur odds_snapshots.
SCHEMA = """
-- Matchs et leur cycle de vie (machine à états).
CREATE TABLE IF NOT EXISTS matches (
    match_id    TEXT PRIMARY KEY,          -- id fourni par The Odds API
    sport       TEXT NOT NULL,             -- 'basketball_nba' (générique pour V2)
    home_team   TEXT NOT NULL,
    away_team   TEXT NOT NULL,
    tipoff_utc  TEXT NOT NULL,             -- heure de tip-off, en UTC
    status      TEXT NOT NULL,             -- DECOUVERT / SUIVI / DECIDE / CLOS / EVALUE
    created_at  TEXT NOT NULL
);

-- Historique brut des cotes : APPEND-ONLY (jamais d'UPDATE/DELETE).
CREATE TABLE IF NOT EXISTS odds_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id    TEXT NOT NULL REFERENCES matches(match_id),
    bookmaker   TEXT NOT NULL,
    market      TEXT NOT NULL,             -- h2h / spreads / totals
    selection   TEXT NOT NULL,             -- équipe, ou Over/Under (totals)
    line        REAL,                      -- valeur de ligne ; NULL pour h2h
    odds        REAL NOT NULL,             -- cote décimale
    snapshot_at TEXT NOT NULL              -- timestamp UTC du relevé
);

-- Alertes temps réel émises pendant le suivi.
CREATE TABLE IF NOT EXISTS alerts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id   TEXT NOT NULL REFERENCES matches(match_id),
    rule       TEXT NOT NULL,
    details    TEXT,
    created_at TEXT NOT NULL
);

-- Verdicts finaux (décision H-1).
CREATE TABLE IF NOT EXISTS verdicts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id        TEXT NOT NULL REFERENCES matches(match_id),
    verdict         TEXT NOT NULL,         -- NO_BET / SIGNAL / ANOMALIE
    selection       TEXT,                  -- sélection concernée (NULL si NO_BET sans pressenti)
    market          TEXT,
    line            REAL,
    odds_at_verdict REAL,                  -- cote au moment du verdict (base du CLV)
    signal_score    INTEGER,
    rules_triggered TEXT,                  -- liste des règles ayant contribué (JSON)
    rationale       TEXT,                  -- justificatif lisible envoyé sur Telegram
    decided_at      TEXT NOT NULL
);

-- Prises de position du développeur (via boutons Telegram).
CREATE TABLE IF NOT EXISTS positions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    verdict_id    INTEGER NOT NULL REFERENCES verdicts(id),
    odds_at_click REAL,                    -- cote au moment du clic (celle qui compte)
    clicked_at    TEXT NOT NULL
);

-- Évaluations du lendemain.
CREATE TABLE IF NOT EXISTS evaluations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    verdict_id   INTEGER NOT NULL REFERENCES verdicts(id),
    home_score   INTEGER,
    away_score   INTEGER,
    verdict_won  INTEGER,                  -- 1/0 ; pour NO_BET : la sélection pressentie serait-elle passée ?
    closing_odds REAL,                     -- cote de clôture (dernier snapshot avant tip-off)
    clv          REAL,                     -- Closing Line Value : odds_at_verdict vs closing_odds
    evaluated_at TEXT NOT NULL
);

-- Index pour accélérer les requêtes fréquentes.
CREATE INDEX IF NOT EXISTS idx_snapshots_match        ON odds_snapshots(match_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_match_market ON odds_snapshots(match_id, market);
CREATE INDEX IF NOT EXISTS idx_snapshots_time         ON odds_snapshots(snapshot_at);
CREATE INDEX IF NOT EXISTS idx_matches_status         ON matches(status);
CREATE INDEX IF NOT EXISTS idx_alerts_match           ON alerts(match_id);
CREATE INDEX IF NOT EXISTS idx_verdicts_match         ON verdicts(match_id);
CREATE INDEX IF NOT EXISTS idx_positions_verdict      ON positions(verdict_id);
CREATE INDEX IF NOT EXISTS idx_evaluations_verdict    ON evaluations(verdict_id);

-- Garantie append-only : la base rejette physiquement toute modification/suppression
-- d'un relevé de cotes existant, quelle que soit l'application ou l'outil.
CREATE TRIGGER IF NOT EXISTS odds_snapshots_no_update
BEFORE UPDATE ON odds_snapshots
BEGIN
    SELECT RAISE(ABORT, 'odds_snapshots est append-only : UPDATE interdit');
END;

CREATE TRIGGER IF NOT EXISTS odds_snapshots_no_delete
BEFORE DELETE ON odds_snapshots
BEGIN
    SELECT RAISE(ABORT, 'odds_snapshots est append-only : DELETE interdit');
END;
"""


def get_connection(db_path: Path) -> sqlite3.Connection:
    """Ouvre une connexion SQLite, crée le dossier parent au besoin.

    - Active la vérification des clés étrangères (désactivée par défaut dans SQLite).
    - Utilise `sqlite3.Row` pour accéder aux colonnes par leur nom.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path) -> None:
    """Crée (si absentes) toutes les tables, index et triggers de la base."""
    logger.info("Initialisation de la base : %s", db_path)
    conn = get_connection(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        logger.info("Base prête : tables, index et triggers append-only en place.")
    finally:
        conn.close()


# ─────────────────── Accès aux matchs et relevés ───────────────────
# Fonctions utilisées par le collecteur (et plus tard les autres composants).
# Elles reçoivent une connexion ouverte et ne committent PAS : l'appelant
# décide quand valider la transaction (une collecte = une transaction).

# Statuts « actifs » : un match dans l'un de ces états est encore suivi.
ACTIVE_STATUSES = ("DECOUVERT", "SUIVI", "DECIDE")


def get_match(conn: sqlite3.Connection, match_id: str) -> sqlite3.Row | None:
    """Renvoie la ligne du match, ou None s'il est inconnu."""
    return conn.execute(
        "SELECT * FROM matches WHERE match_id = ?", (match_id,)
    ).fetchone()


def insert_match(
    conn: sqlite3.Connection,
    *,
    match_id: str,
    sport: str,
    home_team: str,
    away_team: str,
    tipoff_utc: str,
    status: str,
    created_at: str,
) -> None:
    """Insère un nouveau match dans la table `matches`."""
    conn.execute(
        "INSERT INTO matches "
        "(match_id, sport, home_team, away_team, tipoff_utc, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (match_id, sport, home_team, away_team, tipoff_utc, status, created_at),
    )


def update_match_status(conn: sqlite3.Connection, match_id: str, status: str) -> None:
    """Met à jour le statut d'un match (machine à états)."""
    conn.execute(
        "UPDATE matches SET status = ? WHERE match_id = ?", (status, match_id)
    )


def insert_snapshot(
    conn: sqlite3.Connection,
    *,
    match_id: str,
    bookmaker: str,
    market: str,
    selection: str,
    line: float | None,
    odds: float,
    snapshot_at: str,
) -> None:
    """Ajoute un relevé de cote (append-only : uniquement des INSERT)."""
    conn.execute(
        "INSERT INTO odds_snapshots "
        "(match_id, bookmaker, market, selection, line, odds, snapshot_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (match_id, bookmaker, market, selection, line, odds, snapshot_at),
    )


def insert_alert(
    conn: sqlite3.Connection,
    *,
    match_id: str,
    rule: str,
    details: str,
    created_at: str,
) -> None:
    """Enregistre une alerte temps réel émise par l'analyseur."""
    conn.execute(
        "INSERT INTO alerts (match_id, rule, details, created_at) VALUES (?, ?, ?, ?)",
        (match_id, rule, details, created_at),
    )


def insert_verdict(
    conn: sqlite3.Connection,
    *,
    match_id: str,
    verdict: str,
    selection: str | None,
    market: str | None,
    line: float | None,
    odds_at_verdict: float | None,
    signal_score: int,
    rules_triggered: str,
    rationale: str,
    decided_at: str,
) -> int:
    """Enregistre un verdict final. Renvoie l'identifiant de la ligne créée."""
    cursor = conn.execute(
        "INSERT INTO verdicts "
        "(match_id, verdict, selection, market, line, odds_at_verdict, signal_score, "
        "rules_triggered, rationale, decided_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            match_id, verdict, selection, market, line, odds_at_verdict,
            signal_score, rules_triggered, rationale, decided_at,
        ),
    )
    return cursor.lastrowid


def close_finished_matches(conn: sqlite3.Connection, now_utc: datetime) -> int:
    """Passe en CLOS les matchs actifs dont l'heure de tip-off est dépassée.

    La comparaison se fait sur des datetimes (et non des chaînes) pour gérer sans
    ambiguïté les deux formats UTC ISO ('...Z' de l'API et '+00:00' de Python).
    Renvoie le nombre de matchs clôturés.
    """
    placeholders = ",".join("?" * len(ACTIVE_STATUSES))
    rows = conn.execute(
        f"SELECT match_id, tipoff_utc FROM matches WHERE status IN ({placeholders})",
        ACTIVE_STATUSES,
    ).fetchall()

    closed = 0
    for row in rows:
        tipoff = datetime.fromisoformat(row["tipoff_utc"].replace("Z", "+00:00"))
        if tipoff <= now_utc:
            update_match_status(conn, row["match_id"], "CLOS")
            closed += 1
    return closed
