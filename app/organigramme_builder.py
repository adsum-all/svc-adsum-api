# ruff: noqa: E501 - builder carries long SQL lines
"""Build an organisation-chart draft from the real data.

The chart is never invented: every node comes from an existing function holder or
a published structure (commission, coordination, intendance, tribe) and every link
encodes a real relation. Vacant roles are shown as such rather than hidden, so the
skeleton of the organisation is always readable even before it is fully staffed.

The result is written into an existing draft ``organisation_version`` as nodes and
typed links, ready to be edited, validated and published.
"""
from __future__ import annotations

from typing import Any

from . import db

# Apex of the functional chain, top to bottom. Each is a single node; its holder,
# if any, is the first confirmed member of that catalogue function.
_CHAINE = [
    ("role:fondateur", "fondateur"),
    ("role:moderateur", "moderateur"),
    ("role:berger_missions", "berger_missions"),
    ("role:controleur_general", "controleur_general"),
    ("role:intendant_general", "intendant_general"),
]


def _nom_membre(row: dict[str, Any] | None) -> str | None:
    if not row:
        return None
    aff = row.get("nom_affiche_calc")
    if aff:
        return str(aff)
    parts = [str(row.get("prenoms") or "").strip(), str(row.get("nom") or "").strip()]
    nom = " ".join(p for p in parts if p).strip()
    return nom or None


def _label_fonction(catalogue: dict[str, dict[str, Any]], cle: str) -> str:
    row = catalogue.get(cle)
    return str(row["libelle_n"]) if row and row.get("libelle_n") else cle.replace("_", " ").title()


def _titulaire_fonction(cle: str, role: str | None) -> dict[str, Any] | None:
    """First confirmed, active holder of a catalogue function (by principal then order)."""
    return db.fetch_one(
        "SELECT m.id, m.prenoms, m.nom, m.nom_affiche AS nom_affiche_calc "
        "FROM membre_fonction mf JOIN membre m ON m.id = mf.membre_id "
        "WHERE lower(mf.fonction_cle) = %s AND mf.actif = true AND mf.confirmee = true "
        "ORDER BY mf.principale DESC, mf.ordre ASC, mf.cree_le ASC LIMIT 1",
        (cle.lower(),), role=role,
    )


def _membre_par_id(membre_id: object, role: str | None) -> dict[str, Any] | None:
    if not membre_id:
        return None
    return db.fetch_one(
        "SELECT id, prenoms, nom, nom_affiche AS nom_affiche_calc FROM membre WHERE id = %s",
        (membre_id,), role=role,
    )


def _effectifs(colonne: str, role: str | None) -> dict[str, int]:
    rows = db.fetch_all(
        f"SELECT {colonne} AS uid, count(*) AS n FROM membre WHERE {colonne} IS NOT NULL AND statut = 'actif' GROUP BY {colonne}",
        (), role=role,
    )
    return {str(r["uid"]): int(r["n"]) for r in rows}


def construire(version_id: str, role: str | None) -> dict[str, int]:
    """Populate a draft version with nodes and links derived from the real data.

    Returns a small summary (counts) for the change log. Idempotent per call: it
    only ever appends to the given draft (callers pass a fresh draft).
    """
    catalogue = {
        str(r["cle"]): r
        for r in db.fetch_all(
            "SELECT cle, libelle_n, categorie, abreviation FROM fonction_honorifique", (), role=role
        )
    }
    eff_comm = _effectifs("commission_id", role)
    eff_coord = _effectifs("coordination_id", role)
    eff_int = _effectifs("intendance_id", role)
    eff_tribu = _effectifs("tribu_id", role)

    ids: dict[str, str] = {}
    ordre = [0]

    def add_node(cle: str, nom: str, **kw: Any) -> str:
        ordre[0] += 10
        row = db.execute(
            "INSERT INTO organisation_node (version_id, cle, type_noeud, nom, sous_titre, membre_id, "
            "fonction_cle, categorie, unite_type, unite_id, effectif, statut, ordre) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                version_id, cle, kw.get("type_noeud", "structure"), nom, kw.get("sous_titre"),
                kw.get("membre_id"), kw.get("fonction_cle"), kw.get("categorie"),
                kw.get("unite_type"), kw.get("unite_id"), kw.get("effectif"),
                kw.get("statut", "actif"), ordre[0],
            ),
            role=role,
        )
        ids[cle] = str(row["id"])
        return ids[cle]

    def add_link(src_cle: str, dst_cle: str, type_lien: str, libelle: str | None = None) -> None:
        if src_cle not in ids or dst_cle not in ids:
            return
        db.execute(
            "INSERT INTO organisation_link (version_id, source_id, cible_id, type_lien, libelle) VALUES (%s, %s, %s, %s, %s)",
            (version_id, ids[src_cle], ids[dst_cle], type_lien, libelle), role=role,
        )

    # 1. Functional apex chain (person nodes; vacant when no confirmed holder).
    precedent: str | None = None
    for cle, fcle in _CHAINE:
        holder = _titulaire_fonction(fcle, role)
        nom = _nom_membre(holder) or _label_fonction(catalogue, fcle)
        add_node(
            cle, nom, type_noeud="personne" if holder else "structure",
            sous_titre=_label_fonction(catalogue, fcle),
            membre_id=holder["id"] if holder else None, fonction_cle=fcle,
            categorie=(catalogue.get(fcle) or {}).get("categorie"),
            statut="actif" if holder else "vacant",
        )
        if precedent:
            add_link(precedent, cle, "hierarchique")
        precedent = cle

    # 2. Coordinations then intendances (published only), under the general steward.
    for co in db.fetch_all("SELECT id, nom, responsable_id FROM coordination WHERE publie = true ORDER BY nom", (), role=role):
        cle = f"coordination:{co['id']}"
        resp = _membre_par_id(co.get("responsable_id"), role)
        add_node(cle, str(co["nom"]), sous_titre=_nom_membre(resp) or "Coordination", membre_id=resp["id"] if resp else None,
                 fonction_cle="coordinateur", categorie="fonction", unite_type="coordination", unite_id=co["id"],
                 effectif=eff_coord.get(str(co["id"])), statut="actif" if resp else "vacant")
        add_link("role:intendant_general", cle, "hierarchique")
    for it in db.fetch_all("SELECT id, nom, responsable_id, coordination_id FROM intendance WHERE publie = true ORDER BY nom", (), role=role):
        cle = f"intendance:{it['id']}"
        resp = _membre_par_id(it.get("responsable_id"), role)
        add_node(cle, str(it["nom"]), sous_titre=_nom_membre(resp) or "Intendance", membre_id=resp["id"] if resp else None,
                 fonction_cle="intendant", categorie="fonction", unite_type="intendance", unite_id=it["id"],
                 effectif=eff_int.get(str(it["id"])), statut="actif" if resp else "vacant")
        parent = f"coordination:{it['coordination_id']}" if it.get("coordination_id") and f"coordination:{it['coordination_id']}" in ids else "role:intendant_general"
        add_link(parent, cle, "hierarchique")

    # 3. Commissions and missions (published), each with its responsible, under the
    # general steward. The member count is shown; sub-responsibles and members are
    # summarised by that count rather than exploded into thousands of nodes.
    for cm in db.fetch_all("SELECT id, nom, responsable_id, type_organisation FROM commission WHERE publie = true ORDER BY nom", (), role=role):
        cle = f"commission:{cm['id']}"
        resp = _membre_par_id(cm.get("responsable_id"), role)
        add_node(cle, str(cm["nom"]), sous_titre=_nom_membre(resp) or ("Mission" if cm.get("type_organisation") == "mission" else "Commission"),
                 membre_id=resp["id"] if resp else None, fonction_cle="responsable", categorie="fonction",
                 unite_type="commission", unite_id=cm["id"], effectif=eff_comm.get(str(cm["id"])),
                 statut="actif" if resp else "vacant")
        add_link("role:intendant_general", cle, "hierarchique")

    # 4. Special branch: College of shepherds, coordinated by the mission shepherd.
    n_bergers = int((db.fetch_one("SELECT count(*) AS n FROM membre WHERE est_berger = true AND statut = 'actif'", (), role=role) or {}).get("n") or 0)
    add_node("college_bergers", "Collège des bergers", type_noeud="groupe", sous_titre="Titre : Berger / Bergère",
             unite_type="college", effectif=n_bergers)
    add_link("role:berger_missions", "college_bergers", "coordination", "Coordonne le Collège des bergers")

    # 5. Special branch: Patriarchs group, supervised by the founder and moderator,
    # with the twelve tribes under it (tribal responsibility links).
    add_node("groupe_patriarches", "Groupe des Patriarches", type_noeud="groupe", sous_titre="Fonction particulière",
             unite_type="groupe")
    add_link("role:fondateur", "groupe_patriarches", "supervision", "Supervision directe")
    add_link("role:moderateur", "groupe_patriarches", "supervision", "Coordination directe")
    for tr in db.fetch_all("SELECT id, nom, patriarche, patriarche_membre_id FROM tribu ORDER BY nom", (), role=role):
        cle = f"tribu:{tr['id']}"
        patr = _membre_par_id(tr.get("patriarche_membre_id"), role)
        sous = _nom_membre(patr) or (str(tr["patriarche"]) if tr.get("patriarche") else "Patriarche à désigner")
        add_node(cle, str(tr["nom"]), sous_titre=sous, membre_id=patr["id"] if patr else None,
                 fonction_cle="patriarche", categorie="fonction_particuliere", unite_type="tribu", unite_id=tr["id"],
                 effectif=eff_tribu.get(str(tr["id"])), statut="actif" if patr or tr.get("patriarche") else "vacant")
        add_link("groupe_patriarches", cle, "responsabilite_tribu")

    return {"noeuds": len(ids), "chaine": len(_CHAINE)}
