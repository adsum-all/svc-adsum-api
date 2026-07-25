"""Full sheet of an access group: documentation, members, history and lineage.

A group used to be a label and a list of permission keys, which left an
administrator guessing what granting it really means. This module serves everything
needed to decide before granting: what the group is for, when it should not be used,
how far it reaches, what it puts at risk, who is in it, what happened to it, and,
for a copy of a standard group, exactly how it drifted from its source.

It also carries the one write operation the standard groups need. A system group is
protected and stays read only; duplicating it produces a custom, editable group that
remembers its origin, so an administrator can start from a reliable base instead of
rebuilding a permission set by hand.

Kept apart from ``groupes.py``, which is already at its size limit.
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from . import audit, db
from .fields import ShortStr
from .permissions_rbac import require_permission, require_permission_ecriture
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/admin", tags=["groupes"])

# Audit actions that concern a group, mapped to a readable sentence for the sheet.
_ACTIONS_HISTORIQUE = {
    "creation_groupe_acces": "Création du groupe",
    "modification_groupe_acces": "Modification du groupe",
    "suppression_groupe_acces": "Suppression du groupe",
    "duplication_groupe_acces": "Duplication en groupe personnalisé",
    "ajout_groupe_acces": "Ajout d'un membre",
    "retrait_groupe_acces": "Retrait d'un membre",
    "octroi_groupe_application": "Octroi d'un accès applicatif",
    "retrait_groupe_application": "Retrait d'un accès applicatif",
}


def _groupe_ou_404(groupe_id: str, role: str) -> dict[str, Any]:
    row = db.fetch_one(
        "SELECT id, cle, libelle, description, role_accorde, mode, systeme, actif, application_code, "
        "finalite, usage_recommande, usage_deconseille, portee_texte, sensibilite, avertissement_securite, "
        "version, cree_le, cree_par, maj_le, maj_par, source_standard_group_id, derived_from_standard, "
        "source_version, inheritance_mode, custom_scope "
        "FROM groupe_acces WHERE id = %s",
        (groupe_id,), role=role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="groupe inconnu")
    return row


def _permissions(groupe_id: str, role: str) -> list[str]:
    return [
        str(r["permission"])
        for r in db.fetch_all(
            "SELECT permission FROM groupe_permission WHERE groupe_id = %s ORDER BY permission",
            (groupe_id,), role=role,
        )
    ]


def _detail_permissions(cles: list[str]) -> list[dict[str, Any]]:
    """Each permission with what it allows and what it explicitly does not."""
    from .permissions_data import CATALOGUE, PERMISSION_DETAILS

    out = []
    for cle in cles:
        meta = CATALOGUE.get(cle) or {}
        detail = PERMISSION_DETAILS.get(cle) or {}
        out.append({
            "cle": cle,
            "libelle": meta.get("libelle") or cle,
            "domaine": meta.get("domaine"),
            "risque": meta.get("risque"),
            "portee": meta.get("portee"),
            "description": detail.get("description"),
            "limite": detail.get("limite"),
        })
    return out


def _nom_acteur(acteur_id: Any, role: str) -> str | None:
    if not acteur_id:
        return None
    row = db.fetch_one(
        "SELECT u.email, m.nom_affiche, m.prenoms, m.nom FROM utilisateur u "
        "LEFT JOIN membre m ON m.id = u.membre_id WHERE u.id = %s",
        (str(acteur_id),), role=role,
    )
    if not row:
        return None
    return row.get("nom_affiche") or f"{row.get('prenoms') or ''} {row.get('nom') or ''}".strip() or row.get("email")


@router.get("/groupes/{groupe_id}/fiche")
def fiche_groupe(groupe_id: str, user: Annotated[UserMe, Depends(require_permission("acces.administrer"))]) -> dict[str, Any]:
    """Everything an administrator must read before granting this group."""
    g = _groupe_ou_404(groupe_id, user.role)
    gid = str(g["id"])
    mode = str(g.get("mode") or "role")
    perms = _permissions(gid, user.role) if mode == "permissions" else []
    if mode == "role":
        from .permissions_data import permissions_du_role

        perms = sorted(permissions_du_role(str(g["role_accorde"])))

    membres_actifs = int((db.fetch_one(
        "SELECT COUNT(*) AS n FROM membre_groupe WHERE groupe_id = %s AND actif = true", (gid,), role=user.role,
    ) or {}).get("n") or 0)

    source = None
    if g.get("source_standard_group_id"):
        src = db.fetch_one(
            "SELECT id, cle, libelle, version FROM groupe_acces WHERE id = %s",
            (str(g["source_standard_group_id"]),), role=user.role,
        )
        if src:
            source = {"id": str(src["id"]), "cle": src["cle"], "libelle": src["libelle"], "version": src.get("version")}

    return {
        "identite": {
            "id": gid, "cle": g["cle"], "libelle": g["libelle"], "description": g.get("description"),
            "type": "standard" if g["systeme"] else "personnalise",
            "statut": "actif" if g["actif"] else "desactive",
            "modifiable": not bool(g["systeme"]),
            "application_code": g.get("application_code"),
            "version": g.get("version"),
            "cree_le": g.get("cree_le"), "cree_par": _nom_acteur(g.get("cree_par"), user.role),
            "maj_le": g.get("maj_le"), "maj_par": _nom_acteur(g.get("maj_par"), user.role),
        },
        "finalite": {
            "objectif": g.get("finalite"),
            "usage_recommande": g.get("usage_recommande"),
            "usage_deconseille": g.get("usage_deconseille"),
        },
        "portee": {
            "texte": g.get("portee_texte"),
            "application_code": g.get("application_code"),
            "custom_scope": g.get("custom_scope"),
            "sensibilite": g.get("sensibilite"),
        },
        "permissions": {
            "mode": mode,
            "role_accorde": g["role_accorde"] if mode == "role" else None,
            "accordees": _detail_permissions(perms),
            "total": len(perms),
        },
        "gouvernance": {
            "avertissement_securite": g.get("avertissement_securite"),
            "attribution_requiert": "acces.administrer",
            "modification_requiert": "acces.systeme" if not g["systeme"] else None,
            "duplication_requiert": "acces.systeme",
            "protege": bool(g["systeme"]),
        },
        "membres": {"total": membres_actifs},
        "lignee": {
            "derive_d_un_standard": bool(g.get("derived_from_standard")),
            "source": source,
            "source_version": g.get("source_version"),
            "inheritance_mode": g.get("inheritance_mode"),
        },
    }


@router.get("/groupes/{groupe_id}/membres")
def membres_groupe(
    groupe_id: str,
    user: Annotated[UserMe, Depends(require_permission("acces.administrer"))],
    page: int = Query(default=1, ge=1),
    taille: int = Query(default=10, ge=1, le=100),
    q: str | None = None,
    actif: bool | None = None,
) -> dict[str, Any]:
    """Members of a group, paginated, so the sheet never loads an unbounded list."""
    _groupe_ou_404(groupe_id, user.role)
    conditions = ["mg.groupe_id = %s"]
    params: list[Any] = [groupe_id]
    if actif is not None:
        conditions.append("mg.actif = %s")
        params.append(actif)
    if q and q.strip():
        conditions.append("(m.nom ILIKE %s OR m.prenoms ILIKE %s OR m.matricule ILIKE %s OR m.nom_affiche ILIKE %s)")
        motif = f"%{q.strip()}%"
        params.extend([motif, motif, motif, motif])
    where = " AND ".join(conditions)

    total = int((db.fetch_one(
        f"SELECT COUNT(*) AS n FROM membre_groupe mg JOIN membre m ON m.id = mg.membre_id WHERE {where}",
        tuple(params), role=user.role,
    ) or {}).get("n") or 0)

    rows = db.fetch_all(
        "SELECT mg.id AS appartenance_id, mg.membre_id, mg.portee_type, mg.portee_id, mg.role_interne, "
        "mg.actif, mg.ajoute_le, mg.ajoute_par, mg.retire_le, "
        "m.nom_affiche, m.prenoms, m.nom, m.matricule, m.statut, m.photo_url "
        f"FROM membre_groupe mg JOIN membre m ON m.id = mg.membre_id WHERE {where} "
        "ORDER BY mg.actif DESC, m.nom ASC, m.prenoms ASC LIMIT %s OFFSET %s",
        tuple([*params, taille, (page - 1) * taille]), role=user.role,
    )
    items = [{
        "appartenance_id": str(r["appartenance_id"]),
        "membre_id": str(r["membre_id"]),
        "nom_affiche": r.get("nom_affiche") or f"{r.get('prenoms') or ''} {r.get('nom') or ''}".strip(),
        "matricule": r.get("matricule"),
        "statut_membre": r.get("statut"),
        "photo_url": r.get("photo_url"),
        "role_interne": r.get("role_interne"),
        "portee_type": r.get("portee_type"),
        "portee_id": str(r["portee_id"]) if r.get("portee_id") else None,
        "actif": bool(r.get("actif")),
        "ajoute_le": r.get("ajoute_le"),
        "ajoute_par": _nom_acteur(r.get("ajoute_par"), user.role),
        "retire_le": r.get("retire_le"),
    } for r in rows]
    return {"items": items, "total": total, "page": page, "taille": taille,
            "pages": max(1, (total + taille - 1) // taille)}


@router.get("/groupes/{groupe_id}/historique")
def historique_groupe(
    groupe_id: str,
    user: Annotated[UserMe, Depends(require_permission("acces.administrer"))],
    page: int = Query(default=1, ge=1),
    taille: int = Query(default=10, ge=1, le=100),
) -> dict[str, Any]:
    """What happened to this group, read back from the audit journal.

    The journal is the single source of truth for sensitive actions, so the sheet
    reads it rather than keeping a second history that could drift from it.
    """
    _groupe_ou_404(groupe_id, user.role)
    # Two families of events concern a group: those logged against the group itself
    # (creation, modification, duplication) and the membership movements, which are
    # logged against the MEMBER and carry the group in their details. Both are read
    # so the sheet shows arrivals and departures next to the group's own life.
    filtre = "(objet_type = 'groupe_acces' AND objet_id = %s) OR (details ->> 'groupe_id' = %s)"
    total = int((db.fetch_one(
        f"SELECT COUNT(*) AS n FROM audit WHERE {filtre}",
        (groupe_id, groupe_id), role=user.role,
    ) or {}).get("n") or 0)
    rows = db.fetch_all(
        "SELECT id, action, acteur_id, acteur_role, details, horodatage, objet_type, objet_id FROM audit "
        f"WHERE {filtre} ORDER BY horodatage DESC LIMIT %s OFFSET %s",
        (groupe_id, groupe_id, taille, (page - 1) * taille), role=user.role,
    )
    items = [{
        "id": int(r["id"]),
        "action": r["action"],
        "libelle": _ACTIONS_HISTORIQUE.get(str(r["action"]), str(r["action"]).replace("_", " ")),
        "acteur": _nom_acteur(r.get("acteur_id"), user.role),
        "acteur_role": r.get("acteur_role"),
        "details": r.get("details"),
        "horodatage": r.get("horodatage"),
    } for r in rows]
    return {"items": items, "total": total, "page": page, "taille": taille,
            "pages": max(1, (total + taille - 1) // taille)}


@router.get("/groupes/{groupe_id}/comparaison")
def comparer_au_modele(groupe_id: str, user: Annotated[UserMe, Depends(require_permission("acces.administrer"))]) -> dict[str, Any]:
    """How a derived group drifted from the standard it was copied from."""
    g = _groupe_ou_404(groupe_id, user.role)
    if not g.get("source_standard_group_id"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="ce groupe ne dérive d'aucun groupe standard")
    src = _groupe_ou_404(str(g["source_standard_group_id"]), user.role)

    def _jeu(groupe: dict[str, Any]) -> set[str]:
        gid = str(groupe["id"])
        if str(groupe.get("mode") or "role") == "permissions":
            return set(_permissions(gid, user.role))
        from .permissions_data import permissions_du_role

        return set(permissions_du_role(str(groupe["role_accorde"])))

    actuelles, origine = _jeu(g), _jeu(src)
    ajoutees, retirees = sorted(actuelles - origine), sorted(origine - actuelles)
    identiques = sorted(actuelles & origine)
    return {
        "groupe": {"id": str(g["id"]), "cle": g["cle"], "libelle": g["libelle"]},
        "source": {"id": str(src["id"]), "cle": src["cle"], "libelle": src["libelle"],
                   "version_actuelle": src.get("version"), "version_copiee": g.get("source_version")},
        "modele_a_evolue": src.get("version") != g.get("source_version"),
        "permissions_identiques": _detail_permissions(identiques),
        "permissions_ajoutees": _detail_permissions(ajoutees),
        "permissions_retirees": _detail_permissions(retirees),
        "resume": {"identiques": len(identiques), "ajoutees": len(ajoutees), "retirees": len(retirees)},
        "portee_differente": (g.get("custom_scope") or None) != (src.get("portee_texte") or None),
    }


@router.get("/gouvernance/reconciliation")
def reconciliation_acces(user: Annotated[UserMe, Depends(require_permission("acces.administrer"))]) -> dict[str, Any]:
    """Where the account role and the access groups disagree.

    The platform states that platform access is granted only by putting a member in
    a group, but enforcement starts from the role cached on the account, which can be
    set on its own. Both sources must therefore be comparable, and the gap visible
    rather than assumed away.

    Two very different situations come out of it, and they must not be confused. An
    account bound to a member can be aligned by granting the group that carries its
    role, which takes nothing away since the role is already held. A technical account
    with no member cannot belong to any group at all, membership being keyed on the
    member: it is a recognised category, listed here so it stays under review, never
    something to strip silently.
    """
    rows = db.fetch_all(
        "SELECT u.id, u.email, u.role, u.membre_id, m.nom_affiche, m.prenoms, m.nom, "
        "(SELECT COUNT(*) FROM membre_groupe mg JOIN groupe_acces g ON g.id = mg.groupe_id "
        " WHERE mg.membre_id = u.membre_id AND mg.actif = true AND g.actif = true) AS n_groupes "
        "FROM utilisateur u LEFT JOIN membre m ON m.id = u.membre_id "
        "WHERE u.role <> 'membre' AND u.actif = true ORDER BY u.role, u.email",
        (), role=user.role,
    )
    groupes_par_role = {
        str(g["role_accorde"]): {"id": str(g["id"]), "cle": g["cle"], "libelle": g["libelle"]}
        for g in db.fetch_all(
            "SELECT id, cle, libelle, role_accorde FROM groupe_acces WHERE systeme = true AND mode = 'role'",
            (), role=user.role,
        )
    }

    alignables, techniques, coherents = [], [], 0
    for r in rows:
        if int(r.get("n_groupes") or 0) > 0:
            coherents += 1
            continue
        entree = {
            "utilisateur_id": str(r["id"]), "email": r["email"], "role": r["role"],
            "membre_id": str(r["membre_id"]) if r.get("membre_id") else None,
            "nom": r.get("nom_affiche") or f"{r.get('prenoms') or ''} {r.get('nom') or ''}".strip() or None,
            "groupe_suggere": groupes_par_role.get(str(r["role"])),
        }
        (alignables if r.get("membre_id") else techniques).append(entree)

    return {
        "resume": {
            "comptes_privilegies_actifs": len(rows),
            "adosses_a_un_groupe": coherents,
            "alignables": len(alignables),
            "comptes_techniques_sans_membre": len(techniques),
        },
        "alignables": alignables,
        "comptes_techniques": techniques,
        "explication": (
            "Un compte alignable détient déjà son rôle : lui accorder le groupe correspondant ne lui "
            "ajoute aucun droit, cela rend seulement les deux sources cohérentes. Un compte technique "
            "sans membre ne peut appartenir à aucun groupe ; il reste listé ici pour rester sous revue."
        ),
    }


class DuplicationIn(BaseModel):
    libelle: ShortStr
    description: str = Field(min_length=1)
    application_code: str | None = None
    custom_scope: str | None = None


@router.post("/groupes/{groupe_id}/dupliquer", status_code=status.HTTP_201_CREATED)
def dupliquer_groupe(
    groupe_id: str,
    payload: DuplicationIn,
    user: Annotated[UserMe, Depends(require_permission_ecriture("acces.systeme"))],
) -> dict[str, Any]:
    """Copy a standard group into a custom, editable one that remembers its origin.

    Only a system group is duplicated this way: the point is to start from the
    protected reference. The copy keeps the source permission set, always lands in
    'permissions' mode so it can be adjusted without granting a whole platform role,
    and records which version of the model it was taken from, so a later drift of the
    source stays visible in the comparison view.
    """
    src = _groupe_ou_404(groupe_id, user.role)
    if not src["systeme"]:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="seul un groupe standard se duplique ; un groupe personnalisé se modifie directement")

    base = "".join(c if c.isalnum() else "_" for c in payload.libelle.strip().lower()).strip("_")[:40] or "copie"
    cle = base
    suffixe = 2
    while db.fetch_one("SELECT id FROM groupe_acces WHERE cle = %s", (cle,), role=user.role):
        cle = f"{base}_{suffixe}"[:48]
        suffixe += 1

    if str(src.get("mode") or "role") == "permissions":
        perms = _permissions(str(src["id"]), user.role)
    else:
        from .permissions_data import permissions_du_role

        perms = sorted(permissions_du_role(str(src["role_accorde"])))

    application_code = payload.application_code or src.get("application_code")
    if application_code and not db.fetch_one("SELECT code FROM application WHERE code = %s", (application_code,), role=user.role):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="application inconnue")

    # The group and its permissions are written in ONE transaction. Writing them
    # separately would leave a permission-mode group with no permission behind as soon
    # as a single key is refused, which is exactly what happened while two catalogue
    # keys were missing from the permission table the foreign key points at.
    # A single multi-row insert is used rather than one statement per permission: over
    # the Supabase transaction pooler a repeated statement can collide with a prepared
    # statement left on a reused backend.
    with db.connection(user.role) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO groupe_acces (cle, libelle, description, role_accorde, systeme, mode, application_code, "
            "finalite, usage_recommande, usage_deconseille, portee_texte, sensibilite, avertissement_securite, "
            "source_standard_group_id, derived_from_standard, source_version, inheritance_mode, custom_scope, "
            "cree_par, maj_le, maj_par) "
            "VALUES (%s, %s, %s, 'membre', false, 'permissions', %s, %s, %s, %s, %s, %s, %s, %s, true, %s, 'copie', %s, %s, now(), %s) "
            "RETURNING id",
            (cle, payload.libelle.strip(), payload.description.strip(), application_code,
             src.get("finalite"), src.get("usage_recommande"), src.get("usage_deconseille"),
             src.get("portee_texte"), src.get("sensibilite") or "moyen", src.get("avertissement_securite"),
             str(src["id"]), src.get("version") or 1, payload.custom_scope, user.id, user.id),
        )
        nouveau_id = str(cur.fetchone()["id"])
        if perms:
            valeurs = ", ".join(["(%s, %s)"] * len(perms))
            plat: list[Any] = []
            for p in perms:
                plat.extend([nouveau_id, p])
            cur.execute(
                f"INSERT INTO groupe_permission (groupe_id, permission) VALUES {valeurs} ON CONFLICT DO NOTHING",
                plat,
            )

    audit.log(user.id, user.role, "duplication_groupe_acces", "groupe_acces", nouveau_id,
              {"source_id": str(src["id"]), "source_cle": src["cle"], "source_version": src.get("version"),
               "cle": cle, "permissions_copiees": len(perms)})
    return {"id": nouveau_id, "cle": cle, "libelle": payload.libelle.strip(),
            "permissions_copiees": len(perms), "source": {"id": str(src["id"]), "cle": src["cle"]}}
