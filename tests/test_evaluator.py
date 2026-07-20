"""Tests de l'évaluateur (étape 1.6) — composant critique, priorité au grading.

Cas limites exigés par la roadmap : ligne qui traverse zéro, push (ligne entière),
match reporté (pas de résultat), bookmaker manquant au dernier relevé, h2h/spreads/
totals, et NO_BET (faux négatif). Aucun appel réseau (client balldontlie mocké).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from common import db
from common.config import Settings
from common.db import get_connection, init_db
from common.results_api_client import GameResult, ResultsApiClient
from evaluator.clv import compute_clv
from evaluator.evaluator import evaluate_pending
from evaluator.grading import grade_verdict
from evaluator.reconcile import find_result, normalize_team, tipoff_calendar_date
from evaluator.reporting import EvalLine, format_daily_report, success_rate

BOS, MIA = "Boston Celtics", "Miami Heat"


# ─────────────────────────── grading (cœur critique) ───────────────────────────

def test_grade_h2h_win_and_loss():
    assert grade_verdict(market="h2h", selection=BOS, line=None, home_team=BOS,
                         away_team=MIA, home_score=110, away_score=100) == "won"
    assert grade_verdict(market="h2h", selection=MIA, line=None, home_team=BOS,
                         away_team=MIA, home_score=110, away_score=100) == "lost"


def test_grade_spread_favorite_covers_and_fails():
    # Boston -5 : gagne de 10 → couvre (10 - 5 = 5 > 0).
    assert grade_verdict(market="spreads", selection=BOS, line=-5.0, home_team=BOS,
                         away_team=MIA, home_score=110, away_score=100) == "won"
    # Boston -5 : gagne de 3 → ne couvre pas (3 - 5 = -2 < 0).
    assert grade_verdict(market="spreads", selection=BOS, line=-5.0, home_team=BOS,
                         away_team=MIA, home_score=103, away_score=100) == "lost"


def test_grade_spread_crossing_zero_underdog():
    # Miami +6 perd de 4 → couvre (-4 + 6 = 2 > 0) : la ligne « traverse zéro ».
    assert grade_verdict(market="spreads", selection=MIA, line=6.0, home_team=BOS,
                         away_team=MIA, home_score=104, away_score=100) == "won"


def test_grade_spread_push_is_explicit():
    # Boston -5 gagne exactement de 5 → push (remboursé), état EXPLICITE (pas None).
    assert grade_verdict(market="spreads", selection=BOS, line=-5.0, home_team=BOS,
                         away_team=MIA, home_score=105, away_score=100) == "push"


def test_grade_totals_over_under_and_push():
    assert grade_verdict(market="totals", selection="Over", line=210.5, home_team=BOS,
                         away_team=MIA, home_score=110, away_score=105) == "won"   # 215 > 210,5
    assert grade_verdict(market="totals", selection="Under", line=210.5, home_team=BOS,
                         away_team=MIA, home_score=100, away_score=105) == "won"   # 205 < 210,5
    assert grade_verdict(market="totals", selection="Over", line=210.0, home_team=BOS,
                         away_team=MIA, home_score=105, away_score=105) == "push"  # 210 = 210 push


def test_grade_unknown_selection_is_none_not_push():
    # Sélection hors du match → non notable (None), à NE PAS confondre avec un push.
    assert grade_verdict(market="h2h", selection="Lakers", line=None, home_team=BOS,
                         away_team=MIA, home_score=110, away_score=100) is None


# ─────────────────────────── réconciliation ───────────────────────────

def _game(date_, home, away, hs, as_, status="Final"):
    return GameResult(game_date=date_, status=status, home_team=home,
                      away_team=away, home_score=hs, away_score=as_)


def test_normalize_team_is_case_and_space_insensitive():
    assert normalize_team("  Boston   Celtics ") == normalize_team("boston celtics")


def test_tipoff_calendar_date_uses_us_timezone():
    # 00:20 UTC le 17 = encore le 16 au soir à New York.
    assert tipoff_calendar_date("2026-01-17T00:20:00Z", "America/New_York").isoformat() == "2026-01-16"


def test_find_result_matches_names_and_nearby_date():
    games = [_game("2026-01-16", BOS, MIA, 110, 100)]
    r = find_result(games, home_team=BOS, away_team=MIA,
                    tipoff_utc="2026-01-17T00:20:00Z", calendar_tz="America/New_York")
    assert r is not None and r.home_score == 110


def test_find_result_returns_none_when_absent():
    games = [_game("2026-01-16", "Lakers", "Suns", 100, 99)]
    assert find_result(games, home_team=BOS, away_team=MIA,
                       tipoff_utc="2026-01-17T00:20:00Z", calendar_tz="America/New_York") is None


def test_find_result_flexible_matching_partial_names():
    """Matching flexible : accepte les noms partiels (ex: "Tempo" match "Toronto Tempo")."""
    # balldontlie renvoie "Tempo" au lieu de "Toronto Tempo"
    games = [_game("2026-07-17", "Atlanta Dream", "Tempo", 95, 90)]
    r = find_result(games, home_team="Atlanta Dream", away_team="Toronto Tempo",
                    tipoff_utc="2026-07-17T23:30:00Z", calendar_tz="America/New_York")
    assert r is not None and r.home_score == 95


def test_find_result_flexible_matching_reversed_partial():
    """Matching flexible : fonctionne dans les deux sens (nom court dans nom long)."""
    # balldontlie renvoie "Fire" au lieu de "Portland Fire"
    games = [_game("2026-07-16", "Washington Mystics", "Fire", 88, 82)]
    r = find_result(games, home_team="Washington Mystics", away_team="Portland Fire",
                    tipoff_utc="2026-07-16T23:00:00Z", calendar_tz="America/New_York")
    assert r is not None and r.away_score == 82


# ─────────────────────────── client balldontlie ───────────────────────────

def test_results_client_parses_and_paginates():
    pages = [
        {"data": [{"date": "2026-01-16", "status": "Final",
                   "home_team": {"full_name": BOS}, "visitor_team": {"full_name": MIA},
                   "home_score": 110, "away_score": 100}],
         "meta": {"next_cursor": 90}},
        {"data": [{"date": "2026-01-16T00:00:00.000Z", "status": "Final",
                   "home_team": {"full_name": "Lakers"}, "visitor_team": {"full_name": "Suns"},
                   "home_score": 99, "away_score": 98}],
         "meta": {"next_cursor": None}},
    ]
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        resp = pages[calls["n"]]
        calls["n"] += 1
        return httpx.Response(200, json=resp)

    client = ResultsApiClient("key", "https://api.balldontlie.io", "/v1/games",
                              transport=httpx.MockTransport(handler))
    games = client.get_games("2026-01-16", "2026-01-16")
    assert len(games) == 2                       # deux pages agrégées
    assert games[0].is_final and games[0].home_score == 110
    assert games[1].game_date == "2026-01-16"    # date tronquée à YYYY-MM-DD


# ─────────────────────── CLV (proba dé-marginée) ───────────────────────

@pytest.fixture
def conn(tmp_path: Path):
    db_path = tmp_path / "test.db"
    init_db(db_path)
    connection = get_connection(db_path)
    connection.execute(
        "INSERT INTO matches VALUES "
        "('m1','basketball_nba','Boston Celtics','Miami Heat','2026-01-17T00:20:00Z',"
        "'CLOS','2026-01-16T09:00:00Z')"
    )
    connection.commit()
    yield connection
    connection.close()


def _snap(conn, *, book, sel, line, odds, at, market="spreads"):
    db.insert_snapshot(conn, match_id="m1", bookmaker=book, market=market,
                       selection=sel, line=line, odds=odds, snapshot_at=at)


def test_compute_clv_uses_demargined_consensus(conn):
    """CLV = proba dé-marginée clôture − proba dé-marginée verdict (deux books)."""
    from analyzer.preprocessing import preprocess
    # Verdict (t0) : Boston/Miami cotés 1.91/1.91 → proba dé-marginée 0.5.
    for sel, ln, od in [("Boston Celtics", -5.0, 1.91), ("Miami Heat", 5.0, 1.91)]:
        _snap(conn, book="pinnacle", sel=sel, line=ln, odds=od, at="2026-01-16T22:00:00Z")
    # Clôture (t1) : Boston se raccourcit (1.60), Miami s'allonge (2.40) → proba Boston monte.
    for sel, ln, od in [("Boston Celtics", -5.0, 1.60), ("Miami Heat", 5.0, 2.40)]:
        _snap(conn, book="pinnacle", sel=sel, line=ln, odds=od, at="2026-01-17T00:00:00Z")
    conn.commit()

    data = preprocess(conn, "m1")
    closing_odds, clv = compute_clv(data, market="spreads", selection="Boston Celtics",
                                    decided_at="2026-01-16T22:05:00Z", tipoff_utc="2026-01-17T00:20:00Z")
    assert closing_odds == 1.60
    assert clv is not None and clv > 0     # la proba de Boston a monté → CLV positif


def test_compute_clv_none_when_selection_not_quoted(conn):
    from analyzer.preprocessing import preprocess
    _snap(conn, book="pinnacle", sel="Miami Heat", line=5.0, odds=1.91, at="2026-01-17T00:00:00Z")
    conn.commit()
    data = preprocess(conn, "m1")
    closing_odds, clv = compute_clv(data, market="spreads", selection="Boston Celtics",
                                    decided_at="2026-01-16T22:05:00Z", tipoff_utc="2026-01-17T00:20:00Z")
    assert closing_odds is None and clv is None


def test_compute_clv_none_when_same_snapshot(conn):
    """Correctif 1 : CLV None si verdict et clôture sur le même snapshot (pas de collecte entre).
    
    Garde-fou ajouté hier (closing.snapshot_at == opening.snapshot_at) pour distinguer
    CLV None (non mesurable) de CLV 0,0 (marché stable). Ce test vérifie que le garde-fou
    fonctionne et n'est pas juste le repli None existant (closing/opening manquant).
    """
    from analyzer.preprocessing import preprocess
    # Un seul snapshot : verdict et clôture tombent sur le même relevé.
    for sel, ln, od in [("Boston Celtics", -5.0, 1.91), ("Miami Heat", 5.0, 1.91)]:
        _snap(conn, book="pinnacle", sel=sel, line=ln, odds=od, at="2026-01-16T23:00:00Z")
    conn.commit()
    data = preprocess(conn, "m1")
    # Verdict à 23:05, clôture à 00:20 → les deux pointent vers le snapshot 23:00.
    closing_odds, clv = compute_clv(data, market="spreads", selection="Boston Celtics",
                                    decided_at="2026-01-16T23:05:00Z", tipoff_utc="2026-01-17T00:20:00Z")
    # closing_odds est renvoyé (1.91), mais CLV est None (non mesurable).
    assert closing_odds == 1.91
    assert clv is None  # Garde-fou : même snapshot → CLV non mesurable


def test_end_to_end_verdict_to_closing_clv_is_measurable(tmp_path):
    """Preuve du correctif 2026-07-20 (pas seulement le mécanisme, le BUT) :

    verdict réel produit par l'analyseur à H-2, re-décision non matérielle à H-1
    (decided_at figé), clôture distincte stockée à H-0.4 (analyse sautée, decided_at
    toujours figé), et `compute_clv` renvoie enfin une valeur NON NULLE et du bon
    signe — la panne (CLV=None sur les 6 verdicts du 18-19/07, verdict et clôture
    sur le même snapshot) est bien résolue de bout en bout.
    """
    from analyzer.analyzer import analyze_open_matches
    from analyzer.preprocessing import preprocess
    from common.config import load_config

    config = load_config()
    db_path = tmp_path / "test.db"
    init_db(db_path)
    conn = get_connection(db_path)

    tipoff = datetime(2026, 1, 17, 0, 0, tzinfo=timezone.utc)
    db.insert_match(conn, match_id="m1", sport="basketball_wnba", home_team=BOS,
                     away_team=MIA, tipoff_utc=tipoff.isoformat(),
                     status="SUIVI", created_at="2026-01-16T09:00:00Z")

    # Ouverture (H-6) puis mouvement fort à H-2 (R1 + R4 -> score 6 -> SIGNAL Boston).
    for book in ("a", "b", "c", "d"):
        for sel, ln, od, at in [
            (BOS, -2.0, 1.91, "2026-01-16T18:00:00Z"), (MIA, 2.0, 1.91, "2026-01-16T18:00:00Z"),
            (BOS, -5.0, 1.70, "2026-01-16T22:00:00Z"), (MIA, 5.0, 2.15, "2026-01-16T22:00:00Z"),
        ]:
            db.insert_snapshot(conn, match_id="m1", bookmaker=book, market="spreads",
                                selection=sel, line=ln, odds=od, snapshot_at=at)
        db.insert_snapshot(conn, match_id="m1", bookmaker=book, market="h2h",
                            selection=BOS, line=None, odds=1.90, snapshot_at="2026-01-16T18:00:00Z")
        db.insert_snapshot(conn, match_id="m1", bookmaker=book, market="h2h",
                            selection=MIA, line=None, odds=1.90, snapshot_at="2026-01-16T18:00:00Z")
    conn.commit()

    # Analyse à H-2 : verdict SIGNAL Boston.
    analyze_open_matches(conn, config, now=tipoff - timedelta(hours=2))
    verdict = conn.execute("SELECT * FROM verdicts WHERE match_id='m1'").fetchone()
    assert verdict["verdict"] == "SIGNAL"
    assert verdict["selection"] == BOS
    decided_at = verdict["decided_at"]

    # Re-décision à H-1 (rien de nouveau -> non matérielle) : decided_at inchangé.
    analyze_open_matches(conn, config, now=tipoff - timedelta(hours=1))
    v_h1 = conn.execute("SELECT decided_at FROM verdicts WHERE match_id='m1'").fetchone()
    assert v_h1["decided_at"] == decided_at

    # Clôture (H-0.4, stockée par le collecteur) : Boston se raccourcit encore.
    for book in ("a", "b", "c", "d"):
        db.insert_snapshot(conn, match_id="m1", bookmaker=book, market="spreads",
                            selection=BOS, line=-6.0, odds=1.55, snapshot_at="2026-01-16T23:36:00Z")
        db.insert_snapshot(conn, match_id="m1", bookmaker=book, market="spreads",
                            selection=MIA, line=6.0, odds=2.50, snapshot_at="2026-01-16T23:36:00Z")
    conn.commit()

    # Tick de clôture (H-0.4 < decision_min_hours=0.55) : analyse sautée, decided_at figé.
    analyze_open_matches(conn, config, now=tipoff - timedelta(hours=0.4))
    v_closing = conn.execute("SELECT decided_at FROM verdicts WHERE match_id='m1'").fetchone()
    assert v_closing["decided_at"] == decided_at

    # Le CLV se mesure enfin contre le VRAI verdict (H-2), pas contre la clôture.
    data = preprocess(conn, "m1")
    closing_odds, clv = compute_clv(data, market="spreads", selection=BOS,
                                    decided_at=decided_at, tipoff_utc=tipoff.isoformat())
    assert closing_odds == 1.55
    assert clv is not None
    assert clv > 0   # Boston continue de se raccourcir après le verdict -> CLV positif
    conn.close()


# ─────────────────────── orchestration bout en bout ───────────────────────

def _fake_results_client(games):
    class _Fake:
        def get_games(self, start, end):
            return games
        def close(self):
            pass
    return _Fake()


def _settings() -> Settings:
    return Settings(odds_api_key="", balldontlie_api_key="k", telegram_bot_token="",
                    telegram_chat_id="", database_path=Path("x.db"), log_level="INFO")


CONFIG = {
    "api": {"sport": "basketball_wnba"},
    "display": {"timezone": "Europe/Paris"},
    "results": {"calendar_timezone": "America/New_York",
                "base_url": "https://api.balldontlie.io",
                "games_paths": {"basketball_nba": "/v1/games", "basketball_wnba": "/wnba/v1/games"}},
    "evaluator": {"lookback_days": 3},
}


def test_evaluate_pending_grades_and_marks_evalue(conn):
    """Un match clos + verdict → évaluation écrite, statut EVALUE, résumé cohérent."""
    db.insert_verdict(conn, match_id="m1", verdict="SIGNAL", selection="Boston Celtics",
                      market="spreads", line=-5.0, odds_at_verdict=1.91, signal_score=6,
                      rules_triggered=json.dumps(["R1"]), rationale="…",
                      decided_at="2026-01-16T23:20:00Z")
    conn.commit()

    games = [_game("2026-01-16", BOS, MIA, 110, 100)]  # Boston gagne de 10 → couvre -5
    from datetime import datetime, timezone
    summary = evaluate_pending(conn, _settings(), CONFIG,
                               results_client=_fake_results_client(games),
                               telegram_client=None,
                               now=datetime(2026, 1, 17, 12, 0, tzinfo=timezone.utc))

    assert summary["evaluated"] == 1
    ev = conn.execute("SELECT * FROM evaluations").fetchone()
    assert ev["outcome"] == "won" and ev["home_score"] == 110
    assert conn.execute("SELECT status FROM matches WHERE match_id='m1'").fetchone()["status"] == "EVALUE"


def test_evaluate_pending_skips_when_no_result_recent(conn):
    """Match reporté (pas de résultat) et récent → sauté, reste CLOS pour réessai."""
    db.insert_verdict(conn, match_id="m1", verdict="NO_BET", selection="Boston Celtics",
                      market="spreads", line=-5.0, odds_at_verdict=1.91, signal_score=0,
                      rules_triggered="[]", rationale="…", decided_at="2026-01-16T23:20:00Z")
    conn.commit()

    from datetime import datetime, timezone
    summary = evaluate_pending(conn, _settings(), CONFIG,
                               results_client=_fake_results_client([]),  # aucun résultat
                               now=datetime(2026, 1, 17, 12, 0, tzinfo=timezone.utc))

    assert summary == {"evaluated": 0, "given_up": 0, "skipped": 1, "ungradable": 0}
    assert conn.execute("SELECT status FROM matches WHERE match_id='m1'").fetchone()["status"] == "CLOS"


def test_evaluate_pending_gives_up_on_old_unmatched(conn):
    """Match sans résultat au-delà de la fenêtre → EVALUE (abandon), pas de rescan infini."""
    db.insert_verdict(conn, match_id="m1", verdict="SIGNAL", selection="Boston Celtics",
                      market="spreads", line=-5.0, odds_at_verdict=1.91, signal_score=6,
                      rules_triggered="[]", rationale="…", decided_at="2026-01-16T23:20:00Z")
    conn.commit()

    from datetime import datetime, timezone
    summary = evaluate_pending(conn, _settings(), CONFIG,
                               results_client=_fake_results_client([]),
                               now=datetime(2026, 1, 25, 12, 0, tzinfo=timezone.utc))  # 8 j plus tard

    assert summary["given_up"] == 1
    assert conn.execute("SELECT status FROM matches WHERE match_id='m1'").fetchone()["status"] == "EVALUE"


def test_evaluate_pending_rejects_zero_zero_scores(conn):
    """Correctif 2 : Garde-fou grading scores 0-0 (bug API balldontlie).
    
    L'API renvoie parfois status="post" avec scores 0-0 (données invalides). Le grading
    doit rejeter ces scores pour éviter des pushes erronés. Aucune évaluation n'est écrite,
    ungradable est incrémenté, le match reste CLOS pour réessai.
    """
    db.insert_verdict(conn, match_id="m1", verdict="SIGNAL", selection="Boston Celtics",
                      market="spreads", line=-5.0, odds_at_verdict=1.91, signal_score=6,
                      rules_triggered=json.dumps(["R1"]), rationale="…",
                      decided_at="2026-01-16T23:20:00Z")
    conn.commit()

    # API renvoie status="Final" mais scores 0-0 (bug)
    games = [_game("2026-01-16", BOS, MIA, 0, 0, status="Final")]
    from datetime import datetime, timezone
    summary = evaluate_pending(conn, _settings(), CONFIG,
                               results_client=_fake_results_client(games),
                               telegram_client=None,
                               now=datetime(2026, 1, 17, 12, 0, tzinfo=timezone.utc))

    # Le match est rejeté : ungradable incrémenté, aucune évaluation écrite
    assert summary["ungradable"] == 1
    assert summary["evaluated"] == 0
    
    # Aucune évaluation en base (pas de push erroné)
    evals = conn.execute("SELECT * FROM evaluations").fetchall()
    assert len(evals) == 0
    
    # Le match reste CLOS (pas passé en EVALUE) pour réessai
    assert conn.execute("SELECT status FROM matches WHERE match_id='m1'").fetchone()["status"] == "CLOS"


# ─────────────────────────── bilan ───────────────────────────

def test_success_rate_excludes_push_from_denominator():
    # 1 gagné, 1 perdu, 1 push → 1 / (1+1) = 50 % (le push n'est PAS au dénominateur).
    assert success_rate(["won", "lost", "push"]) == 0.5
    # Que des pushes → aucune issue décisive → None.
    assert success_rate(["push", "push"]) is None
    assert success_rate([]) is None


def test_format_daily_report_shows_outcome_clv_rate_and_guardrail():
    lines = [
        EvalLine(home_team=BOS, away_team=MIA, verdict="SIGNAL", selection="Boston Celtics",
                 home_score=110, away_score=100, outcome="won", clv=0.03, position_action="take"),
        EvalLine(home_team="Lakers", away_team="Suns", verdict="SIGNAL", selection="Lakers",
                 home_score=100, away_score=100, outcome="push", clv=None, position_action=None),
    ]
    msg = format_daily_report("17/01/2026", lines, total_evals=12)
    assert "Bilan du 17/01/2026" in msg
    assert "✅ gagné" in msg and "➖ push" in msg
    assert "CLV +3,0 pts" in msg
    assert "ta prise" in msg
    # Taux hors push : 1 gagné, 0 perdu, 1 push → 100 %.
    assert "taux 100 % (hors push)" in msg
    assert "bruit statistique" in msg  # garde-fou < 50 évaluations


def test_format_daily_report_nobet_display_without_symbols():
    """Correctif 5 : NO_BET affichés sans ✅/❌, taux séparé pour faux négatifs.
    
    Les NO_BET sont gradés contre-factuellement (mesure des faux négatifs). Ils doivent
    afficher "aurait gagné (occasion manquée)" / "aurait perdu (abstention justifiée)"
    sans symboles, et le taux de réussite en pied ne doit agréger que SIGNAL/ANOMALIE.
    """
    lines = [
        EvalLine(home_team=BOS, away_team=MIA, verdict="SIGNAL", selection="Boston Celtics",
                 home_score=110, away_score=100, outcome="won", clv=0.03, position_action=None),
        EvalLine(home_team="Lakers", away_team="Suns", verdict="NO_BET", selection="Lakers",
                 home_score=105, away_score=100, outcome="won", clv=0.01, position_action=None),
        EvalLine(home_team="Nets", away_team="Heat", verdict="NO_BET", selection="Nets",
                 home_score=95, away_score=100, outcome="lost", clv=-0.01, position_action=None),
    ]
    msg = format_daily_report("18/07/2026", lines, total_evals=25)
    
    # NO_BET affichés sans ✅/❌
    assert "aurait gagné (occasion manquée)" in msg
    assert "aurait perdu (abstention justifiée)" in msg
    # Les symboles ✅/❌ ne doivent apparaître que pour le SIGNAL
    assert msg.count("✅") == 1  # Uniquement le SIGNAL gagné
    assert "❌" not in msg  # Aucun NO_BET ne doit avoir ❌
    
    # Taux de réussite séparé : SIGNAL uniquement (1 gagné, 0 perdu)
    assert "Bilan : 1 gagné(s), 0 perdu(s), 0 push — taux 100 % (hors push)" in msg
    # Ligne séparée pour les faux négatifs
    assert "NO_BET : 1/2 auraient gagné (faux négatifs)" in msg


def test_format_daily_report_empty():
    assert "Aucun verdict" in format_daily_report("17/01/2026", [], total_evals=0)


def test_format_degraded_report():
    """Bilan dégradé quand des matchs sont en attente mais sans résultats disponibles."""
    from evaluator.reporting import format_degraded_report
    msg = format_degraded_report("18/07/2026", pending_count=6, cause="résultats indisponibles")
    assert "Bilan du 18/07/2026" in msg
    assert "⚠️" in msg
    assert "0 match évalué" in msg
    assert "6 match(s) en attente" in msg
    assert "résultats indisponibles" in msg


def test_evaluate_pending_sends_degraded_report_when_no_results(conn):
    """Défaut 2 : envoi inconditionnel du bilan, même quand aucun match n'a pu être évalué."""
    db.insert_verdict(conn, match_id="m1", verdict="SIGNAL", selection="Boston Celtics",
                      market="spreads", line=-5.0, odds_at_verdict=1.91, signal_score=6,
                      rules_triggered="[]", rationale="…", decided_at="2026-01-16T23:20:00Z")
    conn.commit()

    # Mock telegram client pour capturer le message envoyé
    sent_messages = []
    class _FakeTelegram:
        is_configured = True
        def send_message(self, text, **kwargs):
            sent_messages.append(text)
            return True

    from datetime import datetime, timezone
    summary = evaluate_pending(conn, _settings(), CONFIG,
                               results_client=_fake_results_client([]),  # aucun résultat
                               telegram_client=_FakeTelegram(),
                               now=datetime(2026, 1, 17, 12, 0, tzinfo=timezone.utc))

    # Le match est sauté (pas de résultat)
    assert summary["skipped"] == 1
    assert summary["evaluated"] == 0
    
    # Un bilan dégradé doit avoir été envoyé
    assert len(sent_messages) == 1
    msg = sent_messages[0]
    assert "⚠️" in msg
    assert "0 match évalué" in msg
    assert "1 match(s) en attente" in msg
    assert "résultats indisponibles" in msg


def test_daily_report_idempotence(conn):
    """Correctif 6 : Idempotence du bilan quotidien via clé meta daily_report_sent_YYYY-MM-DD.
    
    Un bilan déjà envoyé pour une journée ne doit pas partir une seconde fois. Le bilan
    dégradé (0 match évalué) compte aussi comme envoyé. Un second appel le même jour
    ne doit envoyer aucun message.
    """
    db.insert_verdict(conn, match_id="m1", verdict="SIGNAL", selection="Boston Celtics",
                      market="spreads", line=-5.0, odds_at_verdict=1.91, signal_score=6,
                      rules_triggered=json.dumps(["R1"]), rationale="…",
                      decided_at="2026-01-16T23:20:00Z")
    conn.commit()

    games = [_game("2026-01-16", BOS, MIA, 110, 100)]
    
    # Mock telegram client pour compter les envois
    sent_messages = []
    class _FakeTelegram:
        is_configured = True
        def send_message(self, text, **kwargs):
            sent_messages.append(text)
            return True

    from datetime import datetime, timezone
    now = datetime(2026, 1, 17, 12, 0, tzinfo=timezone.utc)
    
    # Premier appel : bilan envoyé
    summary1 = evaluate_pending(conn, _settings(), CONFIG,
                                results_client=_fake_results_client(games),
                                telegram_client=_FakeTelegram(),
                                now=now)
    assert summary1["evaluated"] == 1
    assert len(sent_messages) == 1  # Un bilan envoyé
    
    # Vérifie que la clé meta est bien enregistrée
    report_key = "daily_report_sent_2026-01-17"
    assert db.get_meta(conn, report_key) == "true"
    
    # Second appel le même jour : aucun envoi (idempotence)
    summary2 = evaluate_pending(conn, _settings(), CONFIG,
                                results_client=_fake_results_client(games),
                                telegram_client=_FakeTelegram(),
                                now=now)
    assert summary2["evaluated"] == 0  # Aucune nouvelle évaluation (match déjà EVALUE)
    assert len(sent_messages) == 1  # Toujours 1 seul message (pas de doublon)


def test_invalidated_evaluations_excluded_from_aggregations(conn):
    """Correctif 3c : Les évaluations invalidées sont exclues des 4 fonctions d'agrégation.
    
    Une évaluation avec invalidated=1 ne doit apparaître ni dans count_evaluations(),
    ni dans count_evaluations_by_logic_version(), ni dans get_weekly_signal_evals(),
    ni dans get_weekly_nobet_evals().
    """
    from datetime import datetime, timezone
    from common.db import DECISION_LOGIC_VERSION
    
    # Crée 2 verdicts SIGNAL + 1 NO_BET pressenti
    v1_id = db.insert_verdict(conn, match_id="m1", verdict="SIGNAL", selection="Boston Celtics",
                              market="spreads", line=-5.0, odds_at_verdict=1.91, signal_score=6,
                              rules_triggered=json.dumps(["R1"]), rationale="…",
                              decided_at="2026-01-16T23:20:00Z", logic_version=DECISION_LOGIC_VERSION)
    v2_id = db.insert_verdict(conn, match_id="m1", verdict="SIGNAL", selection="Miami Heat",
                              market="h2h", line=None, odds_at_verdict=2.10, signal_score=7,
                              rules_triggered=json.dumps(["R2"]), rationale="…",
                              decided_at="2026-01-16T23:20:00Z", logic_version=DECISION_LOGIC_VERSION)
    v3_id = db.insert_verdict(conn, match_id="m1", verdict="NO_BET", selection="Lakers",
                              market="spreads", line=-3.0, odds_at_verdict=1.85, signal_score=0,
                              rules_triggered="[]", rationale="…",
                              decided_at="2026-01-16T23:20:00Z", logic_version=DECISION_LOGIC_VERSION)
    
    # Crée 3 évaluations : 2 valides + 1 invalidée
    now_iso = datetime(2026, 1, 17, 12, 0, tzinfo=timezone.utc).isoformat()
    db.insert_evaluation(conn, verdict_id=v1_id, home_score=110, away_score=100,
                        outcome="won", closing_odds=1.85, clv=0.03, evaluated_at=now_iso)
    db.insert_evaluation(conn, verdict_id=v2_id, home_score=110, away_score=100,
                        outcome="lost", closing_odds=2.05, clv=-0.02, evaluated_at=now_iso)
    db.insert_evaluation(conn, verdict_id=v3_id, home_score=110, away_score=100,
                        outcome="won", closing_odds=1.80, clv=0.01, evaluated_at=now_iso)
    
    # Invalide la 2e évaluation (verdict_id=v2_id)
    conn.execute("UPDATE evaluations SET invalidated = 1 WHERE verdict_id = ?", (v2_id,))
    conn.commit()
    
    # Test 1 : count_evaluations() exclut l'invalidée (2 valides sur 3 total)
    assert db.count_evaluations(conn) == 2
    
    # Test 2 : count_evaluations_by_logic_version() exclut l'invalidée
    assert db.count_evaluations_by_logic_version(conn, DECISION_LOGIC_VERSION) == 2
    
    # Test 3 : get_weekly_signal_evals() exclut l'invalidée (1 SIGNAL valide sur 2)
    since_iso = datetime(2026, 1, 16, 0, 0, tzinfo=timezone.utc).isoformat()
    signal_rows = db.get_weekly_signal_evals(conn, since_iso)
    assert len(signal_rows) == 1  # Seul v1 (won) est retourné, v2 (lost, invalidé) est exclu
    assert signal_rows[0]["verdict_id"] == v1_id
    assert signal_rows[0]["outcome"] == "won"
    
    # Test 4 : get_weekly_nobet_evals() exclut l'invalidée (1 NO_BET valide)
    nobet_rows = db.get_weekly_nobet_evals(conn, since_iso)
    assert len(nobet_rows) == 1
    assert nobet_rows[0]["outcome"] == "won"


# ─────────────────── CLV insensible aux snapshots post-tip-off ───────────────────

def test_clv_ignores_post_tipoff_snapshot(conn):
    """Un snapshot post-tip-off (cote live) ne fausse pas le calcul de clôture.

    Le CLV filtre sur snapshot_at <= tipoff_utc : les cotes live sont exclues.
    Bug 17/07 : 30 snapshots live à 00:45 (tip-off 23:10) auraient faussé la clôture
    sans ce filtre.
    """
    from evaluator.clv import compute_clv
    from analyzer.preprocessing import preprocess

    T0 = "2026-01-16T20:00:00+00:00"
    T1 = "2026-01-16T22:00:00+00:00"
    T2 = "2026-01-17T00:20:00+00:00"  # tip-off
    T3 = "2026-01-17T01:00:00+00:00"  # post-tip-off (live)

    db.insert_match(
        conn, match_id="m_clv", sport="basketball_wnba",
        home_team="Home", away_team="Away",
        tipoff_utc=T2, status="DECIDE", created_at=T0,
    )
    db.insert_verdict(
        conn, match_id="m_clv", verdict="SIGNAL", selection="Home", market="h2h",
        line=None, odds_at_verdict=1.90, signal_score=6,
        rules_triggered="[]", rationale="test", decided_at=T1,
        logic_version="test",
    )
    for book in ("a", "b", "c", "d", "e"):
        db.insert_snapshot(conn, match_id="m_clv", bookmaker=book, market="h2h", selection="Home", line=None, odds=1.90, snapshot_at=T0)
        db.insert_snapshot(conn, match_id="m_clv", bookmaker=book, market="h2h", selection="Away", line=None, odds=1.90, snapshot_at=T0)
        db.insert_snapshot(conn, match_id="m_clv", bookmaker=book, market="h2h", selection="Home", line=None, odds=1.85, snapshot_at=T1)
        db.insert_snapshot(conn, match_id="m_clv", bookmaker=book, market="h2h", selection="Away", line=None, odds=1.95, snapshot_at=T1)
        # Snapshot post-tip-off (cote live aberrante : 1.02)
        db.insert_snapshot(conn, match_id="m_clv", bookmaker=book, market="h2h", selection="Home", line=None, odds=1.02, snapshot_at=T3)
        db.insert_snapshot(conn, match_id="m_clv", bookmaker=book, market="h2h", selection="Away", line=None, odds=15.0, snapshot_at=T3)
    conn.commit()

    data = preprocess(conn, "m_clv")
    closing_odds, clv = compute_clv(
        data, market="h2h", selection="Home",
        decided_at=T1, tipoff_utc=T2,
    )
    # La cote de clôture doit être ~1.85 (T1, dernier avant tip-off T2),
    # PAS 1.02 (T3, post-tip-off, live).
    assert closing_odds is not None
    assert abs(closing_odds - 1.85) < 0.01  # clôture = T1, pas T3
