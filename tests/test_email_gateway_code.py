"""Unit tests for the one-time code cleaning and verification.

Locks in the fix for the real-world failure where a pasted code carried spaces (the
verification e-mail renders one box per digit, so a copy often yields "1 2 3 4 5 6"),
which made a valid code look wrong. The code must verify regardless of whitespace,
and its single-use fingerprint must be identical across spacings.
"""
from __future__ import annotations

from app import email_gateway as eg

EMAIL = "membre@example.com"
PURPOSE = "login_2fa"


def _valid_code() -> str:
    return eg.generate_code(EMAIL, PURPOSE)


def test_clean_code_keeps_digits_only():
    assert eg.clean_code("123 456") == "123456"
    assert eg.clean_code("1 2 3 4 5 6") == "123456"
    assert eg.clean_code("  123456\n") == "123456"
    assert eg.clean_code("123 456") == "123456"  # non-breaking space
    assert eg.clean_code("") == ""
    assert eg.clean_code(None) == ""


def test_verify_accepts_code_with_internal_spaces():
    code = _valid_code()
    spaced = f"{code[:3]} {code[3:]}"
    per_digit = " ".join(code)
    assert eg.verify_code(EMAIL, PURPOSE, code) is True
    assert eg.verify_code(EMAIL, PURPOSE, spaced) is True
    assert eg.verify_code(EMAIL, PURPOSE, per_digit) is True
    assert eg.verify_code(EMAIL, PURPOSE, f"  {code}\n") is True


def test_verify_rejects_wrong_code():
    code = _valid_code()
    wrong = str((int(code) + 1) % 1_000_000).zfill(6)
    assert eg.verify_code(EMAIL, PURPOSE, wrong) is False


def test_fingerprint_is_stable_across_spacing():
    code = _valid_code()
    variants = [code, f"{code[:2]} {code[2:]}", " ".join(code), f"\t{code} "]
    prints = {eg._code_fingerprint(EMAIL, PURPOSE, v) for v in variants}
    assert len(prints) == 1  # single-use tracking is consistent whatever the spacing


def test_fingerprint_differs_for_different_codes():
    a = eg._code_fingerprint(EMAIL, PURPOSE, "123456")
    b = eg._code_fingerprint(EMAIL, PURPOSE, "654321")
    assert a != b
