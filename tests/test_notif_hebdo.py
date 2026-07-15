"""Weekly recap/agenda scheduling: per-member timezone, configurable week, once per week."""
# ruff: noqa: E501 - test data lines are long by nature
from __future__ import annotations

from datetime import UTC, datetime

from app import notifications as N


def test_debut_semaine_lundi() -> None:
    # Wednesday 2026-07-15 -> week starting Monday 2026-07-13 at 00:00 (jour_debut=0).
    local = datetime(2026, 7, 15, 14, 30, tzinfo=UTC)
    d = N._debut_semaine_locale(local, 0)
    assert (d.year, d.month, d.day, d.hour) == (2026, 7, 13, 0)


def test_debut_semaine_samedi_configurable() -> None:
    # Same Wednesday, but an org whose week starts Saturday (jour_debut=5) -> Saturday 2026-07-11.
    local = datetime(2026, 7, 15, 14, 30, tzinfo=UTC)
    d = N._debut_semaine_locale(local, 5)
    assert (d.year, d.month, d.day) == (2026, 7, 11)


def _run(now: datetime, tz: str, events: list[dict], monkeypatch) -> list[tuple[str, str]]:
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(N, "_semaine_jour_debut", lambda role: 0)
    monkeypatch.setattr(N.db, "fetch_all", lambda *a, **k: events)
    monkeypatch.setattr(N, "notifier", lambda mid, role, cle, ctx, ref_id="", dedup=False, **k: (sent.append((cle, ref_id)) or ["telegram"]))
    monkeypatch.setattr(N, "_prenom", lambda m: "Jean")
    result = {"recap": 0, "agenda": 0}
    N._hebdo_par_membre(now, None, [{"id": "M", "fuseau_horaire": tz}], "https://app", result)
    return sent


def test_delivered_after_local_monday_8am(monkeypatch) -> None:
    # Monday 2026-07-13 09:00 UTC, member in Abidjan (UTC+0) -> local Monday 09:00 -> eligible.
    now = datetime(2026, 7, 13, 9, 0, tzinfo=UTC)
    events = [
        {"titre": "Culte", "debut": datetime(2026, 7, 15, 15, 0, tzinfo=UTC), "fuseau_horaire": "Africa/Abidjan"},  # this week
        {"titre": "Reunion", "debut": datetime(2026, 7, 10, 15, 0, tzinfo=UTC), "fuseau_horaire": "Africa/Abidjan"},  # previous week
    ]
    sent = _run(now, "Africa/Abidjan", events, monkeypatch)
    kinds = {c for c, _ in sent}
    assert "agenda_hebdo" in kinds and "recap_hebdo" in kinds


def test_not_delivered_before_local_monday_8am(monkeypatch) -> None:
    # Monday 2026-07-13 02:00 UTC, member in Abidjan -> local Monday 02:00 -> before 08:00 -> nothing.
    now = datetime(2026, 7, 13, 2, 0, tzinfo=UTC)
    events = [{"titre": "Culte", "debut": datetime(2026, 7, 15, 15, 0, tzinfo=UTC), "fuseau_horaire": "Africa/Abidjan"}]
    sent = _run(now, "Africa/Abidjan", events, monkeypatch)
    assert sent == []


def test_ref_id_is_stable_per_week(monkeypatch) -> None:
    # The dedup ref_id is the local week key, so two runs the same week reuse it (idempotent).
    now = datetime(2026, 7, 13, 9, 0, tzinfo=UTC)
    events = [{"titre": "Culte", "debut": datetime(2026, 7, 15, 15, 0, tzinfo=UTC), "fuseau_horaire": "Africa/Abidjan"}]
    sent = _run(now, "Africa/Abidjan", events, monkeypatch)
    agenda_refs = [ref for cle, ref in sent if cle == "agenda_hebdo"]
    assert agenda_refs == ["2026-07-13"]
