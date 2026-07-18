"""Honorific function catalogue: member read, admin management and validation.

Members pick a function at registration (self-service). The honorific prefix is
only shown to others once an administrator has confirmed the member's function,
so an unearned title can never be displayed. The catalogue itself (labels, VIP
flag, ordering) is fully editable by the administration.
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from . import audit, db, fonctions_membre
from .auth import current_user
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1", tags=["fonctions"])

# The four attribution categories. Kept as a stable ASCII enum; the French label
# shown to the operator lives in the front-office, never in the stored code.
CATEGORIES = ("titre", "fonction_speciale", "fonction", "fonction_particuliere")


class FonctionIn(BaseModel):
    cle: str = Field(min_length=2, max_length=40, pattern="^[a-z0-9_]+$")
    libelle_h: str = Field(min_length=1, max_length=60)
    libelle_f: str = Field(min_length=1, max_length=60)
    libelle_n: str = Field(min_length=1, max_length=60)
    categorie: str = Field(default="fonction")
    abreviation: str | None = Field(default=None, max_length=20)
    est_vip: bool = False
    ordre: int = 100


class FonctionPatch(BaseModel):
    libelle_h: str | None = Field(default=None, max_length=60)
    libelle_f: str | None = Field(default=None, max_length=60)
    libelle_n: str | None = Field(default=None, max_length=60)
    categorie: str | None = None
    abreviation: str | None = Field(default=None, max_length=20)
    est_vip: bool | None = None
    ordre: int | None = None
    actif: bool | None = None


class MembreFonctionIn(BaseModel):
    fonction_cle: str | None = None
    confirmee: bool = True


_CATALOGUE_COLS = "cle, libelle_h, libelle_f, libelle_n, categorie, abreviation, est_vip, ordre, actif"


def _row_to_dict(r: dict[str, object]) -> dict[str, object]:
    return {
        "cle": r["cle"], "libelle_h": r["libelle_h"], "libelle_f": r["libelle_f"],
        "libelle_n": r["libelle_n"], "categorie": r.get("categorie") or "fonction",
        "abreviation": r.get("abreviation"), "est_vip": bool(r["est_vip"]),
        "ordre": r["ordre"], "actif": bool(r["actif"]),
    }


def _valider_categorie(categorie: object) -> str:
    valeur = str(categorie or "fonction")
    if valeur not in CATEGORIES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Catégorie invalide : {valeur}. Valeurs admises : {', '.join(CATEGORIES)}.",
        )
    return valeur


@router.get("/fonctions")
def catalogue_actif(user: Annotated[UserMe, Depends(current_user)]) -> list[dict[str, object]]:
    """Active catalogue, used to populate the registration selects for any member.
    Callers filter by ``categorie`` to build the four separate blocks."""
    rows = db.fetch_all(
        f"SELECT {_CATALOGUE_COLS} FROM fonction_honorifique WHERE actif = true ORDER BY categorie, ordre, cle",
        (),
        role=user.role,
    )
    return [_row_to_dict(r) for r in rows]


@router.get("/admin/fonctions")
def list_fonctions(user: Annotated[UserMe, Depends(require_permission("fonctions.consulter"))]) -> list[dict[str, object]]:
    rows = db.fetch_all(
        f"SELECT {_CATALOGUE_COLS} FROM fonction_honorifique ORDER BY categorie, ordre, cle",
        (),
        role=user.role,
    )
    return [_row_to_dict(r) for r in rows]


@router.post("/admin/fonctions")
def create_fonction(payload: FonctionIn, user: Annotated[UserMe, Depends(require_permission("fonctions.gerer"))]) -> dict[str, object]:
    categorie = _valider_categorie(payload.categorie)
    exists = db.fetch_one("SELECT cle FROM fonction_honorifique WHERE cle = %s", (payload.cle,), role=user.role)
    if exists:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="function already exists")
    abrege = (payload.abreviation or "").strip() or None
    db.execute(
        "INSERT INTO fonction_honorifique (cle, libelle_h, libelle_f, libelle_n, categorie, abreviation, est_vip, ordre, transversal, maj_par, maj_le) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())",
        (payload.cle, payload.libelle_h, payload.libelle_f, payload.libelle_n, categorie, abrege,
         payload.est_vip, payload.ordre, categorie == "fonction_particuliere", user.id),
        role=user.role,
    )
    audit.log(user.id, user.role, "creation_fonction", "fonction_honorifique", payload.cle, {"categorie": categorie})
    return {"ok": True, "cle": payload.cle}


@router.put("/admin/fonctions/{cle}")
def update_fonction(cle: str, payload: FonctionPatch, user: Annotated[UserMe, Depends(require_permission("fonctions.gerer"))]) -> dict[str, object]:
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="no field to update")
    if "categorie" in fields:
        fields["categorie"] = _valider_categorie(fields["categorie"])
        # The transversal flag follows the particular-function category.
        fields["transversal"] = fields["categorie"] == "fonction_particuliere"
    if "abreviation" in fields:
        fields["abreviation"] = (str(fields["abreviation"]).strip() or None) if fields["abreviation"] is not None else None
    sets = ", ".join(f"{k} = %s" for k in fields)
    row = db.execute(
        f"UPDATE fonction_honorifique SET {sets}, maj_par = %s, maj_le = now() WHERE cle = %s RETURNING cle",
        (*fields.values(), user.id, cle),
        role=user.role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown function")
    audit.log(user.id, user.role, "maj_fonction", "fonction_honorifique", cle, fields)
    return {"ok": True, "cle": cle}


@router.delete("/admin/fonctions/{cle}")
def retire_fonction(cle: str, user: Annotated[UserMe, Depends(require_permission("fonctions.gerer"))]) -> dict[str, object]:
    """Soft delete: keep the row so members already linked are not broken."""
    row = db.execute(
        "UPDATE fonction_honorifique SET actif = false, maj_par = %s, maj_le = now() WHERE cle = %s RETURNING cle",
        (user.id, cle),
        role=user.role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown function")
    audit.log(user.id, user.role, "retrait_fonction", "fonction_honorifique", cle, {})
    return {"ok": True, "cle": cle}


class ReaffecterFonctionIn(BaseModel):
    # Target active function to move the holders to, or null to clear the function
    # from its holders (remove their membre_fonction rows).
    cible_cle: str | None = None


@router.get("/admin/fonctions/{cle}/dependances")
def dependances_fonction(
    cle: str, user: Annotated[UserMe, Depends(require_permission("fonctions.consulter"))]
) -> dict[str, object]:
    """How many members currently hold this function (with a sample), and the active
    functions it can be reassigned to, so the administration can resolve the holders
    before deactivating a title instead of leaving them pointing at a dead key."""
    source = db.fetch_one("SELECT cle, categorie FROM fonction_honorifique WHERE cle = %s", (cle,), role=user.role)
    if not source:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown function")
    n = int((db.fetch_one("SELECT count(*) AS n FROM membre_fonction WHERE fonction_cle = %s", (cle,)) or {}).get("n") or 0)
    ech = [
        str(r["nom"]) for r in db.fetch_all(
            "SELECT m.nom_affiche AS nom FROM membre_fonction mf JOIN membre m ON m.id = mf.membre_id "
            "WHERE mf.fonction_cle = %s AND m.nom_affiche IS NOT NULL ORDER BY m.nom_affiche LIMIT 6",
            (cle,),
        )
    ]
    # Only offer targets of the SAME category: reassigning a title holder onto an
    # ordinary function would reintroduce the very category leak we are removing.
    cibles = [
        {"cle": r["cle"], "nom": r["libelle_h"]} for r in db.fetch_all(
            "SELECT cle, libelle_h FROM fonction_honorifique WHERE cle <> %s AND actif = true AND categorie = %s ORDER BY ordre, cle",
            (cle, source.get("categorie") or "fonction"), role=user.role,
        )
    ]
    return {"cle": cle, "categorie": source.get("categorie") or "fonction", "porteurs": n, "supprimable": n == 0, "echantillon": ech, "cibles": cibles}


@router.post("/admin/fonctions/{cle}/reaffecter")
def reaffecter_fonction(
    cle: str, payload: ReaffecterFonctionIn,
    user: Annotated[UserMe, Depends(require_permission("fonctions.gerer"))],
) -> dict[str, object]:
    """Move every holder of this function to a target active function, or detach them
    (remove the holder rows) when no target is given. Keeps the compact mirror on
    ``membre`` correct via ``sync_principale``. This is what unblocks a clean
    deactivation/retirement of a title."""
    if not db.fetch_one("SELECT cle FROM fonction_honorifique WHERE cle = %s", (cle,), role=user.role):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown function")
    cible = (payload.cible_cle or "").strip().lower() or None
    if cible:
        if cible == cle:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="la cible doit differer de la source")
        if cible in fonctions_membre.TITRES_INTERDITS:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="titre de consecration non affectable")
        if not db.fetch_one("SELECT cle FROM fonction_honorifique WHERE cle = %s AND actif = true", (cible,), role=user.role):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="fonction cible inconnue ou inactive")
    porteurs = int((db.fetch_one("SELECT count(*) AS n FROM membre_fonction WHERE fonction_cle = %s", (cle,)) or {}).get("n") or 0)
    membres = [str(r["membre_id"]) for r in db.fetch_all("SELECT DISTINCT membre_id FROM membre_fonction WHERE fonction_cle = %s", (cle,))]
    if cible:
        # Avoid a duplicate holder row when a member already has the target function.
        db.execute(
            "DELETE FROM membre_fonction WHERE fonction_cle = %s AND membre_id IN "
            "(SELECT membre_id FROM membre_fonction WHERE fonction_cle = %s)",
            (cle, cible),
        )
        db.execute("UPDATE membre_fonction SET fonction_cle = %s, maj_le = now() WHERE fonction_cle = %s", (cible, cle))
    else:
        db.execute("DELETE FROM membre_fonction WHERE fonction_cle = %s", (cle,))
    for mid in membres:
        fonctions_membre.sync_principale(mid, user.role)
    audit.log(user.id, user.role, "reaffectation_fonction", "fonction_honorifique", cle,
              {"cible": cible, "porteurs": porteurs})
    return {"reaffectes": porteurs, "cible_cle": cible}


@router.delete("/admin/fonctions/{cle}/definitif")
def supprimer_fonction_definitif(
    cle: str, user: Annotated[UserMe, Depends(require_permission("fonctions.gerer"))]
) -> dict[str, object]:
    """Definitive HARD delete of a title (distinct from the reversible deactivation).
    There is no FK on ``fonction_cle`` (the link from ``membre_fonction`` is by
    convention), so the row is only removed when it has zero holders: otherwise we
    refuse with 409 and the operator must reassign or detach the holders first
    (``/reaffecter``). Irreversible once done, and fully audited."""
    if not db.fetch_one("SELECT cle FROM fonction_honorifique WHERE cle = %s", (cle,), role=user.role):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown function")
    porteurs = int((db.fetch_one("SELECT count(*) AS n FROM membre_fonction WHERE fonction_cle = %s", (cle,)) or {}).get("n") or 0)
    if porteurs > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Suppression definitive impossible : {porteurs} porteur(s) utilisent encore ce titre. Reaffectez-les ou detachez-les d'abord.",
        )
    db.execute("DELETE FROM fonction_honorifique WHERE cle = %s", (cle,), role=user.role)
    audit.log(user.id, user.role, "suppression_definitive_fonction", "fonction_honorifique", cle, {})
    return {"ok": True, "cle": cle, "definitif": True}


@router.put("/admin/membres/{membre_id}/fonction")
def valider_fonction_membre(
    membre_id: str, payload: MembreFonctionIn, user: Annotated[UserMe, Depends(require_permission("membres.gerer"))]
) -> dict[str, object]:
    """Assign and/or confirm a member's primary function (admin validation).

    Kept for backward compatibility, it now operates on the ``membre_fonction``
    model (the single source of truth) and mirrors the result onto
    ``membre.fonction_cle`` through ``sync_principale``, so it can no longer
    diverge from the multi-function manager.
    """
    membre = db.fetch_one("SELECT id FROM membre WHERE id = %s", (membre_id,), role=user.role)
    if not membre:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")
    if payload.fonction_cle is not None:
        cle = payload.fonction_cle.strip()
        if cle.lower() in fonctions_membre.TITRES_INTERDITS:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="Berger/Bergère est un titre de consécration, pas une fonction : gérez-le dans le titre de consécration.")
        known = db.fetch_one("SELECT cle FROM fonction_honorifique WHERE cle = %s", (cle,), role=user.role)
        if not known:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown function")
        # Promote this function to primary in membre_fonction: clear the other
        # primary, then insert or promote the row for this cle.
        db.execute("UPDATE membre_fonction SET principale = false WHERE membre_id = %s", (membre_id,), role=user.role)
        existing = db.fetch_one(
            "SELECT id FROM membre_fonction WHERE membre_id = %s AND fonction_cle = %s", (membre_id, cle), role=user.role
        )
        if existing:
            db.execute(
                "UPDATE membre_fonction SET principale = true, confirmee = %s, actif = true, maj_le = now() WHERE id = %s",
                (payload.confirmee, existing["id"]), role=user.role,
            )
        else:
            db.execute(
                "INSERT INTO membre_fonction (membre_id, fonction_cle, confirmee, principale, ordre) VALUES (%s, %s, %s, true, 0)",
                (membre_id, cle, payload.confirmee), role=user.role,
            )
    else:
        # Only (re)confirm the current primary function.
        db.execute(
            "UPDATE membre_fonction SET confirmee = %s, maj_le = now() WHERE membre_id = %s AND principale = true",
            (payload.confirmee, membre_id), role=user.role,
        )
    fonctions_membre.sync_principale(membre_id, user.role)
    audit.log(user.id, user.role, "validation_fonction_membre", "membre", membre_id,
              {"fonction_cle": payload.fonction_cle, "confirmee": payload.confirmee})
    return {"ok": True}


# --- Multiple functions per member -----------------------------------------

class MembreFonctionCreate(BaseModel):
    fonction_cle: str = Field(min_length=2, max_length=40, pattern="^[a-z0-9_]+$")
    perimetre: str | None = Field(default=None, max_length=120)
    confirmee: bool = True
    principale: bool = False
    ordre: int = Field(default=100, ge=0, le=9999)


class MembreFonctionUpdate(BaseModel):
    perimetre: str | None = Field(default=None, max_length=120)
    confirmee: bool | None = None
    actif: bool | None = None
    principale: bool | None = None
    ordre: int | None = Field(default=None, ge=0, le=9999)


def _genre(membre_id: str, role: str) -> object:
    row = db.fetch_one("SELECT genre FROM membre WHERE id = %s", (membre_id,), role=role)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")
    return row.get("genre")


@router.get("/admin/membres/{membre_id}/fonctions")
def list_membre_fonctions(membre_id: str, user: Annotated[UserMe, Depends(require_permission("membres.consulter"))]) -> list[dict[str, object]]:
    """All functions held by a member (active or ended, confirmed or not)."""
    genre = _genre(membre_id, user.role)
    return fonctions_membre.fonctions_admin(membre_id, genre, user.role)


@router.post("/admin/membres/{membre_id}/fonctions", status_code=status.HTTP_201_CREATED)
def add_membre_fonction(
    membre_id: str, payload: MembreFonctionCreate, user: Annotated[UserMe, Depends(require_permission("membres.gerer"))]
) -> dict[str, object]:
    """Add a function to a member (a member may hold several). Berger/Bergere is a
    consecration title and is refused here."""
    cle = payload.fonction_cle.strip()
    if cle.lower() in fonctions_membre.TITRES_INTERDITS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Berger/Bergère est un titre de consécration, pas une fonction : gérez-le dans le titre de consécration.")
    known = db.fetch_one("SELECT cle FROM fonction_honorifique WHERE cle = %s", (cle,), role=user.role)
    if not known:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown function")
    _genre(membre_id, user.role)  # ensures the member exists
    if payload.principale:
        db.execute("UPDATE membre_fonction SET principale = false WHERE membre_id = %s", (membre_id,), role=user.role)
    db.execute(
        "INSERT INTO membre_fonction (membre_id, fonction_cle, perimetre, confirmee, principale, ordre) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (membre_id, cle, payload.perimetre, payload.confirmee, payload.principale, payload.ordre),
        role=user.role,
    )
    fonctions_membre.sync_principale(membre_id, user.role)
    audit.log(user.id, user.role, "ajout_fonction_membre", "membre", membre_id, {"fonction_cle": cle, "perimetre": payload.perimetre})
    return {"ok": True}


@router.patch("/admin/membres/{membre_id}/fonctions/{fonction_id}")
def update_membre_fonction(
    membre_id: str, fonction_id: str, payload: MembreFonctionUpdate,
    user: Annotated[UserMe, Depends(require_permission("membres.gerer"))],
) -> dict[str, object]:
    """Change a member's function (scope, confirmation, active state, primary, order)."""
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        return {"ok": True}
    # Confirm the (function, member) pair exists before touching the shared
    # ``principale`` flag, so a mismatched id never wipes every primary marker.
    exists = db.fetch_one(
        "SELECT id FROM membre_fonction WHERE id = %s AND membre_id = %s",
        (fonction_id, membre_id),
        role=user.role,
    )
    if not exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="function not found")
    if fields.get("principale") is True:
        db.execute("UPDATE membre_fonction SET principale = false WHERE membre_id = %s", (membre_id,), role=user.role)
    sets = ", ".join(f"{k} = %s" for k in fields)
    row = db.execute(
        f"UPDATE membre_fonction SET {sets}, maj_le = now() WHERE id = %s AND membre_id = %s RETURNING id",
        (*fields.values(), fonction_id, membre_id),
        role=user.role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="function not found")
    fonctions_membre.sync_principale(membre_id, user.role)
    audit.log(user.id, user.role, "modification_fonction_membre", "membre", membre_id, {"champs": list(fields)})
    return {"ok": True}


@router.delete("/admin/membres/{membre_id}/fonctions/{fonction_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_membre_fonction(
    membre_id: str, fonction_id: str, user: Annotated[UserMe, Depends(require_permission("membres.gerer"))]
) -> None:
    """Remove a function from a member."""
    row = db.execute(
        "DELETE FROM membre_fonction WHERE id = %s AND membre_id = %s RETURNING id",
        (fonction_id, membre_id),
        role=user.role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="function not found")
    fonctions_membre.sync_principale(membre_id, user.role)
    audit.log(user.id, user.role, "retrait_fonction_membre", "membre", membre_id, {"fonction_id": fonction_id})
