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
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id    TEXT NOT NULL REFERENCES matches(match_id),
    rule        TEXT NOT NULL,
    details     TEXT,
    created_at  TEXT NOT NULL,
    notified_at TEXT,                      -- horodatage d'envoi Telegram ; NULL = en attente
    state_key   TEXT                        -- clé de déduplication (sélection + direction)
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
    decided_at      TEXT NOT NULL,
    notified_at     TEXT,                  -- horodatage d'envoi Telegram ; NULL = en attente
    -- Re-décision H-1 (étape 1.6bis) : le verdict est ré-évalué à chaque collecte tant
    -- que le match est dans la fenêtre, jusqu'à gel (position prise) ou tip-off.
    logic_version         INTEGER NOT NULL DEFAULT 1,  -- 1 = pré-correctif H-1, 2 = décision H-1
    telegram_message_id   INTEGER,         -- id du message Telegram actuellement affiché
    superseded_message_id INTEGER          -- id d'un message à éditer/désactiver (supersession en attente)
);

-- Prises de position du développeur (via boutons Telegram).
-- `action` distingue une prise ('take') d'un passage explicite ('pass') : les deux
-- sont des décisions humaines évaluables (résultat + CLV), à ne pas confondre avec
-- l'absence de réaction (aucune ligne). `odds_at_click` est renseigné dans les deux cas.
CREATE TABLE IF NOT EXISTS positions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    verdict_id    INTEGER NOT NULL REFERENCES verdicts(id),
    action        TEXT NOT NULL,           -- 'take' (je me positionne) / 'pass' (je passe)
    odds_at_click REAL,                    -- cote médiane au moment du clic (celle qui compte)
    clicked_at    TEXT NOT NULL
);

-- Évaluations du lendemain.
-- `outcome` est un état métier EXPLICITE (jamais porté par un NULL) : 'push' =
-- remboursement, exclu du dénominateur du taux de réussite (won / (won + lost)).
-- Pour un NO_BET : issue qu'aurait eue la sélection pressentie (mesure des faux négatifs).
-- `invalidated` permet de neutraliser une évaluation erronée (ex: API bug scores 0-0).
CREATE TABLE IF NOT EXISTS evaluations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    verdict_id   INTEGER NOT NULL REFERENCES verdicts(id),
    home_score   INTEGER,
    away_score   INTEGER,
    outcome      TEXT CHECK (outcome IN ('won', 'lost', 'push')),
    closing_odds REAL,                     -- cote de clôture (dernier snapshot avant tip-off)
    clv          REAL,                     -- Closing Line Value : odds_at_verdict vs closing_odds
    evaluated_at TEXT NOT NULL,
    invalidated  INTEGER DEFAULT 0         -- 1 = évaluation neutralisée (exclue des agrégations)
);

-- Métadonnées du projet (quota persisté, état de la garde de réserve, etc.).
-- Clé-valeur simple : évite une table dédiée par méta-donnée.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Traçabilité des collectes auto-ordonnancées (Lot 2, architecture par vague).
-- Déduplication par (match_id, target_name) : une cible ne peut être servie qu'une
-- fois par match. target_hours est informatif. La vague (wave_label) regroupe les
-- appels API mais n'est pas la clé.
CREATE TABLE IF NOT EXISTS collection_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id TEXT NOT NULL,
    target_name TEXT NOT NULL,
    target_hours REAL NOT NULL,       -- Informatif (plusieurs cibles peuvent partager hours_before)
    target_timestamp TEXT NOT NULL,   -- Heure cible calculée (tipoff - hours_before)
    collected_at TEXT NOT NULL,       -- Heure réelle de collecte
    markets TEXT NOT NULL,            -- Marchés collectés (csv)
    credits_used INTEGER NOT NULL,
    wave_label TEXT NOT NULL          -- Label informatif (pas clé de déduplication)
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
CREATE UNIQUE INDEX IF NOT EXISTS idx_match_target     ON collection_log(match_id, target_name);

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


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, coltype: str) -> None:
    """Ajoute une colonne à une table existante si elle est absente (migration).

    SQLite ne propose pas `ADD COLUMN IF NOT EXISTS` : on interroge d'abord le
    schéma (`PRAGMA table_info`) pour ne migrer que les bases antérieures à
    l'ajout de la colonne. Opération idempotente.
    """
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        logger.info("Migration : ajout de %s.%s (%s).", table, column, coltype)
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def _migrate_evaluations_outcome(conn: sqlite3.Connection) -> None:
    """Migre `evaluations.verdict_won` (1/0/NULL) vers `outcome` ('won'/'lost'/'push').

    SQLite < 3.35 ne sait pas `DROP COLUMN` : on reconstruit la table (vide à ce stade)
    plutôt que de laisser une colonne morte. Idempotent : ne fait rien si `outcome`
    existe déjà ou si l'ancienne colonne est absente.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(evaluations)")}
    if not cols or "outcome" in cols or "verdict_won" not in cols:
        return
    logger.info("Migration : evaluations.verdict_won → outcome (reconstruction de table).")
    conn.executescript(
        """
        ALTER TABLE evaluations RENAME TO _evaluations_old;
        CREATE TABLE evaluations (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            verdict_id   INTEGER NOT NULL REFERENCES verdicts(id),
            home_score   INTEGER,
            away_score   INTEGER,
            outcome      TEXT CHECK (outcome IN ('won', 'lost', 'push')),
            closing_odds REAL,
            clv          REAL,
            evaluated_at TEXT NOT NULL
        );
        INSERT INTO evaluations
            (id, verdict_id, home_score, away_score, outcome, closing_odds, clv, evaluated_at)
        SELECT id, verdict_id, home_score, away_score,
               CASE verdict_won WHEN 1 THEN 'won' WHEN 0 THEN 'lost' ELSE 'push' END,
               closing_odds, clv, evaluated_at
        FROM _evaluations_old;
        DROP TABLE _evaluations_old;
        CREATE INDEX IF NOT EXISTS idx_evaluations_verdict ON evaluations(verdict_id);
        """
    )


def init_db(db_path: Path) -> None:
    """Crée (si absentes) toutes les tables, index et triggers de la base."""
    logger.info("Initialisation de la base : %s", db_path)
    conn = get_connection(db_path)
    try:
        conn.executescript(SCHEMA)
        # Bases créées avant l'étape 1.4 : ajout de la colonne de suivi des envois.
        _ensure_column(conn, "alerts", "notified_at", "TEXT")
        _ensure_column(conn, "alerts", "state_key", "TEXT")
        _ensure_column(conn, "verdicts", "notified_at", "TEXT")
        # Base créée avant l'étape 1.5 : ajout de l'action du clic (take/pass).
        # DEFAULT 'take' uniquement pour d'éventuelles lignes préexistantes (aucune en
        # pratique) ; l'application fournit toujours l'action explicitement.
        _ensure_column(conn, "positions", "action", "TEXT NOT NULL DEFAULT 'take'")
        # Base créée avant la finalisation de l'étape 1.6 : verdict_won (NULL=push) → outcome.
        _migrate_evaluations_outcome(conn)
        # Correctif H-1 : re-décision + supersession. Verdicts existants = logique v1.
        _ensure_column(conn, "verdicts", "logic_version", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(conn, "verdicts", "telegram_message_id", "INTEGER")
        _ensure_column(conn, "verdicts", "superseded_message_id", "INTEGER")
        # Correctif J0 (18/07/2026) : colonne invalidated pour neutraliser évaluations erronées.
        _ensure_column(conn, "evaluations", "invalidated", "INTEGER DEFAULT 0")
        # Lot 2 : migration collection_log (match_id, target_hours) → (match_id, target_name)
        _ensure_column(conn, "collection_log", "target_name", "TEXT NOT NULL DEFAULT ''")
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

# Version de la logique de décision. Incrémentée à chaque évolution matérielle du
# calcul du verdict, pour distinguer les cohortes dans les stats/calibration.
#   1 = décision figée à la 1ʳᵉ collecte de la fenêtre (pré-correctif H-1)
#   2 = re-décision à chaque collecte jusqu'au tip-off (décision « à H-1 »)
# Constante de données (pas de construction de verdict) : elle vit dans la couche
# données car elle est lue par l'évaluateur (ségrégation des cohortes) et écrite
# par l'analyseur (estampillage des verdicts). Déplacée depuis analyzer/verdict.py
# lors de la revue externe (M2) pour supprimer la dépendance evaluator→analyzer
# sur une simple constante.
DECISION_LOGIC_VERSION = 2


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
    state_key: str = "",
) -> None:
    """Enregistre une alerte temps réel émise par l'analyseur.

    ``state_key`` encode la sélection et la direction du mouvement : il sert à la
    déduplication par changement d'état (l'analyseur vérifie avant d'insérer).
    """
    conn.execute(
        "INSERT INTO alerts (match_id, rule, details, created_at, state_key) "
        "VALUES (?, ?, ?, ?, ?)",
        (match_id, rule, details, created_at, state_key),
    )


def get_last_alert_state(conn: sqlite3.Connection, match_id: str, rule: str) -> str | None:
    """Renvoie le ``state_key`` de la dernière alerte d'un match/règle, ou None.

    Sert à la déduplication : si le nouvel état est identique au précédent,
    l'analyseur n'insère pas de nouvelle alerte (même mouvement, déjà notifié).
    """
    row = conn.execute(
        "SELECT state_key FROM alerts "
        "WHERE match_id = ? AND rule = ? AND state_key IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
        (match_id, rule),
    ).fetchone()
    return row["state_key"] if row else None


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
    logic_version: int = 1,
) -> int:
    """Enregistre un verdict final. Renvoie l'identifiant de la ligne créée."""
    cursor = conn.execute(
        "INSERT INTO verdicts "
        "(match_id, verdict, selection, market, line, odds_at_verdict, signal_score, "
        "rules_triggered, rationale, decided_at, logic_version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            match_id, verdict, selection, market, line, odds_at_verdict,
            signal_score, rules_triggered, rationale, decided_at, logic_version,
        ),
    )
    return cursor.lastrowid


def get_current_verdict(conn: sqlite3.Connection, match_id: str) -> sqlite3.Row | None:
    """Verdict courant d'un match (un seul, mis à jour en place lors des re-décisions)."""
    return conn.execute(
        "SELECT * FROM verdicts WHERE match_id = ? ORDER BY id DESC LIMIT 1", (match_id,)
    ).fetchone()


def update_verdict_fields(
    conn: sqlite3.Connection,
    verdict_id: int,
    *,
    verdict: str,
    selection: str | None,
    market: str | None,
    line: float | None,
    odds_at_verdict: float | None,
    signal_score: int,
    rules_triggered: str,
    rationale: str,
    decided_at: str,
    logic_version: int,
) -> None:
    """Met à jour les champs de décision d'un verdict (re-décision en fenêtre H-1).

    Ne touche PAS aux champs de notification (`notified_at`, `telegram_message_id`,
    `superseded_message_id`) : ceux-ci sont gérés par `supersede_verdict`.
    """
    conn.execute(
        "UPDATE verdicts SET verdict = ?, selection = ?, market = ?, line = ?, "
        "odds_at_verdict = ?, signal_score = ?, rules_triggered = ?, rationale = ?, "
        "decided_at = ?, logic_version = ? WHERE id = ?",
        (verdict, selection, market, line, odds_at_verdict, signal_score,
         rules_triggered, rationale, decided_at, logic_version, verdict_id),
    )


def supersede_verdict(conn: sqlite3.Connection, verdict_id: int, prior_message_id: int | None) -> None:
    """Marque un verdict re-décidé matériellement : l'ancien message devra être édité.

    `superseded_message_id` reçoit `prior_message_id` **uniquement s'il est non-NULL**
    (COALESCE) : si une supersession est déjà en attente (message pas encore envoyé →
    `telegram_message_id` NULL), on ne perd pas l'identifiant d'origine. Re-met le
    verdict en file (`notified_at = NULL`) et efface le message courant.
    """
    conn.execute(
        "UPDATE verdicts SET "
        "superseded_message_id = COALESCE(?, superseded_message_id), "
        "telegram_message_id = NULL, notified_at = NULL WHERE id = ?",
        (prior_message_id, verdict_id),
    )


def set_verdict_notified(
    conn: sqlite3.Connection, verdict_id: int, message_id: int | None, notified_at: str
) -> None:
    """Marque un verdict comme envoyé et mémorise l'id du message (si fourni)."""
    conn.execute(
        "UPDATE verdicts SET notified_at = ?, "
        "telegram_message_id = COALESCE(?, telegram_message_id) WHERE id = ?",
        (notified_at, message_id, verdict_id),
    )


def clear_superseded(conn: sqlite3.Connection, verdict_id: int) -> None:
    """Efface la supersession en attente (à appeler UNIQUEMENT après une édition réussie)."""
    conn.execute("UPDATE verdicts SET superseded_message_id = NULL WHERE id = ?", (verdict_id,))


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


# ─────────────────── File d'attente des notifications ───────────────────
# Le notificateur (étape 1.4) consomme les lignes `notified_at IS NULL` : la base
# joue le rôle de file d'attente entre l'analyseur (qui écrit) et le notificateur
# (qui envoie). Chaque lecture joint `matches` pour disposer des noms d'équipes et
# de l'heure de tip-off nécessaires à la mise en forme du message.


def get_pending_alerts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Alertes pas encore envoyées sur Telegram, de la plus ancienne à la plus récente."""
    return conn.execute(
        "SELECT a.id, a.match_id, a.rule, a.details, a.created_at, "
        "       m.home_team, m.away_team, m.tipoff_utc "
        "FROM alerts a JOIN matches m ON m.match_id = a.match_id "
        "WHERE a.notified_at IS NULL "
        "ORDER BY a.created_at, a.id"
    ).fetchall()


def get_pending_verdicts(
    conn: sqlite3.Connection, verdict_types: list[str]
) -> list[sqlite3.Row]:
    """Verdicts nécessitant une action du notificateur :

    - non envoyés et d'un type notifiable (ex. SIGNAL/ANOMALIE), OU
    - porteurs d'une **supersession en attente** (`superseded_message_id` non-NULL) :
      l'ancien message doit être édité, quel que soit le type — y compris un NO_BET
      succédant à un signal (message d'annulation).

    Un NO_BET sans antécédent reste hors sélection (jamais envoyé, comme prévu).
    """
    placeholders = ",".join("?" * len(verdict_types)) if verdict_types else "NULL"
    return conn.execute(
        f"SELECT v.*, m.home_team, m.away_team, m.tipoff_utc "
        f"FROM verdicts v JOIN matches m ON m.match_id = v.match_id "
        f"WHERE v.superseded_message_id IS NOT NULL "
        f"   OR (v.notified_at IS NULL AND v.verdict IN ({placeholders})) "
        f"ORDER BY v.decided_at, v.id",
        verdict_types,
    ).fetchall()


def mark_alert_notified(conn: sqlite3.Connection, alert_id: int, notified_at: str) -> None:
    """Marque une alerte comme envoyée (horodatage UTC de l'envoi)."""
    conn.execute("UPDATE alerts SET notified_at = ? WHERE id = ?", (notified_at, alert_id))


# ─────────────────── Prises de position (bot d'écoute, étape 1.5) ───────────────────


def get_verdict(conn: sqlite3.Connection, verdict_id: int) -> sqlite3.Row | None:
    """Renvoie la ligne d'un verdict par son identifiant, ou None."""
    return conn.execute("SELECT * FROM verdicts WHERE id = ?", (verdict_id,)).fetchone()


def get_position(conn: sqlite3.Connection, verdict_id: int) -> sqlite3.Row | None:
    """Renvoie la prise de position existante pour un verdict, ou None.

    Sert à garantir l'idempotence : un seul clic est retenu par verdict (le premier),
    toutes actions confondues (take/pass).
    """
    return conn.execute(
        "SELECT * FROM positions WHERE verdict_id = ? ORDER BY id LIMIT 1", (verdict_id,)
    ).fetchone()


def insert_position(
    conn: sqlite3.Connection,
    *,
    verdict_id: int,
    action: str,
    odds_at_click: float | None,
    clicked_at: str,
) -> int:
    """Enregistre une prise de position ('take') ou un passage ('pass'). Renvoie l'id créé."""
    cursor = conn.execute(
        "INSERT INTO positions (verdict_id, action, odds_at_click, clicked_at) "
        "VALUES (?, ?, ?, ?)",
        (verdict_id, action, odds_at_click, clicked_at),
    )
    return cursor.lastrowid


# ─────────────────── Évaluation du lendemain (évaluateur, étape 1.6) ───────────────────


def get_matches_to_evaluate(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Matchs clos (tip-off passé) pas encore évalués, du plus ancien au plus récent."""
    return conn.execute(
        "SELECT * FROM matches WHERE status = 'CLOS' ORDER BY tipoff_utc"
    ).fetchall()


def get_verdicts_for_match(conn: sqlite3.Connection, match_id: str) -> list[sqlite3.Row]:
    """Verdicts d'un match (en pratique au plus un en V1)."""
    return conn.execute(
        "SELECT * FROM verdicts WHERE match_id = ? ORDER BY id", (match_id,)
    ).fetchall()


def insert_evaluation(
    conn: sqlite3.Connection,
    *,
    verdict_id: int,
    home_score: int,
    away_score: int,
    outcome: str,
    closing_odds: float | None,
    clv: float | None,
    evaluated_at: str,
) -> int:
    """Enregistre l'évaluation d'un verdict. `outcome` ∈ {'won','lost','push'} (état explicite)."""
    cursor = conn.execute(
        "INSERT INTO evaluations "
        "(verdict_id, home_score, away_score, outcome, closing_odds, clv, evaluated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (verdict_id, home_score, away_score, outcome, closing_odds, clv, evaluated_at),
    )
    return cursor.lastrowid


def count_evaluations(conn: sqlite3.Connection) -> int:
    """Nombre total d'évaluations cumulées (garde-fou des 50–100, section 11).
    
    Correctif 3c : exclut les évaluations invalidées (API bug, données erronées).
    """
    return conn.execute("SELECT COUNT(*) AS n FROM evaluations WHERE invalidated = 0").fetchone()["n"]


def count_evaluations_by_logic_version(
    conn: sqlite3.Connection, logic_version: int
) -> int:
    """Nombre d'évaluations cumulées pour une cohorte de logique donnée.

    Le garde-fou règle 11 du rapport hebdo se mesure sur la **cohorte de calibration**
    (v2), pas sur le cumul global : les évaluations v1 (pré-correction H-1) ne doivent
    pas faire basculer le seuil prématurément.
    
    Correctif 3c : exclut les évaluations invalidées.
    """
    return conn.execute(
        "SELECT COUNT(*) AS n FROM evaluations e "
        "JOIN verdicts v ON v.id = e.verdict_id "
        "WHERE v.logic_version = ? AND e.invalidated = 0",
        (logic_version,),
    ).fetchone()["n"]


# ─────────────────── Rapport hebdomadaire (post-1.6) ───────────────────
# Pure agrégation par-dessus `evaluations` + `verdicts` : aucune nouvelle donnée
# produite. La période est glissante (7 jours sur `evaluated_at`).


def get_weekly_signal_evals(conn: sqlite3.Connection, since_iso: str) -> list[sqlite3.Row]:
    """Lignes évaluées SIGNAL sur la période, avec les champs nécessaires à l'agrégation.

    Retourne : verdict_id, logic_version, market, rules_triggered (JSON texte), outcome, clv.
    L'agrégation par marché et par règle se fait en Python (parsing JSON fiable).
    `verdict_id` est inclus pour logger les règles illisibles (parsing défensif non silencieux).
    
    Correctif 3c : exclut les évaluations invalidées.
    """
    return conn.execute(
        "SELECT e.verdict_id, v.logic_version, v.market, v.rules_triggered, e.outcome, e.clv "
        "FROM evaluations e "
        "JOIN verdicts v ON v.id = e.verdict_id "
        "WHERE v.verdict = 'SIGNAL' AND e.evaluated_at >= ? AND e.invalidated = 0 "
        "ORDER BY e.evaluated_at",
        (since_iso,),
    ).fetchall()


def get_weekly_nobet_evals(conn: sqlite3.Connection, since_iso: str) -> list[sqlite3.Row]:
    """Lignes évaluées NO_BET **pressenties** (sélection non-NULL) sur la période.

    Sert à mesurer les faux négatifs : la sélection pressentie aurait-elle gagné ?
    Retourne : logic_version, outcome.
    
    Correctif 3c : exclut les évaluations invalidées.
    """
    return conn.execute(
        "SELECT v.logic_version, e.outcome "
        "FROM evaluations e "
        "JOIN verdicts v ON v.id = e.verdict_id "
        "WHERE v.verdict = 'NO_BET' AND v.selection IS NOT NULL "
        "  AND e.evaluated_at >= ? AND e.invalidated = 0 "
        "ORDER BY e.evaluated_at",
        (since_iso,),
    ).fetchall()


# ─────────────────── Métadonnées et collectes conditionnelles ───────────────────


def has_active_matches(conn: sqlite3.Connection) -> bool:
    """Vrai s'il existe au moins un match en statut actif (DECOUVERT/SUIVI/DECIDE).

    Sert au collecteur conditionnel : si aucun match n'est suivi, la collecte est
    sautée (zéro crédit API consommé). Le créneau du matin reste inconditionnel car
    l'API peut renvoyer de nouveaux matchs non encore en base.
    """
    placeholders = ",".join("?" * len(ACTIVE_STATUSES))
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM matches WHERE status IN ({placeholders})",
        ACTIVE_STATUSES,
    ).fetchone()
    return row["n"] > 0


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    """Récupère une valeur de la table `meta`, ou None si absente."""
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Insère ou met à jour une valeur dans la table `meta` (upsert)."""
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# ─────────────────── Collectes auto-ordonnancées (Lot 2) ───────────────────


def get_matches_by_status(conn: sqlite3.Connection, statuses: tuple[str, ...]) -> list[sqlite3.Row]:
    """Renvoie les matchs dans les statuts donnés, triés par tip-off."""
    placeholders = ",".join("?" * len(statuses))
    return conn.execute(
        f"SELECT * FROM matches WHERE status IN ({placeholders}) ORDER BY tipoff_utc",
        statuses,
    ).fetchall()


def is_target_served(conn: sqlite3.Connection, match_id: str, target_name: str) -> bool:
    """Vérifie si une cible a déjà été servie pour un match donné."""
    row = conn.execute(
        "SELECT 1 FROM collection_log WHERE match_id = ? AND target_name = ?",
        (match_id, target_name),
    ).fetchone()
    return row is not None


def mark_target_served(
    conn: sqlite3.Connection,
    *,
    match_id: str,
    target_name: str,
    target_hours: float,
    target_timestamp: str,
    collected_at: str,
    markets: str,
    credits_used: int,
    wave_label: str,
) -> None:
    """Marque une cible comme servie pour un match (anti-doublon via index unique)."""
    try:
        conn.execute(
            "INSERT INTO collection_log "
            "(match_id, target_name, target_hours, target_timestamp, collected_at, markets, credits_used, wave_label) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (match_id, target_name, target_hours, target_timestamp, collected_at, markets, credits_used, wave_label),
        )
    except sqlite3.IntegrityError:
        # Déjà servi (normal si collecte précédente), skip silencieusement
        pass


def get_closing_markets_for_match(conn: sqlite3.Connection, match_id: str) -> list[str]:
    """Marchés des verdicts en attente pour un match (clôture H-0.25 per-match)."""
    rows = conn.execute(
        """SELECT DISTINCT v.market 
           FROM verdicts v
           JOIN matches m ON v.match_id = m.match_id
           WHERE m.match_id = ? AND m.status = 'DECIDE'""",
        (match_id,),
    ).fetchall()
    return [row["market"] for row in rows]
