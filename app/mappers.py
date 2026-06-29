"""Row to response model mappers shared across routers (avoids duplication)."""
from __future__ import annotations

from typing import Any

from .schemas import MembreProfile

# Columns a member SELECT must expose for membre_row_to_profile, in order.
MEMBRE_PROFILE_SELECT = (
    "m.id, m.matricule, m.email, m.nom, m.prenoms, m.telephone, "
    "m.groupe, m.photo_url, m.statut, m.verifie, c.nom AS commission"
)


def membre_row_to_profile(row: dict[str, Any]) -> MembreProfile:
    """Map a member row (with a joined commission name) to its public profile."""
    return MembreProfile(
        id=str(row["id"]),
        matricule=row["matricule"],
        email=row["email"],
        nom=row["nom"],
        prenoms=row["prenoms"],
        telephone=row["telephone"],
        groupe=row["groupe"],
        photo_url=row["photo_url"],
        statut=row["statut"],
        verifie=row["verifie"],
        commission=row["commission"],
    )
