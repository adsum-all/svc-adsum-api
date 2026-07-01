"""Data retention policy: keep member data for a renewable window.

Nothing is deleted at validation. Each approved member's data and photo are kept
for a configurable window (default two years). A yearly notice informs the member
and renews the window automatically (auto-acceptance), so the record is preserved
while the RGPD information duty is met. The retention window is admin-configurable
(integration_config ``retention_annees``).
"""
from __future__ import annotations

from . import channels, db

_DEFAULT_YEARS = 2
# Send the yearly renewal notice once the last consent is older than this.
_RENEWAL_AFTER_DAYS = 335  # about eleven months


def retention_years() -> int:
    raw = channels.integration_value("retention_annees")
    try:
        value = int(raw) if raw else _DEFAULT_YEARS
    except (TypeError, ValueError):
        value = _DEFAULT_YEARS
    return max(1, value)


def set_retention(membre_id: str, role: str | None) -> None:
    """Start or reset the retention window for a member (called on approval)."""
    years = retention_years()
    db.execute(
        "UPDATE membre SET retention_consentement_le = now(), "
        "retention_echeance = now() + make_interval(years => %s) WHERE id = %s",
        (years, membre_id),
        role=role,
    )


def scan_renouvellement(role: str | None) -> int:
    """Yearly retention renewal: notify members whose consent is due and extend
    their window. Auto-acceptance: the window renews without a member action.

    Returns the number of members renewed. Import of ``notifier`` is deferred to
    avoid a circular import (notifications imports channels which imports db)."""
    from .notifications import notifier

    years = retention_years()
    due = db.fetch_all(
        "SELECT id, prenoms FROM membre WHERE verifie = true AND statut = 'actif' "
        "AND (retention_consentement_le IS NULL "
        "OR retention_consentement_le < now() - make_interval(days => %s)) "
        "ORDER BY retention_consentement_le NULLS FIRST LIMIT 500",
        (_RENEWAL_AFTER_DAYS,),
        role=role,
    )
    renewed = 0
    for m in due or []:
        prenom = (str(m.get("prenoms") or "").split(" ")[0]) or "cher membre"
        db.execute(
            "UPDATE membre SET retention_consentement_le = now(), "
            "retention_echeance = now() + make_interval(years => %s) WHERE id = %s",
            (years, str(m["id"])),
            role=role,
        )
        notifier(
            str(m["id"]), role, "retention_renouvellement",
            {"prenom": prenom, "annees": years}, ref_id="", dedup=False,
        )
        renewed += 1
    return renewed
