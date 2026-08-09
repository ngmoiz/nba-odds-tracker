"""Tests du cache disque des réponses balldontlie (B5 lot 3).

Le transport interne est toujours un `httpx.MockTransport` qui **compte ses appels** :
c'est ce compteur, et lui seul, qui prouve qu'un cache sert bien à quelque chose. Un
test qui vérifierait seulement que la réponse est correcte passerait tout aussi bien
sans cache du tout.

Aucun accès réseau, aucune attente réelle (le client reçoit `sleep`/`monotonic`
instrumentés selon la convention du fichier `test_evaluator.py`).
"""
from __future__ import annotations

import json

import httpx
import pytest

from common.config import Settings, load_config
from common.http_cache import CacheMissOffline, CachingTransport, cache_key
from common.results_api_client import ResultsApiClient

PAYLOAD = {
    "data": [{
        "id": 18447401,
        "date": "2026-08-05T02:00:00.000Z",
        "status": "post",
        "season": 2026,
        "postseason": False,
        "home_team": {"full_name": "Golden State Valkyries"},
        "visitor_team": {"full_name": "Toronto Tempo"},
        "home_score": 92,
        "away_score": 81,
    }],
    "meta": {"next_cursor": None},
}


def counting_transport(payload=PAYLOAD, status=200):
    """Transport interne instrumenté : renvoie `payload` et compte ses appels."""
    calls: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handler), calls


def request(url="https://api.balldontlie.io/wnba/v1/games", **params) -> httpx.Request:
    return httpx.Request("GET", httpx.URL(url, params=params))


# ───────────────────────────── clé de cache ─────────────────────────────


def test_cache_key_ignores_parameter_order():
    """Une même requête produit la même clé quel que soit l'ordre d'émission."""
    first = request(start_date="2026-08-05", end_date="2026-08-05", per_page=100)
    second = request(per_page=100, end_date="2026-08-05", start_date="2026-08-05")
    assert cache_key(first) == cache_key(second)


def test_cache_key_separates_pagination_cursors():
    """Deux pages sont deux entrées : sans cela, la page 2 servirait la page 1."""
    page_one = request(start_date="2026-08-05", per_page=100)
    page_two = request(start_date="2026-08-05", per_page=100, cursor="90")
    assert cache_key(page_one) != cache_key(page_two)


def test_cache_key_never_carries_credentials(tmp_path):
    """Aucun secret ni en-tête dans la clé ni sur disque (règle 0.4.1).

    balldontlie authentifie par un en-tête `Authorization` : deux requêtes identiques
    portant des clés d'API différentes doivent viser la même entrée, et l'entrée écrite
    ne doit contenir aucune trace de la clé.
    """
    with_key = httpx.Request("GET", "https://api.balldontlie.io/wnba/v1/games",
                             headers={"Authorization": "secret-key"})
    without = httpx.Request("GET", "https://api.balldontlie.io/wnba/v1/games")
    assert cache_key(with_key) == cache_key(without)

    inner, _ = counting_transport()
    transport = CachingTransport(tmp_path, inner=inner)
    transport.handle_request(with_key).read()

    written = list(tmp_path.glob("*.json"))
    assert len(written) == 1
    assert "secret-key" not in written[0].read_text(encoding="utf-8")


# ───────────────────────────── hit / miss ─────────────────────────────


def test_second_identical_request_never_reaches_the_network(tmp_path):
    """Le seul contrôle qui prouve le cache : le transport interne n'est appelé qu'une fois."""
    inner, calls = counting_transport()
    transport = CachingTransport(tmp_path, inner=inner)

    first = transport.handle_request(request(start_date="2026-08-05"))
    first.read()
    second = transport.handle_request(request(start_date="2026-08-05"))
    second.read()

    assert len(calls) == 1
    assert transport.network_calls == 1
    assert transport.hits == 1
    assert json.loads(first.text) == json.loads(second.text) == PAYLOAD


def test_a_different_request_is_fetched_separately(tmp_path):
    inner, calls = counting_transport()
    transport = CachingTransport(tmp_path, inner=inner)
    transport.handle_request(request(start_date="2026-08-05")).read()
    transport.handle_request(request(start_date="2026-08-06")).read()
    assert len(calls) == 2
    assert transport.hits == 0


def test_cache_survives_a_new_transport_instance(tmp_path):
    """Le cache est sur disque, pas en mémoire : un nouveau processus en profite.

    C'est toute la raison d'être du lot — un backfill se met au point en plusieurs
    exécutions successives.
    """
    inner, calls = counting_transport()
    CachingTransport(tmp_path, inner=inner).handle_request(request(day="1")).read()

    inner_again, calls_again = counting_transport()
    reopened = CachingTransport(tmp_path, inner=inner_again)
    reopened.handle_request(request(day="1")).read()

    assert len(calls) == 1
    assert calls_again == []
    assert reopened.hits == 1


def test_entry_is_human_readable(tmp_path):
    """Une entrée doit pouvoir être ouverte et comprise : URL, statut, horodatage, corps."""
    inner, _ = counting_transport()
    transport = CachingTransport(tmp_path, inner=inner)
    transport.handle_request(request(start_date="2026-08-05")).read()

    entry = json.loads(list(tmp_path.glob("*.json"))[0].read_text(encoding="utf-8"))
    assert entry["status"] == 200
    assert entry["body"] == PAYLOAD
    assert "start_date=2026-08-05" in entry["url"]
    assert entry["fetched_at"].startswith("20")


# ─────────────────────── ce qui ne doit PAS être caché ───────────────────────


@pytest.mark.parametrize("status", [429, 500, 401])
def test_non_200_responses_are_never_cached(tmp_path, status):
    """Figer un 429 ou un 500 sur disque transformerait un incident passager en panne
    permanente. La réponse est rendue telle quelle, en-têtes compris (Retry-After)."""
    inner, calls = counting_transport(status=status)
    transport = CachingTransport(tmp_path, inner=inner)

    transport.handle_request(request(day="1")).read()
    transport.handle_request(request(day="1")).read()

    assert len(calls) == 2
    assert list(tmp_path.glob("*.json")) == []
    assert transport.stored == 0


def test_a_200_that_is_not_json_is_not_cached(tmp_path, caplog):
    """Un 200 illisible n'est pas figé : le mettre en cache rendrait l'anomalie définitive."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>maintenance</html>")

    transport = CachingTransport(tmp_path, inner=httpx.MockTransport(handler))
    with caplog.at_level("WARNING", logger="http_cache"):
        transport.handle_request(request(day="1")).read()

    assert list(tmp_path.glob("*.json")) == []
    assert "non mise en cache" in caplog.text


def test_a_corrupted_entry_is_reported_and_refetched(tmp_path, caplog):
    """Une entrée abîmée est traitée comme absente ET signalée — jamais avalée en silence.

    « Pas encore téléchargé » et « téléchargé puis corrompu » sont deux situations
    différentes : confondre les deux ferait disparaître un vrai problème de disque.
    """
    inner, calls = counting_transport()
    transport = CachingTransport(tmp_path, inner=inner)
    probe = request(day="1")
    transport.handle_request(probe).read()
    transport.entry_path(probe).write_text("{tronqué", encoding="utf-8")

    with caplog.at_level("WARNING", logger="http_cache"):
        transport.handle_request(probe).read()

    assert len(calls) == 2
    assert "illisible" in caplog.text


# ───────────────────────────── mode hors-ligne ─────────────────────────────


def test_offline_mode_refuses_to_reach_the_network(tmp_path):
    """Une exécution de contrôle doit être INCAPABLE de toucher le réseau.

    C'est la garde qui manquait le 2026-07-20, quand une commande de vérification a
    consommé 15 crédits parce que rien n'empêchait l'appel réel — « censé s'abstenir »
    n'est pas une garantie.
    """
    inner, calls = counting_transport()
    transport = CachingTransport(tmp_path, inner=inner, offline=True)

    with pytest.raises(CacheMissOffline, match="hors-ligne"):
        transport.handle_request(request(day="1"))

    assert calls == []
    assert transport.network_calls == 0


def test_offline_mode_serves_what_is_already_cached(tmp_path):
    inner, calls = counting_transport()
    CachingTransport(tmp_path, inner=inner).handle_request(request(day="1")).read()

    offline = CachingTransport(tmp_path, inner=inner, offline=True)
    response = offline.handle_request(request(day="1"))
    response.read()

    assert len(calls) == 1
    assert json.loads(response.text) == PAYLOAD


# ─────────────────── intégration avec le client de résultats ───────────────────


def _settings() -> Settings:
    from pathlib import Path
    return Settings(odds_api_key="", balldontlie_api_key="k", telegram_bot_token="",
                    telegram_chat_id="", database_path=Path("x.db"), log_level="INFO")


def test_from_config_passes_the_transport_through(tmp_path):
    """Le cache se glisse sous le client SANS reconstruire à la main le routage par ligue.

    Recopier la lecture de `games_paths`/`rate_limit` dans le script produirait une
    seconde implémentation qui dériverait de la production — exactement le défaut
    trouvé le 2026-08-07 (routage correct, parsing divergent). On vérifie donc que le
    passe-plat fonctionne ET que les réglages réels restent en place.
    """
    config = load_config()
    inner, calls = counting_transport()
    transport = CachingTransport(tmp_path, inner=inner)
    client = ResultsApiClient.from_config(_settings(), config, transport=transport)
    try:
        games = client.get_games("2026-08-05", "2026-08-05")
        assert len(games) == 1
        assert games[0].game_id == "18447401"
        # Le fuseau du calendrier est bien appliqué : 02:00 UTC = la veille à New York.
        assert games[0].game_date == "2026-08-04"
        assert calls[0].path == config["results"]["games_paths"][config["api"]["sport"]]
        assert client._min_interval == config["results"]["rate_limit"]["min_interval_seconds"]
    finally:
        client.close()


def test_from_config_without_transport_is_unchanged():
    """Le paramètre est inerte quand il est omis : aucun comportement existant modifié."""
    client = ResultsApiClient.from_config(_settings(), load_config())
    try:
        assert client._games_path.endswith("/games")
    finally:
        client.close()


def test_client_reuses_cached_pages_across_runs(tmp_path):
    """Deux `get_games` identiques, un seul appel réseau — le cas d'usage du backfill."""
    config = load_config()
    inner, calls = counting_transport()
    transport = CachingTransport(tmp_path, inner=inner)

    for _ in range(2):
        client = ResultsApiClient.from_config(_settings(), config, transport=transport)
        try:
            assert len(client.get_games("2026-08-05", "2026-08-05")) == 1
        finally:
            client.close()

    assert len(calls) == 1
    assert transport.hits == 1
