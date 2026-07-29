"""Read and change which permission belongs to which application.

The referential decides what an application-tagged access group actually grants, so
it is a security surface, not a display setting: moving a permission into an
application widens what every group tagged for that application confers. It is
therefore administered under the highest access permission, written in one
transaction, and journalled with what changed.
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from . import audit, db
from .permissions_rbac import require_permission, require_permission_ecriture
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/admin", tags=["acces"])

# The back office administers everything, so bounding it would leave an administrator
# unable to administer from the one place built for it. Refusing the change is
# clearer than accepting it and quietly ignoring it.
_APPLICATION_TOTALE = "back-office"


@router.get("/permissions-applications")
def lire_referentiel(
    user: Annotated[UserMe, Depends(require_permission("acces.administrer"))],
) -> dict[str, Any]:
    """The full permission-by-application grid, with the applications it applies to."""
    applications = db.fetch_all(
        "SELECT code, nom, actif FROM application ORDER BY nom", (), role=user.role,
    )
    couples = db.fetch_all(
        "SELECT permission, application_code FROM permission_application", (), role=user.role,
    )
    par_permission: dict[str, list[str]] = {}
    for c in couples:
        par_permission.setdefault(str(c["permission"]), []).append(str(c["application_code"]))
    # Count how many groups are tagged for each application: an administrator moving a
    # permission needs to know whether anybody is actually affected by the change.
    portee = db.fetch_all(
        "SELECT application_code, COUNT(DISTINCT mg.membre_id) AS membres "
        "FROM membre_groupe mg WHERE mg.actif = true AND mg.application_code IS NOT NULL "
        "GROUP BY application_code",
        (), role=user.role,
    )
    concernes = {str(p["application_code"]): int(p["membres"]) for p in portee}
    return {
        "applications": [
            {
                "code": str(a["code"]),
                "nom": a.get("nom"),
                "actif": bool(a.get("actif")),
                "administre_tout": str(a["code"]) == _APPLICATION_TOTALE,
                "membres_concernes": concernes.get(str(a["code"]), 0),
            }
            for a in applications
        ],
        "par_permission": {k: sorted(v) for k, v in par_permission.items()},
        "total_couples": len(couples),
    }


class ReferentielIn(BaseModel):
    permission: str = Field(min_length=1, max_length=120)
    applications: list[str] = Field(default_factory=list)


@router.put("/permissions-applications")
def enregistrer_referentiel(
    payload: ReferentielIn,
    user: Annotated[UserMe, Depends(require_permission_ecriture("acces.administrer"))],
) -> dict[str, Any]:
    """Set, for one permission, the exact list of applications it can be exercised from.

    The whole list is replaced in one transaction rather than patched item by item:
    a half-applied change would leave a right exercisable somewhere nobody chose.
    """
    cle = payload.permission.strip()
    demandees = {a.strip().lower() for a in payload.applications if a.strip()}

    with db.connection(role=user.role) as conn, conn.cursor() as cur:
        cur.execute("SELECT cle FROM permission WHERE cle = %s", (cle,))
        if not cur.fetchone():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="permission inconnue")
        if demandees:
            cur.execute("SELECT code FROM application WHERE code = ANY(%s)", (list(demandees),))
            connues = {str(r["code"]) for r in cur.fetchall()}
            inconnues = sorted(demandees - connues)
            if inconnues:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"application inconnue : {', '.join(inconnues)}",
                )
        # The administration console keeps every permission, whatever was submitted.
        demandees.add(_APPLICATION_TOTALE)

        cur.execute("SELECT application_code FROM permission_application WHERE permission = %s", (cle,))
        avant = {str(r["application_code"]) for r in cur.fetchall()}
        retirees = sorted(avant - demandees)
        ajoutees = sorted(demandees - avant)
        if retirees:
            cur.execute(
                "DELETE FROM permission_application WHERE permission = %s AND application_code = ANY(%s)",
                (cle, retirees),
            )
        for app in ajoutees:
            cur.execute(
                "INSERT INTO permission_application (permission, application_code) "
                "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (cle, app),
            )

    audit.log(user.id, user.role, "maj_permission_application", "permission", cle,
              {"ajoutees": ajoutees, "retirees": retirees})
    return {"permission": cle, "applications": sorted(demandees),
            "ajoutees": ajoutees, "retirees": retirees}
