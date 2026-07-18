"""Client pour l'API de résultats balldontlie (https://docs.balldontlie.io).

Réservé aux **scores officiels** (utilisé par l'évaluateur). The Odds API reste la
seule source de cotes : balldontlie n'entame pas son quota. Le plan gratuit couvre
la NBA et la WNBA.

Le chemin d'endpoint (`/v1/games` pour NBA, `/wnba/v1/games` pour WNBA) est dérivé
automatiquement du sport configuré dans `api.sport` (règle 0.4.7 : pas de constante
codée en dur).

Comme le client The Odds API, on encapsule l'HTTP, on parse le JSON en objets typés,
et on injecte un `transport` httpx pour tester sans réseau.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from common.logging_config import get_logger

logger = get_logger("results_api")

DEFAULT_TIMEOUT = 20.0
# balldontlie plafonne per_page à 100 ; une journée NBA compte ~15 matchs.
_PER_PAGE = 100


class ResultsApiError(Exception):
    """Erreur générique du client balldontlie."""


@dataclass(frozen=True)
class GameResult:
    """Résultat d'un match (endpoint games)."""

    game_date: str        # date calendaire du match, 'YYYY-MM-DD'
    status: str           # 'Final' quand le match est terminé
    home_team: str        # nom complet (ex. 'Boston Celtics')
    away_team: str
    home_score: int
    away_score: int

    @property
    def is_final(self) -> bool:
        """Vrai si le match est terminé (score officiel exploitable).
        
        Accepte 'Final' (NBA) et 'post' (WNBA) comme statuts de match terminé.
        """
        status_lower = self.status.strip().lower()
        return status_lower in ("final", "post")


class ResultsApiClient:
    """Client synchrone pour balldontlie (endpoint games)."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        games_path: str,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._games_path = games_path
        # balldontlie authentifie par un simple en-tête Authorization.
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={"Authorization": api_key},
        )

    @classmethod
    def from_config(cls, settings, config: dict) -> ResultsApiClient:
        """Construit le client à partir de la configuration du projet.
        
        Le chemin d'endpoint est dérivé automatiquement du sport configuré dans
        `api.sport` (règle 0.4.7). Si le sport n'a pas de chemin configuré, une
        erreur explicite est levée.
        """
        sport = config["api"]["sport"]
        results = config["results"]
        games_paths = results["games_paths"]
        
        try:
            games_path = games_paths[sport]
        except KeyError:
            raise ResultsApiError(
                f"Aucun chemin balldontlie configuré pour le sport '{sport}'. "
                f"Sports disponibles : {list(games_paths.keys())}"
            )
        
        return cls(
            api_key=settings.balldontlie_api_key,
            base_url=results["base_url"],
            games_path=games_path,
        )

    def __enter__(self) -> ResultsApiClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def get_games(self, start_date: str, end_date: str) -> list[GameResult]:
        """Récupère les matchs entre deux dates incluses ('YYYY-MM-DD').

        Suit la pagination par curseur de balldontlie jusqu'à épuisement.
        """
        games: list[GameResult] = []
        cursor: str | None = None
        while True:
            params: dict[str, object] = {
                "start_date": start_date,
                "end_date": end_date,
                "per_page": _PER_PAGE,
            }
            if cursor is not None:
                params["cursor"] = cursor
            payload = self._get(params)
            games.extend(_parse_game(g) for g in payload.get("data", []))
            cursor = (payload.get("meta") or {}).get("next_cursor")
            if not cursor:
                break
        logger.info("Résultats récupérés : %d matchs entre %s et %s.", len(games), start_date, end_date)
        return games

    def _get(self, params: dict) -> dict:
        try:
            response = self._client.get(self._games_path, params=params)
        except httpx.RequestError as exc:
            raise ResultsApiError(f"Erreur réseau vers balldontlie : {exc}") from exc
        if response.status_code != 200:
            raise ResultsApiError(f"HTTP {response.status_code} : {response.text[:200]}")
        return response.json()


def _parse_game(game: dict) -> GameResult:
    """Convertit un match brut balldontlie en `GameResult`.

    La date renvoyée par l'API est une chaîne ISO (parfois avec l'heure) : on ne
    conserve que la partie calendaire 'YYYY-MM-DD'.
    
    Compatible NBA et WNBA : les scores sont dans `home_score` / `away_score` (pas
    `home_team_score` / `visitor_team_score`).
    """
    return GameResult(
        game_date=str(game["date"])[:10],
        status=str(game.get("status", "")),
        home_team=game["home_team"]["full_name"],
        away_team=game["visitor_team"]["full_name"],
        home_score=int(game["home_score"]),
        away_score=int(game["away_score"]),
    )
