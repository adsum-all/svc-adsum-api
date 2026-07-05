"""Unit tests for the external check-in helpers (no database).

They pin the two security-critical pure functions of the public link: the phone
matching used to identify a member, and the HMAC token that binds an identified
member to one event so the submit cannot mark anyone else.
"""
from __future__ import annotations

import time

import pytest

from app import emargement


def test_phone_matches_indicatif_and_national_number() -> None:
    # Stored dial code apart, national number with a trunk zero.
    assert emargement._phone_matches("+33", "06 12 34 56 78", "+33", "0612345678")


def test_phone_matches_when_stored_number_embeds_indicatif() -> None:
    # Stored number already international, no separate dial code.
    assert emargement._phone_matches("33", "612345678", None, "0033612345678")


def test_phone_matches_same_national_number() -> None:
    assert emargement._phone_matches("225", "07 000 000", "", "07000000")


def test_phone_does_not_match_different_number() -> None:
    assert not emargement._phone_matches("+33", "0612345678", "+33", "0699999999")


def test_phone_does_not_match_empty() -> None:
    assert not emargement._phone_matches("+33", "", "+33", "0612345678")
    assert not emargement._phone_matches("+33", "0612345678", "+33", None)


def test_norm_matricule_upper_and_no_space() -> None:
    assert emargement._norm_matricule("  ads-000001 ") == "ADS-000001"
    assert emargement._norm_matricule("ads 2026 000001") == "ADS2026000001"


def test_token_roundtrip_returns_member() -> None:
    token = emargement._sign("m-1", "e-1")
    assert emargement._verify(token, "e-1") == "m-1"


def test_token_rejected_for_another_event() -> None:
    token = emargement._sign("m-1", "e-1")
    assert emargement._verify(token, "e-2") is None


def test_token_rejected_when_tampered() -> None:
    token = emargement._sign("m-1", "e-1")
    tampered = token[:-2] + ("aa" if not token.endswith("aa") else "bb")
    assert emargement._verify(tampered, "e-1") is None


def test_token_rejected_when_expired() -> None:
    token = emargement._sign("m-1", "e-1", ttl=-1)
    assert emargement._verify(token, "e-1") is None


def test_token_rejected_when_garbage() -> None:
    assert emargement._verify("not-a-token", "e-1") is None
    assert emargement._verify("", "e-1") is None


class _Payload:
    def __init__(self, statut: str, modalite: str | None) -> None:
        self.statut = statut
        self.modalite = modalite


def test_resolve_scan_overrides_declaration() -> None:
    statut, modalite = emargement._resoudre_statut_modalite({"source": "scan"}, _Payload("absent", None))
    assert (statut, modalite) == ("present", "presentiel")


def test_resolve_absent_clears_modality() -> None:
    statut, modalite = emargement._resoudre_statut_modalite(None, _Payload("absent", "en_ligne"))
    assert (statut, modalite) == ("absent", None)


def test_resolve_present_requires_modality() -> None:
    with pytest.raises(Exception):
        emargement._resoudre_statut_modalite(None, _Payload("present", None))


def test_resolve_present_keeps_modality() -> None:
    statut, modalite = emargement._resoudre_statut_modalite(None, _Payload("present", "en_ligne"))
    assert (statut, modalite) == ("present", "en_ligne")
