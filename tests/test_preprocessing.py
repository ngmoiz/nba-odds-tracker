"""Tests du prétraitement de l'analyseur : dé-margeage et consensus médian."""
from __future__ import annotations

import pytest

from analyzer.preprocessing import preprocess, preprocess_rows
from common import db
from common.db import get_connection, init_db
from tests import fixtures as fx


def _quote(data, bookmaker, market, selection, snapshot_at):
    """Retrouve une cote dé-margée précise dans le MatchData."""
    for q in data.quotes:
        if (q.bookmaker, q.market, q.selection, q.snapshot_at) == (
            bookmaker, market, selection, snapshot_at
        ):
            return q
    raise AssertionError("cote introuvable")


def test_demargin_removes_vig_symmetric():
    """Cotes 1.90/1.90 → 50 %/50 % après retrait de la marge, somme = 100 %."""
    rows = fx.h2h("dk", "Home", "Away", 1.90, 1.90)
    data = preprocess_rows("m", rows)

    home = _quote(data, "dk", "h2h", "Home", fx.T[0])
    away = _quote(data, "dk", "h2h", "Away", fx.T[0])
    assert home.prob == pytest.approx(0.5, abs=1e-9)
    assert away.prob == pytest.approx(0.5, abs=1e-9)
    assert home.prob + away.prob == pytest.approx(1.0)


def test_demargin_asymmetric_odds():
    """Cotes 1.5/2.6 → probabilités dé-margées attendues (somme = 100 %)."""
    rows = fx.h2h("dk", "Home", "Away", 1.5, 2.6)
    data = preprocess_rows("m", rows)

    home = _quote(data, "dk", "h2h", "Home", fx.T[0])
    away = _quote(data, "dk", "h2h", "Away", fx.T[0])
    assert home.prob == pytest.approx(0.63415, abs=1e-4)
    assert away.prob == pytest.approx(0.36585, abs=1e-4)
    assert home.prob + away.prob == pytest.approx(1.0)


def test_consensus_prob_is_median_across_books():
    """Le consensus d'une issue est la médiane des probas dé-margées des books."""
    rows = (
        fx.h2h("a", "Home", "Away", 1.8, 2.0)   # home ~0.5263
        + fx.h2h("b", "Home", "Away", 1.9, 1.9)  # home 0.5000
        + fx.h2h("c", "Home", "Away", 2.0, 1.8)  # home ~0.4737
    )
    data = preprocess_rows("m", rows)

    series = data.consensus_series("h2h", "Home")
    assert len(series) == 1
    point = series[0]
    assert point.prob == pytest.approx(0.5, abs=1e-6)  # médiane des 3
    assert point.n_books == 3


def test_consensus_line_is_median_for_spreads():
    """Le consensus de ligne (spreads) est la médiane des lignes des books."""
    rows = (
        fx.spreads("a", "Home", "Away", -6.5, 1.91, 1.91)
        + fx.spreads("b", "Home", "Away", -7.5, 1.91, 1.91)
        + fx.spreads("c", "Home", "Away", -7.5, 1.91, 1.91)
    )
    data = preprocess_rows("m", rows)

    point = data.consensus_series("spreads", "Home")[0]
    assert point.line == pytest.approx(-7.5)  # médiane de [-7.5, -7.5, -6.5]


def test_h2h_consensus_line_is_none():
    """Le marché h2h n'a pas de ligne : le consensus doit avoir line = None."""
    data = preprocess_rows("m", fx.h2h("dk", "Home", "Away", 1.9, 1.9))
    assert data.consensus_series("h2h", "Home")[0].line is None


def test_opening_time_and_ordering():
    """opening_time renvoie le relevé le plus ancien ; times() est trié."""
    rows = fx.h2h("dk", "Home", "Away", 1.9, 1.9, snapshot_at=fx.T[2])
    rows += fx.h2h("dk", "Home", "Away", 1.85, 1.95, snapshot_at=fx.T[0])
    data = preprocess_rows("m", rows)

    assert data.opening_time() == fx.T[0]
    assert data.times() == [fx.T[0], fx.T[2]]
    # La série de consensus est triée chronologiquement.
    series = data.consensus_series("h2h", "Home")
    assert [p.snapshot_at for p in series] == [fx.T[0], fx.T[2]]


def test_single_book_consensus_equals_book():
    """Avec un seul book, le consensus vaut ce book (n_books = 1)."""
    data = preprocess_rows("m", fx.h2h("dk", "Home", "Away", 1.9, 1.9))
    point = data.consensus_series("h2h", "Home")[0]
    assert point.prob == pytest.approx(0.5)
    assert point.n_books == 1


def test_demargin_totals_sum_to_one():
    """Le dé-margeage s'applique aussi aux totals : Over + Under = 100 %."""
    data = preprocess_rows("m", fx.totals("dk", 210.5, 1.91, 1.91))
    over = _quote(data, "dk", "totals", "Over", fx.T[0])
    under = _quote(data, "dk", "totals", "Under", fx.T[0])
    assert over.prob == pytest.approx(0.5)
    assert over.prob + under.prob == pytest.approx(1.0)


def test_demargin_spreads_both_sides_sum_to_one():
    """Le dé-margeage s'applique aux deux côtés du spread : Home + Away = 100 %."""
    data = preprocess_rows("m", fx.spreads("dk", "Home", "Away", -3.5, 1.95, 1.87))
    home = _quote(data, "dk", "spreads", "Home", fx.T[0])
    away = _quote(data, "dk", "spreads", "Away", fx.T[0])
    assert home.prob + away.prob == pytest.approx(1.0)


def test_consensus_median_ignores_outlier_book():
    """Un book aberrant (cote périmée/erronée) est ignoré par la médiane.

    C'est la raison d'être du choix médiane vs moyenne : ici la moyenne serait
    tirée à ~0.56, la médiane reste à ~0.50.
    """
    rows = (
        fx.h2h("a", "Home", "Away", 1.90, 1.90)          # ~0.500
        + fx.h2h("b", "Home", "Away", 1.88, 1.92)         # ~0.505
        + fx.h2h("c", "Home", "Away", 1.92, 1.88)         # ~0.495
        + fx.h2h("d", "Home", "Away", 1.90, 1.90)         # ~0.500
        + fx.h2h("aberrant", "Home", "Away", 1.20, 4.50)  # ~0.790 (aberrant)
    )
    data = preprocess_rows("m", rows)
    point = data.consensus_series("h2h", "Home")[0]
    assert point.n_books == 5
    assert point.prob == pytest.approx(0.50, abs=0.01)  # l'aberrant n'influence pas la médiane


def test_preprocess_from_database(tmp_path):
    """Le chemin base de données charge et prétraite correctement les relevés."""
    db_path = tmp_path / "test.db"
    init_db(db_path)
    conn = get_connection(db_path)
    db.insert_match(
        conn, match_id="m1", sport="basketball_wnba", home_team="Home",
        away_team="Away", tipoff_utc=fx.T[3], status="SUIVI", created_at=fx.T[0],
    )
    for row in fx.h2h("dk", "Home", "Away", 1.9, 1.9) + fx.spreads("dk", "Home", "Away", -3.5, 1.91, 1.91):
        db.insert_snapshot(conn, match_id="m1", **row)
    conn.commit()

    data = preprocess(conn, "m1")
    conn.close()

    assert len(data.quotes) == 4  # 2 h2h + 2 spreads
    assert data.consensus_series("h2h", "Home")[0].prob == pytest.approx(0.5)
    assert data.consensus_series("spreads", "Home")[0].line == pytest.approx(-3.5)
