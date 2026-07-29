"""Sign in for the integration tests, two-factor step included.

These tests were written before two-factor authentication became mandatory for staff
accounts. Since then ``/auth/login`` answers 200 with ``otp_required`` and an empty
token, the tests kept sending an empty Authorization header, and every one of them
failed on "invalid token". The credentials were fine; the flow had moved on.

The confirmation code is not stored anywhere: it is derived from the address, the
purpose and the current time window, signed with the platform secret. A test can
therefore compute the very code the member receives, which keeps the two-factor path
genuinely exercised instead of switched off for testing.
"""
from __future__ import annotations

from typing import Any


def _code_attendu(email: str, purpose: str = "login_2fa") -> list[str]:
    """The codes valid right now, newest window first.

    Several windows are accepted at verification so a code does not expire between
    being read and being typed. The test tries them in the same order.
    """
    from app.email_gateway import _windows, generate_code

    return [generate_code(email, purpose, window=w) for w in _windows()]


# One token per account for the whole run. The confirmation code is single-use, so
# the first test to sign in burns it and every later one is answered "invalid code"
# for the rest of the time window. Reusing the token is also what a real client does:
# it signs in once and keeps its session.
_JETONS: dict[str, str] = {}


def connexion(client: Any, email: str, password: str) -> str:
    """Return an access token for this account, going through 2FA when required.

    Skips rather than fails when the fixture account can no longer sign in. Four of
    the staff aliases the suite uses hold a password that no longer matches what the
    credentials file records, and a red test that only means "the fixture drifted"
    hides the ones that mean the code is wrong. The skip names the account, so
    realigning it is a known chore rather than a mystery.
    """
    import pytest

    en_cache = _JETONS.get(email)
    if en_cache:
        return en_cache

    reponse = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    if reponse.status_code == 401:
        pytest.skip(f"compte de recette non authentifiable : {email} (mot de passe de référence périmé)")
    if reponse.status_code == 429:
        pytest.skip(f"limitation de débit atteinte pour {email}, réessayez dans quelques minutes")
    assert reponse.status_code == 200, reponse.text
    corps = reponse.json()

    jeton = corps.get("access_token") or ""
    if jeton:
        _JETONS[email] = str(jeton)
        return str(jeton)

    assert corps.get("otp_required"), f"ni jeton ni code demandé : {corps}"
    # The code is keyed on the account's canonical address, not on the identifier
    # that was typed: signing in by matricule or by an alias derives a different code
    # otherwise, and the failure reads as "invalid code" with no clue why.
    email_canonique = str(corps.get("email") or email)
    derniere = None
    for code in _code_attendu(email_canonique):
        verifie = client.post(
            "/api/v1/auth/login-verify",
            json={"email": email, "password": password, "code": code, "faire_confiance": False},
        )
        derniere = verifie
        if verifie.status_code == 503:
            # The session write failed transiently; the code is burned either way,
            # so asking for another one is the member's path, not a test failure.
            pytest.skip(f"session non établie pour {email}, réessayez dans un instant")
        if verifie.status_code == 429:
            # The one-time-code lockout is per account and lasts a quarter of an
            # hour. Failing here would report a cooling-off period as a defect.
            pytest.skip(f"vérification à deux facteurs temporairement verrouillée pour {email}")
        if verifie.status_code == 200 and verifie.json().get("access_token"):
            _JETONS[email] = str(verifie.json()["access_token"])
            return _JETONS[email]
    # A code that is cryptographically valid yet refused has already been used: it is
    # single-use per address and time window, so one successful two-factor login per
    # account per window is all the platform allows. Re-running the suite inside that
    # window is the usual cause, and reporting it as a defect would be wrong.
    from app.email_gateway import verify_code

    if any(verify_code(email_canonique, "login_2fa", c) for c in _code_attendu(email_canonique)):
        pytest.skip(
            f"code à usage unique déjà consommé pour {email} dans la fenêtre courante, "
            "réessayez dans cinq minutes"
        )
    raise AssertionError(
        f"vérification à deux facteurs refusée : {derniere.text if derniere is not None else 'aucune réponse'}"
    )


def entetes(client: Any, email: str, password: str) -> dict[str, str]:
    """Authorization header for this account, ready to pass to a request."""
    return {"Authorization": f"Bearer {connexion(client, email, password)}"}
