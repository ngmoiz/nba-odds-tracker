"""Collecteur : interroge The Odds API, enregistre les relevés, fait avancer la
machine à états des matchs.

Transitions gérées ici :
    (inconnu)   --découverte-->  DECOUVERT   (+ cotes d'ouverture)
    DECOUVERT   --2e relevé--->  SUIVI
    actif       --tip-off passé-> CLOS

Les états DECIDE (analyseur) et EVALUE (évaluateur) sont hors de ce composant.

Collectes conditionnelles (post-1.7) :
- Le créneau du matin (``force=True``) est **inconditionnel** : l'API peut renvoyer
  de nouveaux matchs non encore en base, on doit toujours interroger.
- Les autres créneaux (``force=False``) sont **conditionnels** : si aucun match
  actif n'est en base, la collecte est sautée (zéro crédit consommé).

Garde de réserve (post-1.7) :
- Le dernier quota connu (``x-requests-remaining``) est persisté dans ``meta``.
- Avant une collecte conditionnelle, si le quota restant est sous ``quota.reserve``,
  la collecte est sautée et une notification Telegram avertit le développeur (une
  seule fois par franchissement, déduplication via ``meta['reserve_alerted']``).
- La collecte du matin (inconditionnelle) rafraîchit le quota : si celui-ci repasse
  au-dessus du seuil, la garde est levée (``reserve_alerted`` remis à false) et une
  notification de levée est envoyée.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from common import db
from common.config import Settings
from common.logging_config import get_logger
from common.odds_api_client import OddsEvent

logger = get_logger("collector")

# Clés de la table `meta` pour la persistance du quota et de la garde de réserve.
META_CREDITS_REMAINING = "credits_remaining"
META_RESERVE_ALERTED = "reserve_alerted"


def _record_snapshots(conn: sqlite3.Connection, event: OddsEvent, snapshot_at: str) -> int:
    """Aplatit un match en lignes de relevés (bookmaker × marché × sélection)."""
    count = 0
    for bookmaker in event.bookmakers:
        for market in bookmaker.markets:
            for outcome in market.outcomes:
                db.insert_snapshot(
                    conn,
                    match_id=event.id,
                    bookmaker=bookmaker.key,
                    market=market.key,
                    selection=outcome.name,
                    line=outcome.point,  # None pour h2h
                    odds=outcome.price,
                    snapshot_at=snapshot_at,
                )
                count += 1
    return count


def _parse_credits(value: str | None) -> int | None:
    """Convertit la valeur de l'en-tête x-requests-remaining en int, ou None."""
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _check_reserve(
    conn: sqlite3.Connection, config: dict, settings: Settings | None = None,
    telegram_client=None,
) -> bool:
    """Vérifie la garde de réserve avant une collecte conditionnelle.

    Renvoie True si la collecte peut continuer, False si elle doit être sautée.
    - Si le quota restant est inconnu (premier run), on laisse passer.
    - Si le quota restant est sous ``quota.reserve`` :
      - première fois : notification Telegram + ``reserve_alerted = true`` → skip.
      - fois suivantes : ``reserve_alerted`` déjà true → skip silencieux.
    """
    reserve_threshold = config.get("quota", {}).get("reserve", 0)
    if reserve_threshold <= 0:
        return True  # garde désactivée

    credits = _parse_credits(db.get_meta(conn, META_CREDITS_REMAINING))
    if credits is None:
        return True  # quota inconnu (premier run) : on laisse passer

    if credits >= reserve_threshold:
        return True  # assez de réserve

    # Sous le seuil : skip + notification (dédupliquée).
    alerted = db.get_meta(conn, META_RESERVE_ALERTED) == "true"
    if not alerted:
        db.set_meta(conn, META_RESERVE_ALERTED, "true")
        conn.commit()
        logger.warning(
            "Garde de réserve déclenchée : %d crédits restants < seuil %d. "
            "Collecte sautée.", credits, reserve_threshold,
        )
        if settings is not None:
            _notify_reserve(settings, credits, reserve_threshold, client=telegram_client)
    else:
        logger.info(
            "Garde de réserve active (%d < %d) : collecte sautée silencieusement.",
            credits, reserve_threshold,
        )
    return False


def _notify_reserve(
    settings: Settings, credits: int, threshold: int, client=None,
) -> None:
    """Envoie une notification Telegram d'alerte de réserve (best-effort)."""
    try:
        from notifier.direct import send_direct
        text = (
            "⚠️ Garde de réserve déclenchée\n"
            f"Crédits restants : {credits} (seuil : {threshold})\n"
            "Les collectes non essentielles sont suspendues jusqu'au reset mensuel."
        )
        send_direct(settings, text, client=client)
    except Exception as exc:  # noqa: BLE001 — best-effort, ne bloque pas la collecte
        logger.warning("Notification de réserve non envoyée : %s", exc)


def _notify_reserve_lifted(
    settings: Settings, credits: int, threshold: int, client=None,
) -> None:
    """Envoie une notification Telegram de levée de garde (best-effort)."""
    try:
        from notifier.direct import send_direct
        text = (
            "✅ Garde de réserve levée\n"
            f"Crédits restants : {credits} (seuil : {threshold})\n"
            "Les collectes conditionnelles reprennent."
        )
        send_direct(settings, text, client=client)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Notification de levée de réserve non envoyée : %s", exc)


def _maybe_lift_reserve(
    conn: sqlite3.Connection, config: dict, settings: Settings | None = None,
    telegram_client=None,
) -> None:
    """Après une collecte, lève la garde de réserve si le quota repasse au-dessus du seuil.

    Appelée après persistance du quota rafraîchi (notamment par la collecte du matin
    inconditionnelle qui rafraîchit ``credits_remaining`` au reset mensuel).
    """
    reserve_threshold = config.get("quota", {}).get("reserve", 0)
    if reserve_threshold <= 0:
        return

    credits = _parse_credits(db.get_meta(conn, META_CREDITS_REMAINING))
    alerted = db.get_meta(conn, META_RESERVE_ALERTED) == "true"
    if alerted and credits is not None and credits >= reserve_threshold:
        db.set_meta(conn, META_RESERVE_ALERTED, "false")
        conn.commit()
        logger.info(
            "Garde de réserve levée : %d crédits restants ≥ seuil %d.",
            credits, reserve_threshold,
        )
        if settings is not None:
            _notify_reserve_lifted(settings, credits, reserve_threshold, client=telegram_client)


def _persist_credits(conn: sqlite3.Connection, client) -> None:
    """Persiste le dernier quota connu en base (si disponible sur le client)."""
    credits = getattr(client, "credits_remaining", None)
    if credits is not None:
        db.set_meta(conn, META_CREDITS_REMAINING, str(credits))


def run_collection(
    conn: sqlite3.Connection,
    client,
    sport: str,
    config: dict | None = None,
    *,
    force: bool = False,
    settings: Settings | None = None,
    telegram_client=None,
) -> dict[str, int | bool]:
    """Exécute une collecte complète pour le sport donné.

    Une collecte = une transaction : on ne committe qu'à la fin, pour que la base
    reste cohérente même en cas d'erreur au milieu.

    Paramètres :
        force : si True, la collecte est inconditionnelle (créneau du matin).
                Si False, la collecte est sautée si aucun match actif en base ou si
                la garde de réserve est déclenchée.
        config : configuration du projet (config.yaml). Requis pour la garde de réserve.
        settings : secrets (pour la notification Telegram de réserve). Optionnel.
    """
    # --- Collectes conditionnelles : skip si aucun match actif ---
    if not force:
        if not db.has_active_matches(conn):
            logger.info("Collecte sautée : aucun match à l'horizon (zéro crédit consommé).")
            return {"skipped": True, "reason": "no_active_matches"}

        # --- Garde de réserve ---
        if config is not None and not _check_reserve(conn, config, settings, telegram_client):
            return {"skipped": True, "reason": "reserve"}

    now = datetime.now(timezone.utc)
    snapshot_at = now.isoformat()

    # --- Clôture en tête de traitement : passe en CLOS les matchs dont le tip-off
    # est passé AVANT de stocker les snapshots. Sans cela, l'API renvoie encore des
    # matchs en cours (cotes live) qui seraient stockés en violation de la règle
    # « CLOS au tip-off, plus aucune collecte » (bug révélé par la simulation du 17/07).
    closed = db.close_finished_matches(conn, now)

    events = client.get_odds()
    credits = getattr(client, "credits_remaining", None) or "?"
    cost = getattr(client, "last_request_cost", None) or "?"
    logger.info(
        "Collecte %s : %d match(s) à venir — crédits restants : %s (coût de la requête : %s).",
        sport, len(events), credits, cost,
    )

    discovered = newly_tracked = snapshots = 0
    for event in events:
        # Garde tip-off : exclut tout match dont le tip-off est passé. L'API peut
        # encore renvoyer des matchs en cours (cotes live) — on ne les stocke pas.
        tipoff = datetime.fromisoformat(event.commence_time.replace("Z", "+00:00"))
        if tipoff <= now:
            logger.info(
                "Match %s ignoré : tip-off passé (%s), cotes live non stockées.",
                event.id, event.commence_time,
            )
            continue

        existing = db.get_match(conn, event.id)
        if existing is None:
            # Nouveau match : on l'enregistre et ce premier relevé = cotes d'ouverture.
            db.insert_match(
                conn,
                match_id=event.id,
                sport=sport,
                home_team=event.home_team,
                away_team=event.away_team,
                tipoff_utc=event.commence_time,
                status="DECOUVERT",
                created_at=snapshot_at,
            )
            discovered += 1
            logger.info(
                "Nouveau match DECOUVERT : %s @ %s (%s)",
                event.away_team, event.home_team, event.id,
            )
        elif existing["status"] == "DECOUVERT":
            # 2e relevé d'un match déjà connu : il entre en phase de suivi.
            db.update_match_status(conn, event.id, "SUIVI")
            newly_tracked += 1

        snapshots += _record_snapshots(conn, event, snapshot_at)

    # Persiste le quota rafraîchi pour la garde de réserve.
    if config is not None:
        _persist_credits(conn, client)

    conn.commit()

    # Après persistance : lève la garde si le quota repasse au-dessus du seuil
    # (utile au reset mensuel, rafraîchi par la collecte du matin inconditionnelle).
    if config is not None and force:
        _maybe_lift_reserve(conn, config, settings, telegram_client)

    summary = {
        "discovered": discovered,
        "newly_tracked": newly_tracked,
        "snapshots": snapshots,
        "closed": closed,
        "skipped": False,
    }
    logger.info("Collecte terminée : %s — crédits restants : %s.", summary, credits)
    return summary