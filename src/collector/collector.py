"""Collecteur auto-ordonnancé : interroge The Odds API par vagues, enregistre les
relevés, fait avancer la machine à états des matchs.

Architecture Lot 2 (auto-ordonnancement) :
- Tick anonyme toutes les 20 min (cron battement)
- Groupement des matchs en vagues (seuil 45 min)
- 6 cibles par vague : matin (quotidien), H-6, H-3, H-2 (verdict), H-1 (re-décision),
  H-0.25 (clôture per-match, chaque match collecté sur son marché de verdict)
- Déduplication par (match_id, target_name) : une cible ne peut être servie qu'une
  fois par match (wave_label informatif, pas clé de dédup)
- Garde de réserve par priorités : priorité 1 (verdict/re-décision/clôture) jamais
  bloquée, priorités 2-3 (matin/H-6/H-3) bloquables

Transitions gérées ici :
    (inconnu)   --découverte-->  DECOUVERT   (+ cotes d'ouverture)
    DECOUVERT   --2e relevé--->  SUIVI
    actif       --tip-off passé-> CLOS

Les états DECIDE (analyseur) et EVALUE (évaluateur) sont hors de ce composant.

Collecte du matin (critique pour éviter le gel du système) :
- Évaluée AVANT toute garde conditionnelle (skip si aucun match actif)
- Exemptée de la garde de réserve (priorité implicite 1)
- Idempotente via meta['daily_morning_collected_YYYY-MM-DD']
- Sans cela : base vide → pas de collecte → pas de découverte → gel définitif
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from common import db
from common.config import Settings
from common.logging_config import get_logger
from common.odds_api_client import OddsEvent

logger = get_logger("collector")

# Clés de la table `meta` pour la persistance du quota et de la garde de réserve.
META_CREDITS_REMAINING = "credits_remaining"
META_RESERVE_ALERTED = "reserve_alerted"


class ConfigurationError(Exception):
    """Levée lorsque la configuration du collecteur est invalide ou absente.
    
    Séparation des responsabilités : la logique métier lève une exception,
    le point d'entrée (__main__.py) décide du sort du processus (exit code).
    """
    pass


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


# ─────────────────── Fonctions de base (Lot 2, auto-ordonnancement) ───────────────────


def group_into_waves(
    matches: list[sqlite3.Row], threshold_minutes: int
) -> list[list[sqlite3.Row]]:
    """Groupe les matchs en vagues selon leur tip-off (seuil configurable).
    
    Matchs dont les tip-offs sont espacés de ≤ threshold_minutes → même vague.
    Renvoie une liste de vagues (chaque vague = liste de matchs).
    """
    if not matches:
        return []
    
    waves = []
    current_wave = [matches[0]]
    
    for match in matches[1:]:
        prev_tipoff = datetime.fromisoformat(current_wave[-1]["tipoff_utc"].replace("Z", "+00:00"))
        curr_tipoff = datetime.fromisoformat(match["tipoff_utc"].replace("Z", "+00:00"))
        gap_minutes = (curr_tipoff - prev_tipoff).total_seconds() / 60
        
        if gap_minutes <= threshold_minutes:
            current_wave.append(match)
        else:
            waves.append(current_wave)
            current_wave = [match]
    
    waves.append(current_wave)
    return waves


def compute_wave_earliest_tipoff(wave: list[sqlite3.Row]) -> datetime:
    """Renvoie le tip-off le plus précoce de la vague (min des tip-offs)."""
    tipoffs = [
        datetime.fromisoformat(m["tipoff_utc"].replace("Z", "+00:00"))
        for m in wave
    ]
    return min(tipoffs)


def compute_wave_label(wave: list[sqlite3.Row]) -> str:
    """Génère un label lisible pour la vague (date_heure-heure_Nm).
    
    Exemple : "2026-01-10_01:00-01:30_3m" (3 matchs entre 01:00 et 01:30 UTC).
    """
    tipoffs = [
        datetime.fromisoformat(m["tipoff_utc"].replace("Z", "+00:00"))
        for m in wave
    ]
    earliest = min(tipoffs)
    latest = max(tipoffs)
    return f"{earliest.strftime('%Y-%m-%d_%H:%M')}-{latest.strftime('%H:%M')}_{len(wave)}m"


def compute_due_targets(
    wave: list[sqlite3.Row],
    targets: list[dict],
    now: datetime,
    conn: sqlite3.Connection,
) -> list[dict]:
    """Calcule les cibles dues pour une vague (atteintes ET non servies).
    
    Une cible est due si :
    - now >= (earliest_tipoff - hours_before) [cible atteinte]
    - ET pas encore servie pour TOUS les matchs de la vague (dédup par match_id)
    
    Renvoie une liste de dicts avec :
    - target (config de la cible)
    - target_timestamp (heure cible calculée)
    - matches_needing_collection (matchs de la vague pas encore servis pour cette cible)
    """
    earliest_tipoff = compute_wave_earliest_tipoff(wave)
    due = []
    
    for target in targets:
        # Skip cibles non liées aux vagues (ex: morning avec hours_before: null)
        if target.get("hours_before") is None:
            continue
        
        hours_before = target["hours_before"]
        target_timestamp = earliest_tipoff - timedelta(hours=hours_before)
        
        # Cible atteinte ?
        if now < target_timestamp:
            continue
        
        # Cible per-match (closing) : calculée par match, pas sur earliest
        if target.get("per_match"):
            matches_needing = []
            for match in wave:
                match_tipoff = datetime.fromisoformat(match["tipoff_utc"].replace("Z", "+00:00"))
                match_target_timestamp = match_tipoff - timedelta(hours=hours_before)
                
                # Garde anti-post-tip-off per-match
                if now >= match_tipoff:
                    continue
                
                # Cible atteinte pour ce match ?
                if now < match_target_timestamp:
                    continue
                
                # Déjà servie pour ce match ?
                target_name = target.get("name", f"H-{hours_before}")
                if db.is_target_served(conn, match["match_id"], target_name):
                    continue
                
                matches_needing.append(match)
            
            if matches_needing:
                target_name = target.get("name", f"H-{hours_before}")
                due.append({
                    "target": target,
                    "target_name": target_name,
                    "target_timestamp": target_timestamp.isoformat(),  # informatif (earliest)
                    "matches_needing_collection": matches_needing,
                    "per_match": True,
                })
        else:
            # Cible sur earliest : vérifier dédup pour tous les matchs de la vague
            target_name = target.get("name", f"H-{hours_before}")
            matches_needing = [
                m for m in wave
                if not db.is_target_served(conn, m["match_id"], target_name)
            ]
            
            if matches_needing:
                due.append({
                    "target": target,
                    "target_name": target_name,
                    "target_timestamp": target_timestamp.isoformat(),
                    "matches_needing_collection": matches_needing,
                    "per_match": False,
                })
    
    return due


def _is_morning_time(now: datetime, config: dict) -> bool:
    """Vérifie si l'heure actuelle correspond au créneau du matin (09:00 ±10min).
    
    Tolérance de ±10 min pour absorber les décalages de tick (20 min).
    """
    # Récupère le fuseau d'affichage (config.yaml display.timezone)
    # Pour simplifier V1 : on suppose que le matin = 09:00 UTC (à ajuster si besoin)
    # TODO : utiliser pytz si besoin de gérer les fuseaux proprement
    morning_hour = 9  # 09:00 UTC (à paramétrer si besoin)
    return morning_hour - 1 <= now.hour <= morning_hour + 1


def _should_collect_morning(conn: sqlite3.Connection, now: datetime) -> bool:
    """Vérifie si la collecte du matin doit être exécutée (idempotence quotidienne).
    
    Renvoie True si :
    - On est dans le créneau du matin (09:00 ±10min)
    - ET la collecte du matin n'a pas déjà été faite aujourd'hui
    
    Déduplication via meta['daily_morning_collected_YYYY-MM-DD'].
    """
    today = now.strftime("%Y-%m-%d")
    meta_key = f"daily_morning_collected_{today}"
    already_collected = db.get_meta(conn, meta_key) == "true"
    
    return _is_morning_time(now, {}) and not already_collected


def _mark_morning_collected(conn: sqlite3.Connection, now: datetime) -> None:
    """Marque la collecte du matin comme effectuée pour aujourd'hui."""
    today = now.strftime("%Y-%m-%d")
    meta_key = f"daily_morning_collected_{today}"
    db.set_meta(conn, meta_key, "true")


def _collect_and_record(
    conn: sqlite3.Connection,
    client,
    sport: str,
    markets: list[str],
    match_ids: list[str],
    target_name: str,
    target_hours: float,
    target_timestamp: str,
    wave_label: str,
    now: datetime,
) -> dict[str, int]:
    """Exécute un appel API et enregistre les snapshots pour les matchs donnés.
    
    Renvoie un dict avec discovered, newly_tracked, snapshots.
    Marque chaque match comme servi pour cette cible (anti-doublon).
    """
    snapshot_at = now.isoformat()
    
    # Appel API avec les marchés configurés
    events = client.get_odds(markets=markets)
    credits = getattr(client, "credits_remaining", None) or "?"
    cost = getattr(client, "last_request_cost", None) or "?"
    
    logger.info(
        "Collecte %s (cible H-%.2f, vague %s) : %d match(s) — crédits : %s (coût : %s).",
        sport, target_hours, wave_label, len(events), credits, cost,
    )
    
    discovered = newly_tracked = snapshots = 0
    collected_match_ids = set()
    
    for event in events:
        # Garde tip-off : exclut tout match dont le tip-off est passé
        tipoff = datetime.fromisoformat(event.commence_time.replace("Z", "+00:00"))
        if tipoff <= now:
            logger.info(
                "Match %s ignoré : tip-off passé (%s), cotes live non stockées.",
                event.id, event.commence_time,
            )
            continue
        
        # Ne traiter que les matchs de la vague (filtre côté client)
        if event.id not in match_ids:
            continue
        
        existing = db.get_match(conn, event.id)
        if existing is None:
            # Nouveau match : on l'enregistre (cotes d'ouverture)
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
            # 2e relevé : passage en SUIVI
            db.update_match_status(conn, event.id, "SUIVI")
            newly_tracked += 1
        
        snapshots += _record_snapshots(conn, event, snapshot_at)
        collected_match_ids.add(event.id)
    
    # Marque chaque match collecté comme servi pour cette cible
    for match_id in collected_match_ids:
        db.mark_target_served(
            conn,
            match_id=match_id,
            target_name=target_name,
            target_hours=target_hours,
            target_timestamp=target_timestamp,
            collected_at=snapshot_at,
            markets=",".join(markets),
            credits_used=int(cost) if cost != "?" else 0,
            wave_label=wave_label,
        )
    
    return {
        "discovered": discovered,
        "newly_tracked": newly_tracked,
        "snapshots": snapshots,
        "credits": credits,
        "cost": cost,
    }


def run_collection(
    conn: sqlite3.Connection,
    client,
    sport: str,
    config: dict | None = None,
    *,
    force: bool = False,
    settings: Settings | None = None,
    telegram_client=None,
    now: datetime | None = None,
) -> dict[str, int | bool]:
    """Exécute une collecte auto-ordonnancée (Lot 2, architecture par vague).
    
    Architecture :
    1. Collecte du matin (si créneau 09:00 ±10min, idempotente quotidienne)
    2. Clôture en tête (matchs passés → CLOS)
    3. Groupement des matchs actifs en vagues (seuil 45 min)
    4. Calcul des cibles dues par vague (atteintes ET non servies)
    5. Collecte par cible avec garde de réserve par priorité
    
    Paramètres :
        force : Si True, exécute une collecte complète inconditionnelle (tous marchés,
                tous matchs API, bypass gardes). Usage : tests ou collecte manuelle.
                En production, utiliser le tick anonyme (force=False).
        config : configuration du projet (config.yaml). Requis.
        settings : secrets (pour notifications Telegram). Optionnel.
        now : heure actuelle (injectable pour tests). Défaut : datetime.now(UTC).
    """
    if config is None:
        config = {}
    
    if now is None:
        now = datetime.now(timezone.utc)
    
    # ═══════════════════════════════════════════════════════════════════════════
    # MODE FORCE : Collecte complète inconditionnelle (tests / usage manuel)
    # ═══════════════════════════════════════════════════════════════════════════
    if force:
        logger.info("Mode force : collecte complète inconditionnelle (bypass gardes).")
        
        # Clôture en tête
        closed = db.close_finished_matches(conn, now)
        
        # Collecte tous les marchés configurés
        markets = config.get("api", {}).get("markets", ["h2h", "spreads", "totals"])
        snapshot_at = now.isoformat()
        
        events = client.get_odds(markets=markets)
        credits = getattr(client, "credits_remaining", None) or "?"
        cost = getattr(client, "last_request_cost", None) or "?"
        
        logger.info(
            "Collecte force %s : %d match(s) — crédits : %s (coût : %s).",
            sport, len(events), credits, cost,
        )
        
        discovered = newly_tracked = snapshots = 0
        for event in events:
            tipoff = datetime.fromisoformat(event.commence_time.replace("Z", "+00:00"))
            if tipoff <= now:
                continue
            
            existing = db.get_match(conn, event.id)
            if existing is None:
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
                db.update_match_status(conn, event.id, "SUIVI")
                newly_tracked += 1
            
            snapshots += _record_snapshots(conn, event, snapshot_at)
        
        _persist_credits(conn, client)
        _maybe_lift_reserve(conn, config, settings, telegram_client)
        conn.commit()
        
        return {
            "skipped": False,
            "discovered": discovered,
            "newly_tracked": newly_tracked,
            "snapshots": snapshots,
            "closed": closed,
        }
    
    # ═══════════════════════════════════════════════════════════════════════════
    # ÉTAPE 1 : COLLECTE DU MATIN (critique pour éviter le gel du système)
    # ═══════════════════════════════════════════════════════════════════════════
    # Évaluée AVANT toute garde conditionnelle (skip si aucun match actif).
    # Exemptée de la garde de réserve (priorité implicite 1).
    # Idempotente via meta['daily_morning_collected_YYYY-MM-DD'].
    
    morning_collected = False
    if _should_collect_morning(conn, now):
        logger.info("Créneau du matin détecté : collecte de découverte (inconditionnelle).")
        
        # Collecte tous les marchés configurés (découverte complète)
        markets = config.get("api", {}).get("markets", ["h2h", "spreads", "totals"])
        snapshot_at = now.isoformat()
        
        events = client.get_odds(markets=markets)
        credits = getattr(client, "credits_remaining", None) or "?"
        cost = getattr(client, "last_request_cost", None) or "?"
        
        logger.info(
            "Collecte matin %s : %d match(s) — crédits : %s (coût : %s).",
            sport, len(events), credits, cost,
        )
        
        discovered = newly_tracked = snapshots = 0
        for event in events:
            tipoff = datetime.fromisoformat(event.commence_time.replace("Z", "+00:00"))
            if tipoff <= now:
                continue
            
            existing = db.get_match(conn, event.id)
            if existing is None:
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
                db.update_match_status(conn, event.id, "SUIVI")
                newly_tracked += 1
            
            snapshots += _record_snapshots(conn, event, snapshot_at)
        
        # Persiste crédits + marque matin collecté
        _persist_credits(conn, client)
        _mark_morning_collected(conn, now)
        conn.commit()
        
        # Lève la garde de réserve si quota rafraîchi
        _maybe_lift_reserve(conn, config, settings, telegram_client)
        
        morning_collected = True
        logger.info(
            "Collecte matin terminée : %d découverts, %d suivis, %d snapshots.",
            discovered, newly_tracked, snapshots,
        )
    
    # ═══════════════════════════════════════════════════════════════════════════
    # ÉTAPE 2 : CLÔTURE EN TÊTE (bug cotes live du 17/07)
    # ═══════════════════════════════════════════════════════════════════════════
    closed = db.close_finished_matches(conn, now)
    if closed > 0:
        logger.info("Clôture : %d match(s) passé(s) en CLOS.", closed)
    
    # ═══════════════════════════════════════════════════════════════════════════
    # ÉTAPE 3 : GROUPEMENT EN VAGUES
    # ═══════════════════════════════════════════════════════════════════════════
    active_matches = db.get_matches_by_status(conn, db.ACTIVE_STATUSES)
    
    if not active_matches and not morning_collected:
        logger.info("Aucun match actif : tick sans collecte (zéro crédit consommé).")
        return {
            "skipped": True,
            "reason": "no_active_matches",
            "morning_collected": False,
            "closed": closed,
        }
    
    if not active_matches:
        # Matin collecté mais aucun match actif (rare : tous clôturés entre-temps)
        # RETOUR IMMÉDIAT : la collecte du matin a réussi, pas besoin de targets
        return {
            "skipped": False,
            "morning_collected": True,
            "closed": closed,
            "waves": 0,
            "targets_collected": 0,
        }
    
    # ═══════════════════════════════════════════════════════════════════════════
    # ÉTAPE 4 : COLLECTE PAR VAGUE + CIBLE
    # ═══════════════════════════════════════════════════════════════════════════
    # Groupement en vagues (nécessaire pour le summary même si targets vide)
    wave_threshold = config.get("collector", {}).get("wave_grouping_minutes", 45)
    waves = group_into_waves(active_matches, wave_threshold)
    
    logger.info(
        "Tick collecteur : %d match(s) actif(s), %d vague(s) détectée(s).",
        len(active_matches), len(waves),
    )
    
    # GARDE CRITIQUE : Config targets absente/vide = panne silencieuse → EXIT
    # Vérifiée ICI (après retour matin) pour ne pas neutraliser la garde anti-gel
    targets_config = config.get("collector", {}).get("targets", [])
    
    if not targets_config:
        # Si matin collecté, on retourne un summary valide (matin OK, 0 vague)
        if morning_collected:
            return {
                "skipped": False,
                "morning_collected": True,
                "closed": closed,
                "waves": len(waves),
                "targets_collected": 0,
                "snapshots": 0,
            }
        
        # Sinon, config invalide → exception
        logger.error(
            "ERREUR CRITIQUE : collector.targets absent ou vide dans config.yaml. "
            "Aucune collecte de vagues ne sera exécutée (72 ticks/jour en silence). "
            "Vérifier config.yaml section collector.targets."
        )
        if settings is not None:
            try:
                from notifier.direct import send_direct
                text = (
                    "🔴 ERREUR CRITIQUE COLLECTEUR\n"
                    "collector.targets absent ou vide dans config.yaml\n"
                    "Aucune collecte de vagues ne sera exécutée.\n"
                    "Vérifier immédiatement la configuration."
                )
                send_direct(settings, text, client=telegram_client)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Notification d'erreur config non envoyée : %s", exc)
        
        # Lève une exception : séparation des responsabilités (logique métier vs exit code)
        raise ConfigurationError(
            "collector.targets absent ou vide dans config.yaml. "
            "Aucune collecte de vagues ne sera exécutée."
        )
    
    total_targets_collected = 0
    total_snapshots = 0
    
    for wave in waves:
        wave_label = compute_wave_label(wave)
        due_targets = compute_due_targets(wave, targets_config, now, conn)
        
        if not due_targets:
            continue
        
        logger.info(
            "Vague %s : %d cible(s) due(s) sur %d match(s).",
            wave_label, len(due_targets), len(wave),
        )
        
        for due in due_targets:
            target = due["target"]
            target_name = target.get("name", f"H-{target['hours_before']}")
            priority = target.get("priority", 2)
            matches_needing = due["matches_needing_collection"]
            
            # Garde de réserve par priorité (priorité 1 jamais bloquée)
            if priority > 1:
                if not _check_reserve(conn, config, settings, telegram_client):
                    logger.info(
                        "Cible %s (priorité %d) sautée : garde de réserve active.",
                        target_name, priority,
                    )
                    continue
            
            # Cible per-match (closing) : chaque match collecté sur son marché de verdict
            if due.get("per_match"):
                # Boucle per-match : chaque match collecté individuellement
                for match in matches_needing:
                    # Garde anti-post-tip-off per-match (redondante mais explicite)
                    match_tipoff = datetime.fromisoformat(match["tipoff_utc"].replace("Z", "+00:00"))
                    if now >= match_tipoff:
                        logger.info(
                            "Match %s ignoré (closing) : tip-off passé (%s).",
                            match["match_id"], match["tipoff_utc"],
                        )
                        continue
                    
                    # Récupère les marchés du verdict de CE match
                    markets = db.get_closing_markets_for_match(conn, match["match_id"])
                    
                    if not markets:
                        # Aucun verdict en attente pour ce match : skip
                        logger.info(
                            "Match %s : aucun verdict en attente, closing skippée.",
                            match["match_id"],
                        )
                        continue
                    
                    logger.info(
                        "Closing match %s : marchés %s (verdict).",
                        match["match_id"], markets,
                    )
                    
                    # Collecte ce match uniquement
                    result = _collect_and_record(
                        conn,
                        client,
                        sport,
                        markets,
                        [match["match_id"]],
                        due.get("target_name", target_name),
                        target["hours_before"],
                        due["target_timestamp"],
                        wave_label,
                        now,
                    )
                    
                    total_snapshots += result["snapshots"]
                    total_targets_collected += 1
                    
                    # Persiste crédits après chaque collecte
                    _persist_credits(conn, client)
            else:
                # Cible sur earliest : collecte groupée
                markets = target.get("markets", ["h2h", "spreads", "totals"])
                
                # Collecte + enregistrement
                match_ids = [m["match_id"] for m in matches_needing]
                result = _collect_and_record(
                    conn,
                    client,
                    sport,
                    markets,
                    match_ids,
                    due.get("target_name", target_name),
                    target["hours_before"],
                    due["target_timestamp"],
                    wave_label,
                    now,
                )
                
                total_snapshots += result["snapshots"]
                total_targets_collected += 1
                
                # Persiste crédits après chaque collecte
                _persist_credits(conn, client)
    
    conn.commit()
    
    summary = {
        "skipped": False,
        "morning_collected": morning_collected,
        "closed": closed,
        "waves": len(waves),
        "targets_collected": total_targets_collected,
        "snapshots": total_snapshots,
    }
    
    logger.info(
        "Tick terminé : %d vague(s), %d cible(s) collectée(s), %d snapshots.",
        len(waves), total_targets_collected, total_snapshots,
    )
    
    return summary
