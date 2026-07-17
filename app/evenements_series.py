"""Recurring-activity series management, split out of admin.py to keep that
module within the file-size policy.

The single endpoint here appends dates to an activity's series in a strictly
additive way: it only inserts new occurrences, promotes a one-off to a series
master on the first append, skips dates already scheduled, and caps the series
length. It never deletes or overwrites an existing occurrence, so occurrences
that already carry attendance are left untouched.
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from . import activites
from .permissions_rbac import require_permission
from .schemas import OccurrenceIn, UserMe

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


class AjoutOccurrencesIn(BaseModel):
    """Extra dates to append to an activity's series (additive, never destructive)."""

    occurrences: list[OccurrenceIn] = Field(default_factory=list, max_length=104)


@router.post("/evenements/{evenement_id}/occurrences")
def ajouter_occurrences(
    evenement_id: str,
    payload: AjoutOccurrencesIn,
    user: Annotated[UserMe, Depends(require_permission("evenements.gerer"))],
) -> dict[str, int]:
    """Append dates to an activity's series (additive), via the shared engine also
    used by collaboration, so both apps append occurrences identically."""
    return activites.ajouter_occurrences_serie(evenement_id, payload.occurrences, user.id, user.role)
