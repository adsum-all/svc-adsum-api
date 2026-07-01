"""Unit tests for the duplicate-detection scoring (pure, no database)."""
from __future__ import annotations

from app.doublons import _WEIGHTS, _WEIGHTS_PHOTO, _hamming_hex, _photo_similarity, _score


def test_weights_sum_to_one() -> None:
    assert abs(sum(_WEIGHTS.values()) - 1.0) < 1e-9


def test_photo_weights_sum_to_one() -> None:
    assert abs(sum(_WEIGHTS_PHOTO.values()) - 1.0) < 1e-9


def test_hamming_hex() -> None:
    assert _hamming_hex("0000000000000000", "0000000000000000") == 0
    assert _hamming_hex("0000000000000000", "0000000000000001") == 1
    assert _hamming_hex("0000000000000000", "ffffffffffffffff") == 64


def test_photo_similarity_identical_and_opposite() -> None:
    assert _photo_similarity("abcdef0123456789", "abcdef0123456789") == 1.0
    assert _photo_similarity("0000000000000000", "ffffffffffffffff") == 0.0
    assert _photo_similarity(None, "abcdef0123456789") is None
    assert _photo_similarity("short", "abcdef0123456789") is None


def test_score_uses_photo_when_both_present() -> None:
    row = {
        "name_sim": 1.0, "addr_sim": 1.0, "same_dob": True, "same_phone": True, "same_city": True,
        "a_phash": "abcdef0123456789", "b_phash": "abcdef0123456789",
    }
    score, signaux = _score(row)
    assert score == 1.0
    assert signaux["photo"] == 1.0


def test_identical_pair_scores_one() -> None:
    row = {"name_sim": 1.0, "addr_sim": 1.0, "same_dob": True, "same_phone": True, "same_city": True}
    score, signaux = _score(row)
    assert score == 1.0
    assert signaux["date_naissance"] is True
    assert signaux["telephone"] is True
    assert signaux["ville"] is True
    assert signaux["nom"] == 1.0
    assert signaux["adresse"] == 1.0


def test_no_signal_scores_zero() -> None:
    row = {"name_sim": 0.0, "addr_sim": 0.0, "same_dob": False, "same_phone": False, "same_city": False}
    score, signaux = _score(row)
    assert score == 0.0
    assert signaux["date_naissance"] is False
    assert signaux["telephone"] is False


def test_name_and_dob_only() -> None:
    row = {"name_sim": 1.0, "addr_sim": 0.0, "same_dob": True, "same_phone": False, "same_city": False}
    score, _ = _score(row)
    # name weight (0.35) + dob weight (0.25) = 0.60
    assert score == 0.6


def test_partial_name_similarity_is_weighted() -> None:
    row = {"name_sim": 0.5, "addr_sim": 0.0, "same_dob": False, "same_phone": True, "same_city": False}
    score, signaux = _score(row)
    # 0.35 * 0.5 + 0.20 (phone) = 0.375
    assert score == 0.375
    assert signaux["nom"] == 0.5


def test_none_similarity_is_treated_as_zero() -> None:
    row = {"name_sim": None, "addr_sim": None, "same_dob": False, "same_phone": False, "same_city": False}
    score, _ = _score(row)
    assert score == 0.0
