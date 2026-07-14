"""Tests du client The Odds API : parsing des réponses et gestion des erreurs.

On simule l'API avec `httpx.MockTransport` : aucun appel réseau, aucun crédit
consommé, résultats reproductibles. Le format des réponses reproduit celui de la
documentation v4.
"""
from __future__ import annotations

import httpx
import pytest

from common.odds_api_client import OddsApiClient, OddsApiError, RateLimitError

# Réponse cotes au format documenté (h2h + spreads + totals), oddsFormat=decimal.
ODDS_RESPONSE = [
    {
        "id": "match1",
        "sport_key": "basketball_nba",
        "sport_title": "NBA",
        "commence_time": "2026-01-10T00:20:00Z",
        "home_team": "Boston Celtics",
        "away_team": "Miami Heat",
        "bookmakers": [
            {
                "key": "pinnacle",
                "title": "Pinnacle",
                "last_update": "2026-01-09T13:33:18Z",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Boston Celtics", "price": 1.5},
                            {"name": "Miami Heat", "price": 2.6},
                        ],
                    },
                    {
                        "key": "spreads",
                        "outcomes": [
                            {"name": "Boston Celtics", "price": 1.91, "point": -7.5},
                            {"name": "Miami Heat", "price": 1.91, "point": 7.5},
                        ],
                    },
                    {
                        "key": "totals",
                        "outcomes": [
                            {"name": "Over", "price": 1.9, "point": 224.5},
                            {"name": "Under", "price": 1.9, "point": 224.5},
                        ],
                    },
                ],
            }
        ],
    }
]

SCORES_RESPONSE = [
    {
        "id": "match1",
        "sport_key": "basketball_nba",
        "commence_time": "2026-01-10T00:20:00Z",
        "completed": True,
        "home_team": "Boston Celtics",
        "away_team": "Miami Heat",
        "scores": [
            {"name": "Boston Celtics", "score": "113"},
            {"name": "Miami Heat", "score": "103"},
        ],
        "last_update": "2026-01-10T02:18:19Z",
    }
]


def make_client(handler) -> OddsApiClient:
    """Client branché sur un faux transport HTTP (le handler produit la réponse)."""
    transport = httpx.MockTransport(handler)
    return OddsApiClient(
        api_key="fake",
        sport="basketball_nba",
        region="us",
        markets=["h2h", "spreads", "totals"],
        transport=transport,
    )


def test_get_odds_parses_events():
    """Le parsing produit bien les objets typés, avec point=None pour h2h."""
    client = make_client(lambda req: httpx.Response(200, json=ODDS_RESPONSE))
    events = client.get_odds()

    assert len(events) == 1
    ev = events[0]
    assert ev.id == "match1"
    assert ev.home_team == "Boston Celtics"
    assert len(ev.bookmakers) == 1

    markets = {m.key: m for m in ev.bookmakers[0].markets}
    # h2h : aucune ligne
    assert markets["h2h"].outcomes[0].point is None
    # spreads : la ligne est dans `point`
    spread_home = markets["spreads"].outcomes[0]
    assert spread_home.name == "Boston Celtics"
    assert spread_home.point == -7.5
    assert spread_home.price == 1.91
    # totals : Over/Under + ligne
    totals = markets["totals"].outcomes
    assert {o.name for o in totals} == {"Over", "Under"}
    assert totals[0].point == 224.5


def test_get_odds_request_uses_configured_params():
    """L'URL construite reflète bien la config (sport, région, marchés, format)."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=[])

    make_client(handler).get_odds()
    url = captured["url"]
    assert "/v4/sports/basketball_nba/odds/" in url
    assert "regions=us" in url
    assert "markets=h2h%2Cspreads%2Ctotals" in url
    assert "oddsFormat=decimal" in url
    assert "apiKey=fake" in url


def test_get_scores_parses_scores():
    """Les scores (chaînes dans l'API) sont convertis en entiers."""
    client = make_client(lambda req: httpx.Response(200, json=SCORES_RESPONSE))
    scores = client.get_scores(days_from=1)

    assert len(scores) == 1
    sc = scores[0]
    assert sc.completed is True
    assert sc.scores == {"Boston Celtics": 113, "Miami Heat": 103}


def test_get_scores_handles_null_scores():
    """Un match non commencé a `scores` à None."""
    resp = [dict(SCORES_RESPONSE[0], completed=False, scores=None)]
    client = make_client(lambda req: httpx.Response(200, json=resp))
    assert client.get_scores()[0].scores is None


def test_rate_limit_raises_dedicated_error():
    """Un code 429 lève RateLimitError."""
    client = make_client(lambda req: httpx.Response(429, text="Too Many Requests"))
    with pytest.raises(RateLimitError):
        client.get_odds()


def test_http_error_raises():
    """Un code d'erreur (ex. 401) lève OddsApiError."""
    client = make_client(lambda req: httpx.Response(401, text="Unauthorized"))
    with pytest.raises(OddsApiError):
        client.get_odds()
