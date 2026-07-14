"""Accès à la base SQLite partagée entre tous les composants.

Choix technique : module standard `sqlite3` (pas d'ORM). Le SQL reste explicite et
lisible, ce qui est suffisant pour ce volume ; la couche sera de toute façon réécrite
pour DynamoDB en phase 4.

La table `odds_snapshots` est **append-only** (règle 0.4.2) : cette garantie est
imposée au niveau de la base par des triggers qui rejettent tout UPDATE/DELETE.
"""
from __future__ import annotations

import sqlite3
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
