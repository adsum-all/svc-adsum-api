"""Cross-cutting collaboration endpoints: notifications, current profile, the
"my cards" and calendar (cards with a due date) views, dashboards and search.

Backs the remaining ``lib/store`` contract. Every query runs under the caller
role (RLS); the visible space set is the admin-sees-all / member-sees-own rule.
Profile identity is managed centrally, so the profile endpoint never mutates the
global member (it would leak into the other apps); it returns the resolved member.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from . import db
from .collaboration_cartes import _CARTE_COLS, LECTEURS, CarteProtoOut, carte_out
from .collaboration_espaces import MembreOut, _initials, _name_from_email, require_espace_role
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
        "WHERE em.espace_id = %s GROUP BY em.utilisateur_id, nom",
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
