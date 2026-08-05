"""The direction dashboard's per-entity breakdown must reconcile and never guess.

This endpoint feeds the screen where the direction compares presence between
commissions, tribes, countries and so on. Its whole worth is that the numbers are
trustworthy: a read-only dashboard that shows a plausible but wrong figure is worse
than one that shows nothing, because nobody can tell the difference until a decision
has been made on it.

Two properties matter, and neither can be read off the source. The breakdown must
sum to the same total whatever axis it is cut along, since it is one set of
attendance facts grouped differently. And a dimension the platform does not compute
must be refused, not approximated into a shape that looks like an answer.

The consolidation is exercised against a replaced database so the arithmetic is
checked without depending on what production happens to hold today.
"""
from __future__ import annotations

import pytest

from app import stats_direction


class _FakeDb:
    """Answers the one consolidation query with a fixed set of attendance rows.

    The rows are keyed by whatever label expression the function built, so the same
    fake serves every dimension: the test asserts on the totals the function returns,
    not on the SQL it wrote.
    """

    def __init__(self, rows):
        self._rows = rows
        self.last_sql = ""

    def fetch_all(self, sql, params=(), role=None):  # noqa: ARG002
        self.last_sql = sql
        return self._rows


@pytest.fixture
def _rows(monkeypatch):
    def poser(rows):
        faux = _FakeDb(rows)
        monkeypatch.setattr(stats_direction, "db", faux)
        return faux

    return poser


def test_every_direction_dimension_is_offered():
    # The front's picker lists exactly these; a missing one is a screen that offers a
    # choice the server cannot serve.
    assert set(stats_direction.dimensions_direction()) == {
        "commission", "tribu", "coordination", "intendance", "pays", "continent", "volet",
    }


@pytest.mark.parametrize(
    "dimension",
    ["commission", "tribu", "coordination", "intendance", "pays", "continent", "volet"],
)
def test_a_known_dimension_returns_rows_in_the_front_s_shape(_rows, dimension):
    _rows([
        {"cle": "Alpha", "presents": 12, "partiels": 3, "absents": 5},
        {"cle": "Bêta", "presents": 7, "partiels": 0, "absents": 2},
    ])
    out = stats_direction.repartition_globale_par_entite(dimension, None)
    assert out == [
        {"label": "Alpha", "presents": 12, "partiels": 3, "absents": 5},
        {"label": "Bêta", "presents": 7, "partiels": 0, "absents": 2},
    ]


def test_an_unknown_dimension_is_refused_rather_than_guessed(_rows):
    faux = _rows([])
    assert stats_direction.repartition_globale_par_entite("salaire", None) is None
    # And the database was never touched: an unknown dimension is turned away before
    # it can reach a query, so no label expression it does not control is ever built.
    assert faux.last_sql == ""


def test_an_unknown_cross_is_refused_too(_rows):
    faux = _rows([])
    assert stats_direction.repartition_globale_par_entite("commission", None, "salaire") is None
    assert faux.last_sql == ""


def test_a_cross_builds_a_combined_label_from_both_axes(_rows):
    faux = _rows([{"cle": "Alpha - A", "presents": 4, "partiels": 1, "absents": 0}])
    out = stats_direction.repartition_globale_par_entite("commission", None, "volet")
    assert out == [{"label": "Alpha - A", "presents": 4, "partiels": 1, "absents": 0}]
    # Both dimension expressions are present in the query, joined into one label.
    assert "commission" in faux.last_sql.lower()
    assert "e.volet" in faux.last_sql


def test_the_breakdown_sums_the_same_whatever_the_axis(_rows):
    # One member is either present, partial or absent per event, so the present count
    # is a fixed total of facts however they are grouped. The fake returns the same
    # facts split two ways; the function must not invent or lose any.
    faux = _rows([
        {"cle": "x", "presents": 10, "partiels": 4, "absents": 6},
        {"cle": "y", "presents": 5, "partiels": 1, "absents": 3},
    ])
    par_commission = stats_direction.repartition_globale_par_entite("commission", None)
    faux._rows = [
        {"cle": "a", "presents": 9, "partiels": 3, "absents": 4},
        {"cle": "b", "presents": 6, "partiels": 2, "absents": 5},
    ]
    par_pays = stats_direction.repartition_globale_par_entite("pays", None)
    assert sum(r["presents"] for r in par_commission) == sum(r["presents"] for r in par_pays) == 15


def test_the_query_only_counts_validated_attendance(_rows):
    faux = _rows([])
    stats_direction.repartition_globale_par_entite("commission", None)
    # The same COMPTE guard every other consolidation uses: a declaration not yet
    # validated must not inflate the direction's figures.
    assert stats_direction.COMPTE in faux.last_sql
