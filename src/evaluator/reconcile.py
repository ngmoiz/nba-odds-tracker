"""Appariement d'un match suivi (The Odds API) avec son résultat (balldontlie).

Les deux sources ont des identifiants différents : on apparie par **noms d'équipes
normalisés** + **proximité de date**. La date de match côté balldontlie est une date
calendaire US (fuseau de la ligue), alors que le tip-off est stocké en UTC : on
convertit d'abord le tip-off dans le fuseau du calendrier, puis on tolère un écart
d'un jour pour absorber les cas limites de fuseau.

Fonctions pures (aucune base ni réseau), testables directement.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from common.results_api_client import GameResult


def normalize_team(name: str) -> str:
    """Normalise un nom d'équipe pour la comparaison (casse et espaces)."""
    return " ".join(name.strip().lower().split())


def teams_match(name1: str, name2: str) -> bool:
    """Vérifie si deux noms d'équipes correspondent (exact ou partiel).
    
    Accepte une correspondance si :
    - Les noms normalisés sont identiques (ex: "Boston Celtics" == "boston celtics")
    - Un nom contient l'autre (ex: "Toronto Tempo" contient "Tempo")
    
    Cette flexibilité gère les incohérences entre The Odds API et balldontlie
    (ex: "Toronto Tempo" vs "Tempo", "Portland Fire" vs "Fire").
    """
    n1, n2 = normalize_team(name1), normalize_team(name2)
    return n1 == n2 or n1 in n2 or n2 in n1


def tipoff_calendar_date(tipoff_utc: str, calendar_tz: str) -> date:
    """Date calendaire du match dans le fuseau de la ligue (US), depuis le tip-off UTC."""
    dt = datetime.fromisoformat(tipoff_utc.replace("Z", "+00:00"))
    return dt.astimezone(ZoneInfo(calendar_tz)).date()


def find_result(
    games: list[GameResult],
    *,
    home_team: str,
    away_team: str,
    tipoff_utc: str,
    calendar_tz: str,
    max_day_gap: int = 1,
) -> GameResult | None:
    """Trouve le résultat correspondant au match, ou None.

    Critères : noms d'équipes correspondants (exact ou partiel) et date balldontlie à
    ±`max_day_gap` jour de la date calendaire du tip-off. En cas de plusieurs candidats,
    on prend le plus proche en date.
    
    Le matching flexible gère les incohérences entre APIs (ex: "Toronto Tempo" vs "Tempo").
    """
    target = tipoff_calendar_date(tipoff_utc, calendar_tz)

    best: GameResult | None = None
    best_gap = timedelta(days=max_day_gap + 1)
    for game in games:
        # Matching flexible : accepte correspondance exacte ou partielle
        if not (teams_match(game.home_team, home_team) and teams_match(game.away_team, away_team)):
            continue
        gap = abs(date.fromisoformat(game.game_date) - target)
        if gap <= timedelta(days=max_day_gap) and gap < best_gap:
            best, best_gap = game, gap
    return best
