"""Client pour l'API de résultats balldontlie (https://docs.balldontlie.io).

Réservé aux **scores officiels** (utilisé par l'évaluateur). The Odds API reste la
seule source de cotes : balldontlie n'entame pas son quota. Le plan gratuit couvre
la NBA et la WNBA.

Le chemin d'endpoint (`/nba/v1/games` pour NBA, `/wnba/v1/games` pour WNBA) est
dérivé automatiquement du sport configuré dans `api.sport` (règle 0.4.7 : pas de
constante codée en dur). Les deux ligues n'exposent pas le même schéma de match —
voir `_parse_game`.

Comme le client The Odds API, on encapsule l'HTTP, on parse le JSON en objets typés,
et on injecte un `transport` httpx pour tester sans réseau.

⚠️ `get_games` ne renvoie que les matchs **terminés**. Une requête par plage de dates
renvoie aussi les matchs programmés ou en cours (scores `null`) : c'est le cas normal,
pas une anomalie — ils sont filtrés et comptés, jamais parsés. Un score absent sur un
match *déclaré terminé* reste une donnée incohérente et lève. Voir `get_games`.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

import httpx

from common.logging_config import get_logger

logger = get_logger("results_api")

DEFAULT_TIMEOUT = 20.0
# balldontlie plafonne per_page à 100 ; une journée NBA compte ~15 matchs.
_PER_PAGE = 100

# Statuts signalant un match terminé (score officiel exploitable). Les deux ligues
# n'emploient pas le même vocabulaire : NBA → 'Final', WNBA → 'post' (schémas vérifiés
# par appel réel le 2026-08-07, cf. journal des décisions). Tout autre statut
# ('Scheduled', 'pre', 'in', quart en cours…) désigne un match non terminé.
_FINAL_STATUSES = ("final", "post")


def _status_is_final(status: str) -> bool:
    """Vrai si le statut brut désigne un match terminé.

    Fonction unique consultée par le filtre de `get_games` (avant parsing) et par
    `GameResult.is_final` (après parsing) : les deux ne peuvent pas diverger.
    """
    return status.strip().lower() in _FINAL_STATUSES


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

        Accepte 'Final' (NBA) et 'post' (WNBA). Toujours vrai sur un objet issu de
        `get_games`, qui filtre en amont ; conservé pour les `GameResult` construits
        directement (tests, futurs appelants).
        """
        return _status_is_final(self.status)


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
        """Récupère les matchs **terminés** entre deux dates incluses ('YYYY-MM-DD').

        Suit la pagination par curseur de balldontlie jusqu'à épuisement.

        Filtrage par statut **avant** parsing, et c'est la raison d'être de l'ordre :
        une plage de dates inclut presque toujours des matchs programmés ou en cours,
        dont les scores valent `null`. Les parser lèverait `ResultsApiError` (garde de
        `_score`) et ferait échouer toute la requête à cause d'un match dont l'absence
        de score est parfaitement normale — l'évaluateur, qui interroge
        `[aujourd'hui − lookback, aujourd'hui]`, en rencontre à chaque exécution.

        Les deux natures d'absence sont donc séparées :

        - **non terminé** → match ignoré, compté, loggé (fonctionnement normal) ;
        - **terminé mais sans score** → `ResultsApiError` (donnée incohérente).

        Le décompte des ignorés est ventilé par statut : un statut inattendu d'une
        future ligue apparaît dans les logs au lieu de disparaître en silence
        (invariant 6, « jamais de no-op silencieux »).
        """
        games: list[GameResult] = []
        skipped: Counter[str] = Counter()
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
            for raw in payload.get("data", []):
                status = str(raw.get("status", ""))
                if not _status_is_final(status):
                    skipped[status or "(statut absent)"] += 1
                    continue
                games.append(_parse_game(raw))
            cursor = (payload.get("meta") or {}).get("next_cursor")
            if not cursor:
                break
        if skipped:
            detail = ", ".join(f"{status}×{count}" for status, count in sorted(skipped.items()))
            logger.info(
                "Matchs non terminés ignorés : %d (%s).", sum(skipped.values()), detail
            )
        logger.info(
            "Résultats récupérés : %d matchs terminés entre %s et %s.",
            len(games), start_date, end_date,
        )
        return games

    def _get(self, params: dict) -> dict:
        try:
            response = self._client.get(self._games_path, params=params)
        except httpx.RequestError as exc:
            raise ResultsApiError(f"Erreur réseau vers balldontlie : {exc}") from exc
        if response.status_code != 200:
            raise ResultsApiError(f"HTTP {response.status_code} : {response.text[:200]}")
        return response.json()


def _score(game: dict, *keys: str) -> int:
    """Lit un score en essayant les conventions de nommage par ligue, dans l'ordre.

    Aucun défaut silencieux (invariant 5 « `None` explicite obligatoire ») : si
    aucune clé n'est présente, ou si la valeur est `None`, on lève `ResultsApiError`
    avec les clés réellement reçues. Un score par défaut serait exactement le bug
    d'origine du projet (0-0 gradé « push » au lieu d'être traité comme absent).

    ⚠️ N'est atteint que pour un match **déclaré terminé** (`get_games` filtre les
    autres en amont) : un score `null` ici n'est donc pas un match à venir, c'est
    une incohérence de la source — d'où la levée plutôt qu'un saut silencieux.
    """
    for key in keys:
        if key in game:
            value = game[key]
            if value is None:
                raise ResultsApiError(
                    f"Score '{key}' absent (null) sur un match déclaré terminé — "
                    f"donnée incohérente : status={game.get('status')!r}, "
                    f"date={game.get('date')!r}"
                )
            return int(value)
    raise ResultsApiError(
        f"Aucune clé de score parmi {list(keys)} dans la réponse balldontlie. "
        f"Clés reçues : {sorted(game.keys())}"
    )


def _parse_game(game: dict) -> GameResult:
    """Convertit un match brut balldontlie en `GameResult`.

    La date renvoyée par l'API est une chaîne ISO (parfois avec l'heure) : on ne
    conserve que la partie calendaire 'YYYY-MM-DD'.

    ⚠️ Les deux ligues n'exposent pas le même schéma (vérifié par appel réel le
    2026-08-07, cf. journal des décisions) :

    - NBA  : `home_team_score` / `visitor_team_score`, `date` = date calendaire US
      pure ('2026-01-16'), `status` = 'Final' ;
    - WNBA : `home_score` / `away_score`, `date` = date-**time** UTC
      ('2026-08-05T02:00:00.000Z'), `status` = 'post'.

    On lit donc les deux conventions. La troncature `[:10]` reste **exacte pour la
    NBA** (date déjà calendaire) mais **décale d'un jour les matchs WNBA en soirée
    US** (02:00 UTC = veille à New York) : réserve connue, traitée dans la session
    backfill, pas ici.
    """
    return GameResult(
        game_date=str(game["date"])[:10],
        status=str(game.get("status", "")),
        home_team=game["home_team"]["full_name"],
        away_team=game["visitor_team"]["full_name"],
        home_score=_score(game, "home_score", "home_team_score"),
        away_score=_score(game, "away_score", "visitor_team_score"),
    )
