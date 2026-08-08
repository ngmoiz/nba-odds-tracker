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

import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from common.logging_config import get_logger

logger = get_logger("results_api")

DEFAULT_TIMEOUT = 20.0
# balldontlie plafonne per_page à 100 ; une journée NBA compte ~15 matchs.
_PER_PAGE = 100
# Borne dure du nombre de pages suivies (garde anti-boucle, cf. `get_games`).
_DEFAULT_MAX_PAGES = 50

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
    # Champs exposés par les deux schémas de ligue, requis par le backfill Elo (B5).
    # Défaut `None` : une clé absente reste absente (invariant 5). En particulier
    # `postseason=None` signifie « inconnu », jamais « saison régulière » — affirmer
    # False sans l'avoir lu serait exactement le défaut par défaut qu'on s'interdit.
    game_id: str | None = None      # id balldontlie ; clé d'idempotence du rejeu Elo
    season: int | None = None
    postseason: bool | None = None

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
        *,
        min_interval_seconds: float = 0.0,
        max_retries: int = 0,
        backoff_base_seconds: float = 2.0,
        max_pages: int = _DEFAULT_MAX_PAGES,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Construit le client.

        **Les défauts d'étranglement et de retry sont neutres** : un client construit
        directement se comporte exactement comme avant l'introduction de ces options
        (`min_interval_seconds=0` → aucune attente, `max_retries=0` → un 429 lève du
        premier coup). Seul `from_config` injecte les valeurs réelles, et il ne
        concerne que l'évaluateur — qui ne tire qu'une page, donc n'attend jamais
        (la première requête n'est pas retardée).

        `max_pages` est la seule valeur **non neutre**, délibérément : la neutraliser
        (borne absente) préserverait la boucle non bornée qu'elle corrige. Elle reste
        inatteignable en pratique (une page pour l'évaluateur, 3 à 5 pour le backfill).

        `sleep` et `monotonic` sont injectables, sur le modèle du `transport` httpx :
        aucun test ne dort réellement.
        """
        self._api_key = api_key
        self._games_path = games_path
        self._min_interval = min_interval_seconds
        self._max_retries = max_retries
        self._backoff_base = backoff_base_seconds
        self._max_pages = max_pages
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_request_at: float | None = None
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

        # Étranglement et pagination : seuls réglages qui activent le comportement
        # non neutre. Blocs optionnels — une config antérieure garde les défauts.
        rate_limit = results.get("rate_limit") or {}
        pagination = results.get("pagination") or {}

        return cls(
            api_key=settings.balldontlie_api_key,
            base_url=results["base_url"],
            games_path=games_path,
            min_interval_seconds=float(rate_limit.get("min_interval_seconds", 0.0)),
            max_retries=int(rate_limit.get("max_retries", 0)),
            backoff_base_seconds=float(rate_limit.get("backoff_base_seconds", 2.0)),
            max_pages=int(pagination.get("max_pages", _DEFAULT_MAX_PAGES)),
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

        **Bornes de la boucle.** `next_cursor` ne suffit pas à décider de la fin :
        la session 2 a observé un curseur **non nul sur une page pourtant terminale**.
        Trois gardes, dont deux ne changent que des cas qui bouclaient :

        1. page sans donnée → terminale, quoi que dise le curseur ;
        2. curseur déjà vu (curseur figé) → sortie avec warning ;
        3. dépassement de `max_pages` → `ResultsApiError`. **Jamais de troncature
           silencieuse** : un backfill partiel d'apparence normale produirait des
           notes Elo fausses sans qu'aucun signal ne le révèle.
        """
        games: list[GameResult] = []
        skipped: Counter[str] = Counter()
        cursor: str | None = None
        seen_cursors: set[str] = set()
        pages = 0
        while True:
            params: dict[str, object] = {
                "start_date": start_date,
                "end_date": end_date,
                "per_page": _PER_PAGE,
            }
            if cursor is not None:
                params["cursor"] = cursor
            payload = self._get(params)
            pages += 1

            data = payload.get("data") or []
            for raw in data:
                status = str(raw.get("status", ""))
                if not _status_is_final(status):
                    skipped[status or "(statut absent)"] += 1
                    continue
                games.append(_parse_game(raw))

            if not data:
                # Page vide : fin réelle, même si l'API annonce encore un curseur.
                break
            cursor = (payload.get("meta") or {}).get("next_cursor")
            if not cursor:
                break
            if str(cursor) in seen_cursors:
                logger.warning(
                    "Pagination balldontlie interrompue : curseur %r déjà vu "
                    "(curseur figé après %d page(s)).", cursor, pages,
                )
                break
            seen_cursors.add(str(cursor))
            if pages >= self._max_pages:
                raise ResultsApiError(
                    f"Pagination balldontlie : {pages} pages atteintes (max_pages="
                    f"{self._max_pages}) sans fin de curseur, entre {start_date} et "
                    f"{end_date}. Arrêt plutôt que résultat tronqué silencieusement."
                )
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

    def _wait_for_slot(self) -> None:
        """Complète l'intervalle minimal depuis la requête précédente.

        La **première** requête n'attend jamais (aucune précédente) : sur un appel
        d'une seule page — le cas de l'évaluateur — l'étranglement ne coûte rien.
        Inerte tant que `min_interval_seconds` vaut 0 (défaut).
        """
        if self._min_interval <= 0:
            return
        if self._last_request_at is not None:
            elapsed = self._monotonic() - self._last_request_at
            remaining = self._min_interval - elapsed
            if remaining > 0:
                logger.debug("Étranglement balldontlie : attente de %.1f s.", remaining)
                self._sleep(remaining)
        self._last_request_at = self._monotonic()

    def _get(self, params: dict) -> dict:
        """Exécute une requête, étranglée et retentée sur 429 / coupure réseau.

        Le tier gratuit balldontlie plafonne à 5 requêtes/minute. Deux protections
        distinctes, aux portées volontairement étroites :

        - **429** et erreurs réseau → nouvelle tentative, en honorant `Retry-After`
          s'il est fourni, sinon backoff exponentiel. Chaque tentative est loggée
          (invariant 6) : un ralentissement ne doit jamais être invisible.
        - **tout autre statut ≠ 200** → levée immédiate, sans retry. Un 401 (clé
          invalide) doit échouer du premier coup, pas au bout de quatre.

        Inerte tant que `max_retries` vaut 0 (défaut) : le comportement est alors
        exactement celui d'avant l'introduction du retry.
        """
        attempts = self._max_retries + 1
        for attempt in range(attempts):
            self._wait_for_slot()
            try:
                response = self._client.get(self._games_path, params=params)
            except httpx.RequestError as exc:
                if attempt + 1 >= attempts:
                    raise ResultsApiError(
                        f"Erreur réseau vers balldontlie après {attempts} tentative(s) : {exc}"
                    ) from exc
                self._backoff(attempt, reason=f"erreur réseau ({exc})", retry_after=None)
                continue

            if response.status_code == 429:
                if attempt + 1 >= attempts:
                    raise ResultsApiError(
                        f"HTTP 429 (quota balldontlie) après {attempts} tentative(s) : "
                        f"{response.text[:200]}"
                    )
                self._backoff(
                    attempt,
                    reason="HTTP 429 (quota balldontlie)",
                    retry_after=response.headers.get("Retry-After"),
                )
                continue

            if response.status_code != 200:
                # Pas de retry : une erreur applicative ne se résout pas en attendant.
                raise ResultsApiError(f"HTTP {response.status_code} : {response.text[:200]}")
            return response.json()

        # Inatteignable : chaque branche ci-dessus sort ou lève.
        raise ResultsApiError("Échec inattendu de la boucle de tentatives balldontlie.")

    def _backoff(self, attempt: int, *, reason: str, retry_after: str | None) -> None:
        """Attend avant une nouvelle tentative, en loggant toujours pourquoi.

        `Retry-After` (en secondes) prime sur le backoff calculé : le serveur sait
        mieux que nous quand il acceptera de nouveau. Une valeur illisible est
        ignorée au profit du backoff, avec une trace.
        """
        delay = self._backoff_base * (2**attempt)
        if retry_after is not None:
            try:
                delay = float(retry_after)
            except ValueError:
                logger.warning("En-tête Retry-After illisible (%r) : backoff calculé.", retry_after)
        logger.warning(
            "balldontlie : %s — nouvelle tentative dans %.1f s (tentative %d).",
            reason, delay, attempt + 1,
        )
        self._sleep(delay)


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
        game_id=_optional(game, "id", str),
        season=_optional(game, "season", int),
        postseason=_optional(game, "postseason", bool),
    )


def _optional(game: dict, key: str, cast):
    """Lit une clé facultative, ou `None` si absente/nulle (invariant 5).

    Aucune valeur inventée : `postseason` absent vaut `None` (« inconnu »), jamais
    `False` (« saison régulière »), qui serait une affirmation non vérifiée.
    """
    value = game.get(key)
    return None if value is None else cast(value)
