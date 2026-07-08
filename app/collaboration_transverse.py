"""Cross-cutting collaboration endpoints: notifications, current profile, the
"my cards" and calendar (cards with a due date) views, dashboards and search.

Backs the remaining ``lib/store`` contract. Every query runs under the caller
role (RLS); the visible space set is the admin-sees-all / member-sees-own rule.
Profile identity is managed centrally, so the profile endpoint never mutates the
global member (it would leak into the other apps); it returns the resolved member.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from . import activites, audit, db
from .collaboration_cartes import _CARTE_COLS, LECTEURS, CarteProtoOut, carte_out
from .collaboration_espaces import MembreOut, _initials, _name_from_email, require_espace_role
from .fields import LineStr, ShortStr, TitleStr
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/collaboration", tags=["collaboration-transverse"])

ADMINS = ("super_admin", "admin")
_TERMINEE = r"termin|publi|fait"
# Card columns qualified with the c. alias, for queries that join collab_tableau
# (the bare "id" would be ambiguous). Result keys stay the base column names.
_C_CARTE_COLS = ", ".join(f"c.{col.strip()}" for col in _CARTE_COLS.split(","))


class NotificationOut(BaseModel):
    id: str
    membre_id: str
    type: str
    carte_id: str | None = None
    espace_id: str | None = None
    texte: str
    cree_le: str | None = None
    lue: bool


class CarteEcheanceOut(CarteProtoOut):
    espace_id: str | None = None


class StatsGlobalesOut(BaseModel):
    espaces: int
    tableaux: int
    cartes: int
    enRetard: int
    termineesSemaine: int


class EtiquetteStat(BaseModel):
    id: str
    nom: str
    couleur: str
    count: int


class AssigneStat(BaseModel):
    id: str
    nom: str
    count: int


class StatsEspaceOut(BaseModel):
    tableaux: int
    cartes: int
    parPriorite: dict[str, int]
    parEtiquette: list[EtiquetteStat]
    parAssigne: list[AssigneStat]


class ResultatRecherche(BaseModel):
    kind: str
    id: str
    titre: str
    sous_titre: str
    espace_id: str | None = None
    tableau_id: str | None = None


def _visible_espace_ids(user: UserMe) -> list[str]:
    if user.role in ADMINS:
        rows = db.fetch_all("SELECT id FROM collab_espace WHERE NOT archive", (), role=user.role)
    else:
        rows = db.fetch_all(
            "SELECT e.id FROM collab_espace e JOIN collab_espace_membre m ON m.espace_id = e.id "
            "WHERE m.utilisateur_id = %s AND NOT e.archive",
            (user.id,),
            role=user.role,
        )
    return [str(r["id"]) for r in rows]


@router.get("/notifications", response_model=list[NotificationOut])
def list_notifications(
    user: Annotated[UserMe, Depends(require_permission("collaboration.superviser"))]
) -> list[NotificationOut]:
    rows = db.fetch_all(
        "SELECT id, utilisateur_id, type, carte_id, espace_id, texte, cree_le, lue "
        "FROM collab_notification WHERE utilisateur_id = %s ORDER BY cree_le DESC LIMIT 200",
        (user.id,),
        role=user.role,
    )
    return [
        NotificationOut(
            id=str(r["id"]),
            membre_id=str(r["utilisateur_id"]),
            type=r["type"],
            carte_id=str(r["carte_id"]) if r["carte_id"] else None,
            espace_id=str(r["espace_id"]) if r["espace_id"] else None,
            texte=r["texte"],
            cree_le=r["cree_le"].isoformat() if r["cree_le"] else None,
            lue=bool(r["lue"]),
        )
        for r in rows
    ]


@router.post("/notifications/{notif_id}/lue", status_code=204)
def marquer_lue(
    notif_id: str, user: Annotated[UserMe, Depends(require_permission("collaboration.superviser"))]
) -> None:
    db.execute(
        "UPDATE collab_notification SET lue = true WHERE id = %s AND utilisateur_id = %s",
        (notif_id, user.id),
        role=user.role,
    )


@router.post("/notifications/toutes-lues", status_code=204)
def marquer_toutes_lues(
    user: Annotated[UserMe, Depends(require_permission("collaboration.superviser"))]
) -> None:
    db.execute(
        "UPDATE collab_notification SET lue = true WHERE utilisateur_id = %s AND NOT lue", (user.id,), role=user.role
    )


@router.get("/moi", response_model=MembreOut)
def moi(user: Annotated[UserMe, Depends(require_permission("collaboration.superviser"))]) -> MembreOut:
    # The signed-in member's real identity. Identity is managed centrally
    # (utilisateur / membre, shared with the other apps and governed by the civil
    # identity rules), so collaboration reads it here (real nom_affiche) and never
    # edits it: display-name changes belong to the back office.
    row = db.fetch_one(
        "SELECT u.id, u.email, m.nom_affiche FROM utilisateur u LEFT JOIN membre m ON m.id = u.membre_id "
        "WHERE u.id = %s",
        (user.id,),
        role=user.role,
    )
    email = row["email"] if row else user.email
    nom = (row["nom_affiche"] if row and row["nom_affiche"] else None) or _name_from_email(email)
    return MembreOut(id=user.id, nom=nom, courriel=email, initiales=_initials(nom))


@router.get("/mes-cartes", response_model=list[CarteEcheanceOut])
def mes_cartes(
    user: Annotated[UserMe, Depends(require_permission("collaboration.superviser"))]
) -> list[CarteEcheanceOut]:
    espaces = _visible_espace_ids(user)
    if not espaces:
        return []
    rows = db.fetch_all(
        f"SELECT {_C_CARTE_COLS}, t.espace_id AS _espace "
        "FROM collab_carte c JOIN collab_tableau t ON t.id = c.tableau_id "
        "JOIN collab_carte_membre cm ON cm.carte_id = c.id "
        "WHERE cm.utilisateur_id = %s AND NOT c.archive AND t.espace_id = ANY(%s) "
        "ORDER BY c.echeance NULLS LAST",
        (user.id, espaces),
        role=user.role,
    )
    return [_carte_echeance(r, user.role) for r in rows]


@router.get("/cartes-echeance", response_model=list[CarteEcheanceOut])
def cartes_echeance(
    user: Annotated[UserMe, Depends(require_permission("collaboration.superviser"))]
) -> list[CarteEcheanceOut]:
    espaces = _visible_espace_ids(user)
    if not espaces:
        return []
    rows = db.fetch_all(
        f"SELECT {_C_CARTE_COLS}, t.espace_id AS _espace "
        "FROM collab_carte c JOIN collab_tableau t ON t.id = c.tableau_id "
        "WHERE c.echeance IS NOT NULL AND NOT c.archive AND t.espace_id = ANY(%s) "
        "ORDER BY c.echeance",
        (espaces,),
        role=user.role,
    )
    return [_carte_echeance(r, user.role) for r in rows]


def _carte_echeance(row: dict[str, Any], role: str) -> CarteEcheanceOut:
    base = carte_out(row, role)
    return CarteEcheanceOut(**base.model_dump(), espace_id=str(row["_espace"]) if row.get("_espace") else None)


class ActivitePublieeOut(BaseModel):
    """A real activity published from a collaboration card: the evenement row, so
    the collaboration calendar shows the same activities as the member agenda."""

    id: str
    carte_id: str | None = None
    tableau_id: str | None = None
    espace_id: str | None = None
    titre: str
    type: str | None = None
    cible_type: str | None = None
    debut: str | None = None
    lieu: str | None = None


@router.get("/activites", response_model=list[ActivitePublieeOut])
def activites_publiees(
    user: Annotated[UserMe, Depends(require_permission("collaboration.superviser"))]
) -> list[ActivitePublieeOut]:
    """Every programmed activity from the shared ``evenement`` table (non-cancelled),
    so the collaboration calendar shows the SAME activities as the back office and
    the member agenda: same table, same source of truth, the difference between apps
    being only the filters each one applies. An activity that was published from a
    collaboration card also carries its card link (so it can be opened); the others
    are shown read-only. No space scoping here: the whole fraternity's programme."""
    rows = db.fetch_all(
        "SELECT e.id, e.titre, e.type, e.cible_type, e.debut, e.lieu, "
        "c.id AS carte_id, c.tableau_id, t.espace_id "
        "FROM evenement e "
        "LEFT JOIN collab_carte c ON c.evenement_id = e.id "
        "LEFT JOIN collab_tableau t ON t.id = c.tableau_id "
        "WHERE NOT e.annule "
        "ORDER BY e.debut",
        (),
        role=user.role,
    )
    out: list[ActivitePublieeOut] = []
    for r in rows:
        debut = r["debut"]
        out.append(
            ActivitePublieeOut(
                id=str(r["id"]),
                carte_id=str(r["carte_id"]) if r.get("carte_id") else None,
                tableau_id=str(r["tableau_id"]) if r.get("tableau_id") else None,
                espace_id=str(r["espace_id"]) if r.get("espace_id") else None,
                titre=r["titre"],
                type=r.get("type"),
                cible_type=r.get("cible_type"),
                debut=debut.isoformat() if hasattr(debut, "isoformat") else (str(debut) if debut else None),
                lieu=r.get("lieu"),
            )
        )
    return out


class ActiviteIn(BaseModel):
    """Create a real activity from the collaboration app. Same engine and same
    evenement row as the back office and pilotage, so it appears in every calendar."""

    titre: TitleStr
    type: ShortStr | None = None
    debut: str
    fin: str | None = None
    lieu: LineStr | None = None
    cible_type: ShortStr = "general"
    cible_id: ShortStr | None = None
    visibilite: ShortStr = "membres"


@router.post("/activites", status_code=status.HTTP_201_CREATED)
def creer_activite(
    payload: ActiviteIn,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> dict[str, str]:
    """Program an activity directly from the collaboration app, through the shared
    activity engine (same evenement row as the back office and pilotage), so it is
    immediately visible in every aligned calendar."""
    evenement_id = activites.inserer_evenement(
        titre=payload.titre, type_=payload.type, debut=payload.debut, fin=payload.fin,
        lieu=payload.lieu, cible_type=payload.cible_type, cible_id=payload.cible_id,
        visibilite=payload.visibilite, cree_par=user.id, role=user.role,
    )
    audit.log(user.id, user.role, "creation_activite_collaboration", "evenement", evenement_id,
              {"cible_type": payload.cible_type})
    return {"id": evenement_id}


class ActiviteDetail(BaseModel):
    id: str
    titre: str
    type: str | None = None
    debut: str | None = None
    fin: str | None = None
    lieu: str | None = None
    cible_type: str | None = None
    cible_id: str | None = None
    visibilite: str | None = None
    annule: bool = False


@router.get("/activites/{evenement_id}", response_model=ActiviteDetail)
def detail_activite(
    evenement_id: str,
    user: Annotated[UserMe, Depends(require_permission("collaboration.superviser"))],
) -> ActiviteDetail:
    """One activity's editable fields, from the shared evenement table."""
    r = db.fetch_one(
        "SELECT id, titre, type, debut, fin, lieu, cible_type, cible_id, visibilite, annule "
        "FROM evenement WHERE id = %s",
        (evenement_id,),
        role=user.role,
    )
    if not r:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="activity not found")
    return ActiviteDetail(
        id=str(r["id"]), titre=r["titre"], type=r.get("type"),
        debut=r["debut"].isoformat() if hasattr(r["debut"], "isoformat") else (str(r["debut"]) if r["debut"] else None),
        fin=r["fin"].isoformat() if hasattr(r["fin"], "isoformat") else (str(r["fin"]) if r["fin"] else None),
        lieu=r.get("lieu"), cible_type=r.get("cible_type"),
        cible_id=str(r["cible_id"]) if r.get("cible_id") else None,
        visibilite=r.get("visibilite"), annule=bool(r.get("annule")),
    )


class ActivitePatch(BaseModel):
    titre: TitleStr | None = None
    type: ShortStr | None = None
    debut: str | None = None
    fin: str | None = None
    lieu: LineStr | None = None
    cible_type: ShortStr | None = None
    cible_id: ShortStr | None = None
    visibilite: ShortStr | None = None


@router.patch("/activites/{evenement_id}")
def modifier_activite(
    evenement_id: str,
    payload: ActivitePatch,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> dict[str, str]:
    """Edit an activity from the collaboration app, whatever app created it (parity
    with the back office). The same evenement row is updated, so the change shows in
    every aligned calendar. Targeting is validated by the shared engine."""
    fields = payload.model_dump(exclude_unset=True)
    if "cible_type" in fields:
        stored = activites.valider_cible(str(fields["cible_type"]), fields.get("cible_id"), user.role)
        fields["cible_id"] = stored
    if not fields:
        return {"id": evenement_id}
    cols = {"titre", "type", "debut", "fin", "lieu", "cible_type", "cible_id", "visibilite"}
    fields = {k: v for k, v in fields.items() if k in cols}
    sets = ", ".join(f"{k} = %s" for k in fields)
    updated = db.execute(
        f"UPDATE evenement SET {sets} WHERE id = %s RETURNING id",
        (*fields.values(), evenement_id),
        role=user.role,
    )
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="activity not found")
    audit.log(user.id, user.role, "modification_activite_collaboration", "evenement", evenement_id,
              {"champs": list(fields)})
    return {"id": str(updated["id"])}


@router.post("/activites/{evenement_id}/annuler")
def annuler_activite(
    evenement_id: str,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> dict[str, str]:
    """Cancel an activity from the collaboration app (parity with the back office):
    it is marked cancelled and leaves every calendar, but its history is kept."""
    updated = db.execute(
        "UPDATE evenement SET annule = true, annule_le = now() WHERE id = %s RETURNING id",
        (evenement_id,),
        role=user.role,
    )
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="activity not found")
    audit.log(user.id, user.role, "annulation_activite_collaboration", "evenement", evenement_id, {})
    return {"id": str(updated["id"])}


@router.get("/stats", response_model=StatsGlobalesOut)
def stats_globales(
    user: Annotated[UserMe, Depends(require_permission("collaboration.superviser"))]
) -> StatsGlobalesOut:
    espaces = _visible_espace_ids(user)
    if not espaces:
        return StatsGlobalesOut(espaces=0, tableaux=0, cartes=0, enRetard=0, termineesSemaine=0)
    tabs = db.fetch_one(
        "SELECT count(*) AS n FROM collab_tableau WHERE espace_id = ANY(%s) AND NOT archive", (espaces,), role=user.role
    )
    cartes = db.fetch_one(
        "SELECT count(*) AS n FROM collab_carte c JOIN collab_tableau t ON t.id = c.tableau_id "
        "WHERE t.espace_id = ANY(%s) AND NOT c.archive AND NOT t.archive",
        (espaces,),
        role=user.role,
    )
    retard = db.fetch_one(
        "SELECT count(*) AS n FROM collab_carte c JOIN collab_tableau t ON t.id = c.tableau_id "
        "WHERE t.espace_id = ANY(%s) AND NOT c.archive AND c.echeance IS NOT NULL AND c.echeance < now()",
        (espaces,),
        role=user.role,
    )
    # "done this week" is best effort: cards in a terminal-looking column touched in
    # the last 7 days (there is no first-class done flag yet).
    terminees = db.fetch_one(
        "SELECT count(*) AS n FROM collab_carte c JOIN collab_tableau t ON t.id = c.tableau_id "
        "JOIN collab_colonne col ON col.id = c.colonne_id "
        "WHERE t.espace_id = ANY(%s) AND NOT c.archive AND col.nom ~* %s AND c.maj_le >= now() - interval '7 days'",
        (espaces, _TERMINEE),
        role=user.role,
    )
    return StatsGlobalesOut(
        espaces=len(espaces),
        tableaux=int(tabs["n"]) if tabs else 0,
        cartes=int(cartes["n"]) if cartes else 0,
        enRetard=int(retard["n"]) if retard else 0,
        termineesSemaine=int(terminees["n"]) if terminees else 0,
    )


@router.get("/espaces/{espace_id}/stats", response_model=StatsEspaceOut)
def stats_espace(
    espace_id: str, user: Annotated[UserMe, Depends(require_permission("collaboration.superviser"))]
) -> StatsEspaceOut:
    # Same space ACL as the space detail: only a member (or an admin, treated as a
    # synthetic owner) may read a space's stats, never any space by id.
    require_espace_role(espace_id, user, LECTEURS)
    tabs = db.fetch_one(
        "SELECT count(*) AS n FROM collab_tableau WHERE espace_id = %s AND NOT archive", (espace_id,), role=user.role
    )
    priorites = db.fetch_all(
        "SELECT c.priorite, count(*) AS n FROM collab_carte c JOIN collab_tableau t ON t.id = c.tableau_id "
        "WHERE t.espace_id = %s AND NOT c.archive GROUP BY c.priorite",
        (espace_id,),
        role=user.role,
    )
    par_priorite = {"urgente": 0, "haute": 0, "normale": 0, "basse": 0}
    total_cartes = 0
    for p in priorites:
        par_priorite[p["priorite"]] = int(p["n"])
        total_cartes += int(p["n"])
    etiq = db.fetch_all(
        "SELECT e.id, e.nom, e.couleur, count(ce.carte_id) AS n FROM collab_etiquette e "
        "LEFT JOIN collab_carte_etiquette ce ON ce.etiquette_id = e.id "
        "LEFT JOIN collab_carte c ON c.id = ce.carte_id AND NOT c.archive "
        "WHERE e.espace_id = %s GROUP BY e.id, e.nom, e.couleur ORDER BY e.position",
        (espace_id,),
        role=user.role,
    )
    assignes = db.fetch_all(
        "SELECT em.utilisateur_id, coalesce(m.nom_affiche, u.email) AS nom, "
        "count(DISTINCT cm.carte_id) AS n FROM collab_espace_membre em "
        "JOIN utilisateur u ON u.id = em.utilisateur_id LEFT JOIN membre m ON m.id = u.membre_id "
        "LEFT JOIN collab_carte_membre cm ON cm.utilisateur_id = em.utilisateur_id AND cm.role = 'assigne' "
        "LEFT JOIN collab_carte c ON c.id = cm.carte_id AND NOT c.archive "
        "LEFT JOIN collab_tableau t ON t.id = c.tableau_id AND t.espace_id = %s "
        "WHERE em.espace_id = %s GROUP BY em.utilisateur_id, m.nom_affiche, u.email",
        (espace_id, espace_id),
        role=user.role,
    )
    return StatsEspaceOut(
        tableaux=int(tabs["n"]) if tabs else 0,
        cartes=total_cartes,
        parPriorite=par_priorite,
        parEtiquette=[
            EtiquetteStat(id=str(e["id"]), nom=e["nom"], couleur=e["couleur"], count=int(e["n"])) for e in etiq
        ],
        parAssigne=[
            AssigneStat(id=str(a["utilisateur_id"]), nom=a["nom"], count=int(a["n"])) for a in assignes
        ],
    )


@router.get("/recherche", response_model=list[ResultatRecherche])
def recherche(
    q: str, user: Annotated[UserMe, Depends(require_permission("collaboration.superviser"))]
) -> list[ResultatRecherche]:
    query = q.strip()
    if not query:
        return []
    espaces = _visible_espace_ids(user)
    if not espaces:
        return []
    like = f"%{query.lower()}%"
    out: list[ResultatRecherche] = []
    esp_rows = db.fetch_all(
        "SELECT id, nom, description FROM collab_espace WHERE id = ANY(%s) AND "
        "(lower(nom) LIKE %s OR lower(description) LIKE %s) LIMIT 15",
        (espaces, like, like),
        role=user.role,
    )
    for e in esp_rows:
        out.append(
            ResultatRecherche(kind="espace", id=str(e["id"]), titre=e["nom"], sous_titre=e["description"] or "Espace")
        )
    tab_rows = db.fetch_all(
        "SELECT t.id, t.nom, t.espace_id, e.nom AS espace_nom FROM collab_tableau t "
        "JOIN collab_espace e ON e.id = t.espace_id "
        "WHERE t.espace_id = ANY(%s) AND NOT t.archive AND (lower(t.nom) LIKE %s OR lower(t.description) LIKE %s) "
        "LIMIT 15",
        (espaces, like, like),
        role=user.role,
    )
    for t in tab_rows:
        out.append(
            ResultatRecherche(
                kind="tableau",
                id=str(t["id"]),
                espace_id=str(t["espace_id"]),
                titre=t["nom"],
                sous_titre=t["espace_nom"],
            )
        )
    carte_rows = db.fetch_all(
        "SELECT c.id, c.titre, c.tableau_id, t.espace_id, t.nom AS tab_nom, e.nom AS esp_nom "
        "FROM collab_carte c JOIN collab_tableau t ON t.id = c.tableau_id "
        "JOIN collab_espace e ON e.id = t.espace_id "
        "WHERE t.espace_id = ANY(%s) AND NOT c.archive AND (lower(c.titre) LIKE %s OR lower(c.description) LIKE %s) "
        "LIMIT 20",
        (espaces, like, like),
        role=user.role,
    )
    for c in carte_rows:
        out.append(
            ResultatRecherche(
                kind="carte",
                id=str(c["id"]),
                tableau_id=str(c["tableau_id"]),
                espace_id=str(c["espace_id"]),
                titre=c["titre"],
                sous_titre=f"{c['tab_nom']} - {c['esp_nom']}",
            )
        )
    return out
