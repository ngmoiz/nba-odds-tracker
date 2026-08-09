"""Cache disque des réponses HTTP, sous forme de transport httpx (chantier B5, lot 3).

Raison d'être. Un backfill de saison se met au point en plusieurs passes : on relance
le rejeu, on corrige une règle de repos, on relance encore. Sans cache, chaque passe
re-télécharge la saison et paie l'étranglement du tier gratuit balldontlie (5 req/min).
Le réseau ne doit être payé qu'une fois.

Conception. Le cache s'interpose **sous** le client, via le point d'injection
`transport=` qui existe déjà (celui qu'utilisent les tests avec `MockTransport`) :
`ResultsApiClient` n'est pas modifié et ne sait pas qu'il est mis en cache.

    get_games() → _get() → httpx.Client → CachingTransport ─┬─ hit  : lit un .json
                                                            └─ miss : délègue, puis écrit

**Granularité page, pas appel.** La pagination par curseur de balldontlie fait plusieurs
requêtes par `get_games` : une interruption au milieu ne doit pas perdre les pages déjà
obtenues. Un cache posé au niveau de `get_games` n'aurait pas cette propriété.

**Seules les réponses 200 sont mises en cache.** Un 429 ou un 500 figé sur disque
transformerait un incident passager en panne permanente.

**Aucun secret sur disque** (règle 0.4.1). balldontlie authentifie par un en-tête
`Authorization` : les en-têtes de requête ne sont ni stockés, ni pris dans la clé de
cache — qui ne porte que la méthode, le chemin et la query. Rien de secret n'y transite.

**Mode hors-ligne.** `offline=True` fait lever tout défaut de cache au lieu d'appeler
le réseau. C'est la garde qui manquait le 2026-07-20, quand une commande de vérification
a consommé 15 crédits parce que rien n'empêchait l'appel réel : une exécution de
contrôle doit être *incapable* de toucher le réseau, pas seulement censée s'en abstenir.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from common.logging_config import get_logger

logger = get_logger("http_cache")

# Longueur du condensé retenue dans le nom de fichier : 16 caractères hexadécimaux
# (64 bits) rendent une collision inatteignable à l'échelle de quelques centaines
# d'entrées, tout en gardant un nom lisible.
_DIGEST_LENGTH = 16

_SLUG_RE = re.compile(r"[^a-z0-9]+")


class CacheMissOffline(Exception):
    """Défaut de cache alors que le mode hors-ligne interdit tout accès réseau.

    Volontairement **pas** une `httpx.RequestError` : le client de résultats retente
    les erreurs réseau, et retenter un défaut de cache n'aurait aucun sens. L'exception
    doit traverser la boucle de tentatives et faire échouer l'exécution.
    """


def unthrottled(config: dict) -> dict:
    """Copie de la configuration sans étranglement, pour une exécution hors-ligne.

    L'étranglement du lot 1a protège le tier gratuit (5 req/min) : il doit rester
    intact dans tous les modes qui peuvent toucher le réseau. En mode hors-ligne
    aucune requête ne *peut* partir — `CachingTransport` lève sur défaut de cache —
    donc attendre 13 s entre deux lectures de disque ne protège rien et ferait payer
    une demi-minute à chaque ré-essai, c'est-à-dire l'inverse de ce que le cache
    apporte.

    Ne mute jamais la configuration reçue : renvoie des dictionnaires neufs, sur le
    modèle de `scripts/verify_game_date_timezone.py`.
    """
    results = config.get("results") or {}
    rate_limit = dict(results.get("rate_limit") or {}, min_interval_seconds=0)
    return dict(config, results=dict(results, rate_limit=rate_limit))


def _slug(path: str) -> str:
    """Fragment lisible tiré du chemin ('/wnba/v1/games' → 'wnba-v1-games')."""
    return _SLUG_RE.sub("-", path.lower()).strip("-") or "root"


def cache_key(request: httpx.Request) -> str:
    """Clé stable d'une requête : méthode, chemin et query **triée**.

    La query est triée pour qu'un même appel produise la même clé quel que soit
    l'ordre d'émission des paramètres. Le curseur de pagination en fait partie :
    deux pages successives sont donc deux entrées distinctes.

    Les en-têtes sont exclus — c'est ce qui garantit qu'aucune clé d'API ne peut
    se retrouver sur disque ni dans un nom de fichier.
    """
    query = "&".join(
        f"{key}={value}" for key, value in sorted(request.url.params.multi_items())
    )
    material = f"{request.method} {request.url.path}?{query}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:_DIGEST_LENGTH]
    return f"{_slug(request.url.path)}-{digest}"


class CachingTransport(httpx.BaseTransport):
    """Transport httpx qui sert depuis un cache disque et n'appelle le réseau qu'au besoin.

    Compteurs exposés pour le rapport d'exécution : un backfill doit pouvoir dire
    combien de requêtes il a réellement émises, et non combien il en a demandé.
    """

    def __init__(
        self,
        cache_dir: Path,
        inner: httpx.BaseTransport | None = None,
        *,
        offline: bool = False,
    ) -> None:
        self._cache_dir = Path(cache_dir)
        self._inner = inner if inner is not None else httpx.HTTPTransport()
        self._offline = offline
        self.hits = 0
        self.misses = 0
        self.network_calls = 0
        self.stored = 0
        # Instant de la dernière requête réellement partie sur le réseau. Sert au
        # rapport ; l'étranglement reste géré par le client, jamais ici.
        self.last_network_at: float | None = None

    @property
    def offline(self) -> bool:
        return self._offline

    def entry_path(self, request: httpx.Request) -> Path:
        return self._cache_dir / f"{cache_key(request)}.json"

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        path = self.entry_path(request)
        cached = self._load(path)
        if cached is not None:
            self.hits += 1
            logger.debug("Cache HIT %s → %s", request.url, path.name)
            return self._response(cached["status"], cached["body"], request)

        self.misses += 1
        if self._offline:
            raise CacheMissOffline(
                f"Mode hors-ligne : {request.url} n'est pas en cache "
                f"(entrée attendue : {path}). Relancer sans --offline pour la "
                f"télécharger, ou vérifier que le cache n'a pas été vidé."
            )

        logger.debug("Cache MISS %s → réseau", request.url)
        response = self._inner.handle_request(request)
        self.network_calls += 1
        self.last_network_at = time.monotonic()

        if response.status_code != 200:
            # Ni mise en cache, ni reconstruction : on rend la réponse telle quelle
            # pour que le client voie le vrai statut, ses en-têtes (dont Retry-After)
            # et puisse appliquer sa politique de retry.
            return response

        response.read()
        try:
            body = json.loads(response.text)
        except json.JSONDecodeError:
            # Un 200 illisible n'est pas mis en cache : le figer sur disque
            # rendrait l'anomalie permanente. On laisse le client échouer dessus.
            logger.warning("Réponse 200 non-JSON sur %s : non mise en cache.", request.url)
            return response

        self._store(path, request, response.status_code, body)
        return self._response(response.status_code, body, request)

    def close(self) -> None:
        self._inner.close()

    # ───────────────────────────── disque ─────────────────────────────

    def _load(self, path: Path) -> dict | None:
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as handle:
                entry = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            # Entrée corrompue : on la traite comme absente et on le dit. Ne jamais
            # avaler en silence — c'est la différence entre « pas encore téléchargé »
            # et « téléchargé puis abîmé ».
            logger.warning("Entrée de cache illisible (%s) : %s — ignorée.", path.name, exc)
            return None
        if "body" not in entry or "status" not in entry:
            logger.warning("Entrée de cache incomplète (%s) : ignorée.", path.name)
            return None
        return entry

    def _store(self, path: Path, request: httpx.Request, status: int, body: object) -> None:
        entry = {
            "url": str(request.url),
            "method": request.method,
            "status": status,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "body": body,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        # Écriture atomique : un fichier temporaire renommé. Une interruption pendant
        # l'écriture laisserait sinon une entrée tronquée, qui serait relue comme
        # valide au run suivant.
        temporary = path.with_suffix(".json.tmp")
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(entry, handle, ensure_ascii=False, indent=2, sort_keys=True)
        temporary.replace(path)
        self.stored += 1

    @staticmethod
    def _response(status: int, body: object, request: httpx.Request) -> httpx.Response:
        """Reconstruit une réponse à partir du corps stocké.

        Les en-têtes d'origine ne sont pas rejoués : `Content-Encoding` ou
        `Content-Length` décrivaient les octets reçus, pas ceux qu'on ré-émet ici.
        Seul le type de contenu est réaffirmé.
        """
        return httpx.Response(
            status,
            content=json.dumps(body).encode("utf-8"),
            headers={"content-type": "application/json"},
            request=request,
        )
