"""Client pour The Odds API v4 (https://the-odds-api.com/liveapi/guides/v4/).

Responsabilités :
- encapsuler les appels HTTP (cotes, scores, liste des sports) ;
- logger le quota restant après chaque requête (règle 4.3) ;
- convertir le JSON brut en objets typés, pour découpler le reste du code du
  format exact de l'API.

Les timestamps renvoyés par l'API sont déjà en UTC ISO 8601 : on les conserve
tels quels (chaînes) pour les stocker directement en base sans reconversion.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from common.logging_config import get_logger

logger = get_logger("odds_api")

BASE_URL = "https://api.the-odds-api.com"
DEFAULT_TIMEOUT = 20.0  # secondes


# ─────────────────────────── Exceptions ───────────────────────────

class OddsApiError(Exception):
    """Erreur générique du client The Odds API."""


class RateLimitError(OddsApiError):
    """Levée sur un code HTTP 429 (limite de débit atteinte)."""


# ─────────────────────── Objets du domaine ───────────────────────

@dataclass(frozen=True)
class Outcome:
    """Une issue d'un marché : une équipe, ou 'Over'/'Under' (totals)."""

    name: str            # nom de l'équipe, ou 'Over'/'Under'
    price: float         # cote décimale
    point: float | None  # ligne (spreads/totals) ; None pour h2h


@dataclass(frozen=True)
class Market:
    key: str             # h2h / spreads / totals
    outcomes: list[Outcome]


@dataclass(frozen=True)
class Bookmaker:
    key: str
    title: str
    last_update: str     # ISO 8601 UTC
    markets: list[Market]


@dataclass(frozen=True)
class OddsEvent:
    """Un match avec les cotes de plusieurs bookmakers."""

    id: str
    sport_key: str
    commence_time: str   # ISO 8601 UTC (= tip-off)
    home_team: str
    away_team: str
    bookmakers: list[Bookmaker]


@dataclass(frozen=True)
class ScoreEvent:
    """Un match et son score (endpoint scores, utilisé par l'évaluateur)."""

    id: str
    commence_time: str
    completed: bool
    home_team: str
    away_team: str
    scores: dict[str, int] | None  # nom d'équipe -> score, ou None si non commencé


# ─────────────────────────── Le client ───────────────────────────

class OddsApiClient:
    """Client synchrone pour The Odds API.

    Les paramètres métier (sport, région, marchés, format) sont injectés depuis
    la configuration (voir `from_config`), jamais codés en dur.
    """

    def __init__(
        self,
        api_key: str,
        sport: str,
        region: str,
        markets: list[str],
        odds_format: str = "decimal",
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._sport = sport
        self._region = region
        self._markets = markets
        self._odds_format = odds_format
        # Dernier quota connu (en-tête x-requests-remaining du dernier appel).
        self.credits_remaining: str | None = None
        # Cout de la derniere requete (en-tete x-requests-last).
        self.last_request_cost: str | None = None
        # `transport` permet d'injecter un faux transport dans les tests (sans réseau).
        self._client = httpx.Client(base_url=BASE_URL, timeout=timeout, transport=transport)

    @classmethod
    def from_config(cls, settings, config: dict) -> OddsApiClient:
        """Construit le client à partir des objets de configuration du projet."""
        api = config["api"]
        return cls(
            api_key=settings.odds_api_key,
            sport=api["sport"],
            region=api["region"],
            markets=api["markets"],
            odds_format=api["odds_format"],
        )

    # --- Gestion de la connexion (context manager) ---

    def __enter__(self) -> OddsApiClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # --- Requête générique ---

    def _log_quota(self, response: httpx.Response) -> None:
        """Mémorise et logge le quota d'après les en-têtes renvoyés par l'API."""
        self.credits_remaining = response.headers.get("x-requests-remaining")
        self.last_request_cost = response.headers.get("x-requests-last")
        logger.info(
            "Quota API — crédits restants: %s, consommés: %s, coût de la requête: %s",
            self.credits_remaining or "?",
            response.headers.get("x-requests-used", "?"),
            response.headers.get("x-requests-last", "?"),
        )

    def _get(self, path: str, params: dict) -> list:
        """Exécute un GET, gère les erreurs, logge le quota, renvoie le JSON."""
        params = {"apiKey": self._api_key, **params}
        try:
            response = self._client.get(path, params=params)
        except httpx.RequestError as exc:
            raise OddsApiError(f"Erreur réseau vers {path} : {exc}") from exc

        # Les en-têtes de quota sont présents même sur une réponse d'erreur.
        self._log_quota(response)

        if response.status_code == 429:
            raise RateLimitError("Limite de débit atteinte (HTTP 429).")
        if response.status_code != 200:
            raise OddsApiError(f"HTTP {response.status_code} sur {path} : {response.text[:200]}")

        return response.json()

    # --- Endpoints ---

    def get_sports(self, include_out_of_season: bool = False) -> list[dict]:
        """Liste les sports disponibles (endpoint gratuit, 0 crédit)."""
        params = {"all": "true"} if include_out_of_season else {}
        return self._get("/v4/sports/", params)

    def get_odds(self, markets: list[str] | None = None) -> list[OddsEvent]:
        """Récupère les cotes de tous les matchs à venir du sport configuré.

        Coût : nb de marchés × nb de régions (ici 3 × 1 = 3 crédits).
        Une réponse vide (hors saison) ne coûte aucun crédit.
        
        Paramètres :
            markets : liste des marchés à collecter. Si None, utilise self._markets.
        """
        markets_to_use = markets if markets is not None else self._markets
        params = {
            "regions": self._region,
            "markets": ",".join(markets_to_use),
            "oddsFormat": self._odds_format,
            "dateFormat": "iso",
        }
        raw = self._get(f"/v4/sports/{self._sport}/odds/", params)
        return [_parse_odds_event(event) for event in raw]

    def get_scores(self, days_from: int = 1) -> list[ScoreEvent]:
        """Récupère les scores (matchs terminés des `days_from` derniers jours).

        Coût : 2 crédits avec `daysFrom`. Utilisé par l'évaluateur.
        """
        params = {"daysFrom": days_from, "dateFormat": "iso"}
        raw = self._get(f"/v4/sports/{self._sport}/scores/", params)
        return [_parse_score_event(event) for event in raw]


# ─────────────────────── Parsing (JSON -> objets) ───────────────────────

def _parse_odds_event(event: dict) -> OddsEvent:
    bookmakers = [
        Bookmaker(
            key=bk["key"],
            title=bk["title"],
            last_update=bk["last_update"],
            markets=[
                Market(
                    key=mk["key"],
                    outcomes=[
                        Outcome(
                            name=oc["name"],
                            price=oc["price"],
                            point=oc.get("point"),  # absent pour h2h
                        )
                        for oc in mk["outcomes"]
                    ],
                )
                for mk in bk["markets"]
            ],
        )
        for bk in event.get("bookmakers", [])
    ]
    return OddsEvent(
        id=event["id"],
        sport_key=event["sport_key"],
        commence_time=event["commence_time"],
        home_team=event["home_team"],
        away_team=event["away_team"],
        bookmakers=bookmakers,
    )


def _parse_score_event(event: dict) -> ScoreEvent:
    raw_scores = event.get("scores")
    scores = {s["name"]: int(s["score"]) for s in raw_scores} if raw_scores else None
    return ScoreEvent(
        id=event["id"],
        commence_time=event["commence_time"],
        completed=event["completed"],
        home_team=event["home_team"],
        away_team=event["away_team"],
        scores=scores,
    )
