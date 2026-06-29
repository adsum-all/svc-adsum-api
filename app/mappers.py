"""Row to response model mappers shared across routers (avoids duplication)."""
from __future__ import annotations

from typing import Any

from .schemas import MembreProfile

# Columns a member SELECT must expose for membre_row_to_profile, in order.
MEMBRE_PROFILE_SELECT = (
    "m.id, m.matricule, m.email, m.nom, m.prenoms, m.telephone, m.groupe, m.photo_url, "
    "m.statut, m.verifie, m.genre, m.date_naissance, m.pays, m.ville, m.date_entree, "
    "m.cheminement_pastoral, m.statut_administratif, m.intendance_id, m.berger_referent_id, "
    "c.nom AS commission, i.nom AS intendance, bm.nom AS berger_nom, bm.prenoms AS berger_prenoms"
)

# The joins that back the columns above. Use together with MEMBRE_PROFILE_SELECT.
MEMBRE_PROFILE_FROM = (
    "FROM membre m "
    "LEFT JOIN commission c ON c.id = m.commission_id "
    "LEFT JOIN intendance i ON i.id = m.intendance_id "
    "LEFT JOIN utilisateur bu ON bu.id = m.berger_referent_id "
    "LEFT JOIN membre bm ON bm.id = bu.membre_id"
)


def _berger_name(row: dict[str, Any]) -> str | None:
    name = f"{row.get('berger_prenoms') or ''} {row.get('berger_nom') or ''}".strip()
    return name or None


def membre_row_to_profile(row: dict[str, Any]) -> MembreProfile:
    """Map a member row (with joined commission, intendance and shepherd) to its profile."""
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
        genre=row.get("genre"),
        date_naissance=row.get("date_naissance"),
        pays=row.get("pays"),
        ville=row.get("ville"),
        date_entree=row.get("date_entree"),
        cheminement_pastoral=row.get("cheminement_pastoral"),
        statut_administratif=row.get("statut_administratif"),
        commission=row.get("commission"),
        intendance=row.get("intendance"),
        intendance_id=str(row["intendance_id"]) if row.get("intendance_id") else None,
        berger_referent_id=str(row["berger_referent_id"]) if row.get("berger_referent_id") else None,
        berger=_berger_name(row),
    )
