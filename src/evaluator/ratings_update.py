"""Alimentation continue des notes Elo par l'évaluateur (chantier B5, lot 4).

Le backfill a rejoué la saison une fois ; ce module prend le relais match après match,
avec **le même moteur** (`analyzer.ratings.apply_game`) appliqué à un match unique au
lieu d'une saison. Il n'existe donc jamais deux implémentations de la transition Elo :
c'est la raison d'être du module, et sa contrainte de conception principale.

Trois propriétés à ne pas perdre de vue
---------------------------------------
**Population = celle du backfill.** On applique *tous* les matchs terminés de la fenêtre
interrogée, pas seulement ceux que l'outil suit. Un match WNBA joué mais non suivi ferait
sinon dériver les notes du vrai rejeu de saison, et la vérification de cohérence des deux
chemins deviendrait fausse par construction. L'appel réseau est celui que l'évaluateur
fait déjà pour le CLV : **zéro requête supplémentaire, zéro crédit The Odds API**.

**Aucun échec ici ne peut coûter une évaluation.** L'appelant lance cette étape *après*
avoir committé les évaluations, et chaque match est protégé individuellement. C'est la
leçon du micro-lot §0 : une garde bruyante placée au mauvais endroit du flux ne protège
pas la donnée, elle supprime le service.

**Un trou d'intégration se voit.** La fenêtre glissante rattrape un cron manqué, mais
seulement dans sa largeur. Au-delà, des matchs deviennent définitivement inaccessibles —
en silence, car les notes restent plausibles. Deux détections le rendent visible sans
qu'on ait à lancer une vérification : une preuve au niveau de la ligue, une suspicion
par équipe (cf. `detect_league_gap` / `detect_team_gap`).
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from analyzer.model import params_from_config
from analyzer.ratings import (
    ReplayError,
    apply_game,
    sort_games,
    unusable_reason,
)
from analyzer.ratings_store import build_resolver, load_team_state, persist_application
from common import db
from common.logging_config import get_logger
from common.results_api_client import GameResult
from evaluator.reconcile import TeamContractError

logger = get_logger("evaluator.ratings")

SOURCE = "evaluator"

# Écart de jours au-delà duquel l'historique d'une équipe est jugé suspect. Défaut
# aligné sur la formulation « plus de 3 jours » : la garde se déclenche à partir de 4.
_DEFAULT_GAP_WARNING_DAYS = 3


def detect_league_gap(
    latest_integrated: str | None, window_start: str
) -> str | None:
    """Trou **prouvé** au niveau de la ligue, ou `None`.

    Si le match le plus récemment intégré est antérieur au début de la fenêtre que
    l'évaluateur peut encore interroger, l'intervalle entre les deux ne sera jamais
    appliqué : la fenêtre glissante est passée devant lui. Ce n'est pas une suspicion,
    c'est une conséquence arithmétique — d'où un message qui nomme l'intervalle manquant
    plutôt qu'un vague « vérifier les notes ».

    Scénario réel visé : machine éteinte un week-end. Les notes restent plausibles à
    l'inspection, simplement fausses, et rien d'autre ne le signalerait.

    Aucun historique du tout (`None`) n'est pas un trou : c'est une base neuve, ou un
    backfill jamais lancé. Le distinguer évite de crier au loup au premier démarrage.
    """
    if latest_integrated is None:
        return None
    if latest_integrated >= window_start:
        return None
    missing_from = (date.fromisoformat(latest_integrated) + timedelta(days=1)).isoformat()
    missing_to = (date.fromisoformat(window_start) - timedelta(days=1)).isoformat()
    return (
        f"dernier match intégré le {latest_integrated}, or la fenêtre interrogée "
        f"commence le {window_start} : les matchs du {missing_from} au {missing_to} "
        f"ne seront JAMAIS appliqués aux notes (fenêtre glissante passée devant eux)"
    )


def detect_team_gap(
    days_rest: int | None, threshold: int = _DEFAULT_GAP_WARNING_DAYS
) -> bool:
    """Écart suspect entre le dernier match intégré d'une équipe et le match courant.

    Complète `detect_league_gap`, qui ne voit que les trous touchant *toute* la ligue.
    Une équipe dont l'historique est incomplet — un match écarté pour donnée corrompue,
    par exemple — passe sous ce radar-là mais pas sous celui-ci.

    **Faux positifs assumés** : une vraie coupure de calendrier (trêve du All-Star Game,
    intersaison) déclenche aussi cet avertissement. C'est acceptable pour un signal dont
    le rôle est « va regarder », pas « quelque chose est cassé » — et le seuil est
    configurable via `model.ratings.gap_warning_days` si le bruit devient gênant.

    `None` (équipe sans historique) n'est pas un écart : c'est un premier match.
    """
    return days_rest is not None and days_rest > threshold


def _skip(journal: Counter, reasons: list[str], key: str, game: GameResult, why: str) -> None:
    """Écarte un match : compté **et** nommé, jamais silencieux (invariant 6)."""
    journal[key] += 1
    reasons.append(
        f"{game.game_date}  {game.away_team} @ {game.home_team}  "
        f"(id={game.game_id})  — {why}"
    )


def update_ratings_from_games(
    conn: sqlite3.Connection,
    config: dict,
    games: list[GameResult],
    *,
    sport: str,
    window_start: str,
    now: datetime | None = None,
) -> dict:
    """Applique aux notes tous les matchs terminés exploitables. Renvoie des compteurs.

    Committe une fois, à la fin. Les erreurs par match sont attrapées et comptées : un
    match illisible ne doit pas empêcher les autres d'entrer.
    """
    now = now or datetime.now(timezone.utc)
    stamp = now.isoformat()
    params = params_from_config(config, sport=sport)
    gap_threshold = int(
        ((config.get("model") or {}).get("ratings") or {}).get(
            "gap_warning_days", _DEFAULT_GAP_WARNING_DAYS
        )
    )

    journal: Counter = Counter()
    skipped_reasons: list[str] = []

    # Détection de trou, AVANT d'appliquer quoi que ce soit : l'avertissement doit
    # partir même si la fenêtre du jour est vide.
    league_gap = detect_league_gap(
        db.get_latest_rating_history_date(conn, sport), window_start
    )
    if league_gap is not None:
        logger.warning(
            "TROU D'INTÉGRATION des notes Elo (%s) : %s. "
            "Réparation : reconstruction complète (cf. journal des décisions, "
            "entrée B5 lot 4, « procédure de réparation après trou »).",
            sport, league_gap,
        )
        journal["league_gap"] += 1

    resolve = build_resolver(conn, config, sport)

    applied = 0
    for game in sort_games(games):
        reason = unusable_reason(game)
        if reason is not None:
            _skip(journal, skipped_reasons, "unusable", game, reason)
            continue

        if game.game_id is None:
            # Sans identifiant, la contrainte d'unicité ne dédoublonne rien (en SQLite
            # deux NULL sont distincts) : le match serait ré-appliqué à chaque passage
            # de la fenêtre, gonflant les notes un peu plus chaque jour.
            _skip(journal, skipped_reasons, "no_game_id", game,
                  "aucun identifiant source — idempotence impossible")
            continue

        if db.rating_history_has_game(conn, sport, game.game_id):
            journal["already_applied"] += 1
            continue

        try:
            home_key, away_key = resolve(game.home_team), resolve(game.away_team)
        except TeamContractError as exc:
            _skip(journal, skipped_reasons, "unresolved", game, str(exc))
            continue

        # Un match est appliqué en entier ou pas du tout. Sans cette borne, un échec
        # entre les deux équipes laisserait une seule ligne d'historique — que la
        # pré-vérification d'idempotence prendrait ensuite pour un match déjà intégré,
        # figeant la moitié manquante à jamais.
        conn.execute("SAVEPOINT rating_game")
        try:
            game_date = date.fromisoformat(game.game_date)
            states = {
                key: load_team_state(conn, sport, key, as_of=game_date, params=params)
                for key in (home_key, away_key)
            }
            for key, state in states.items():
                gap = (game_date - state.last_game_date).days if state.last_game_date else None
                if detect_team_gap(gap, gap_threshold):
                    logger.warning(
                        "Historique suspect pour %r (%s) : dernier match intégré le %s, "
                        "match courant le %s (%d jours d'écart). Coupure de calendrier "
                        "réelle, ou trou d'intégration — à vérifier.",
                        key, sport, state.last_game_date, game_date, gap,
                    )
                    journal["team_gap"] += 1

            _, application = apply_game(states, game, params, normalize=resolve)
            persist_application(conn, sport, application, source=SOURCE, stamp=stamp)
            conn.execute("RELEASE rating_game")
            applied += 1
        except ReplayError as exc:
            conn.execute("ROLLBACK TO rating_game")
            conn.execute("RELEASE rating_game")
            # Match arrivé après qu'un match ultérieur de la même équipe a été appliqué
            # (reporté, terminé tardivement). Refuser plutôt que d'intégrer le futur
            # dans le passé : les notes resteraient fausses sans aucun signal.
            _skip(journal, skipped_reasons, "out_of_order", game, str(exc))
        except sqlite3.IntegrityError as exc:
            conn.execute("ROLLBACK TO rating_game")
            conn.execute("RELEASE rating_game")
            # La pré-vérification aurait dû l'éviter : y arriver signale un défaut, pas
            # un fonctionnement normal (cf. docstring de `db.insert_rating_history`).
            _skip(journal, skipped_reasons, "duplicate", game, f"doublon en base : {exc}")
        except Exception as exc:  # noqa: BLE001 — un match illisible n'arrête pas les autres
            conn.execute("ROLLBACK TO rating_game")
            conn.execute("RELEASE rating_game")
            _skip(journal, skipped_reasons, "failed", game, f"{type(exc).__name__}: {exc}")

    conn.commit()

    counters = {
        "applied": applied,
        "already_applied": journal["already_applied"],
        "unusable": journal["unusable"],
        "no_game_id": journal["no_game_id"],
        "unresolved": journal["unresolved"],
        "out_of_order": journal["out_of_order"],
        "duplicate": journal["duplicate"],
        "failed": journal["failed"],
        "team_gap": journal["team_gap"],
        "league_gap": journal["league_gap"],
    }
    for line in skipped_reasons:
        logger.warning("Note Elo non appliquée — %s", line)
    return counters
