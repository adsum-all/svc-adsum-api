"""Test configuration: provide a JWT secret for the security unit tests.

It also decides what happens to a test that needs a database when none is
configured. Seven files in this suite open a real connection; the other sixty-one
do not. Before this, those seven hung: psycopg was handed an empty connection
string, fell back to the local defaults where nothing listens, and waited. A whole
suite that never finishes is worse than one that fails, because nobody can tell a
broken test from an absent database.

They are now skipped, with one condition that matters: setting
ADSUM_TESTS_EXIGENT_BDD makes the same tests fail instead. The continuous
integration pipeline sets it. Without that switch, a pipeline that lost its
database would report green while running almost nothing, which is the failure mode
this file exists to prevent.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("ADSUM_JWT_SECRET", "test-secret-not-for-production")
os.environ.setdefault("ADSUM_JWT_ALGORITHM", "HS256")

#: Set this in CI. A missing database is then a failure, never a skip.
EXIGE_BDD = os.environ.get("ADSUM_TESTS_EXIGENT_BDD", "") not in ("", "0", "false")

#: What the refusal says when no connection string is configured. Matched on the
#: variable name rather than on the whole sentence, so rewording the message does
#: not silently stop the skip from applying.
SANS_BASE = "ADSUM_DATABASE_URL is not set"


def _sans_base(erreur: BaseException) -> bool:
    """Is this failure the absence of a database rather than a real defect?

    The cause chain is walked because the refusal usually surfaces wrapped: a route
    catches it, re-raises, and the test sees something else entirely.
    """
    vu: set[int] = set()
    courante: BaseException | None = erreur
    while courante is not None and id(courante) not in vu:
        vu.add(id(courante))
        if SANS_BASE in str(courante):
            return True
        courante = courante.__cause__ or courante.__context__
    return False


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: pytest.Item):
    resultat = yield
    if EXIGE_BDD:
        return
    echec = resultat.excinfo
    if echec is not None and _sans_base(echec[1]):
        resultat.force_exception(
            pytest.skip.Exception(
                f"{item.name} exige une base de données et aucune n'est configurée. "
                "Poser ADSUM_TESTS_EXIGENT_BDD pour en faire un échec.",
                _use_item_location=True,
            )
        )
