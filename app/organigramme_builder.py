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
    # Two grouping blocks under the general steward: "Intendances" and
    # "Coordinations", at the same level. Each holds its units, folded by default,
    # so the chart stays readable and accepts new units without changing shape.
    # Responsibles are NEVER at this level: they hang under a concrete unit below.
    intendances = db.fetch_all("SELECT id, nom, responsable_id FROM intendance WHERE publie = true ORDER BY nom", (), role=role)
    coordinations = db.fetch_all("SELECT id, nom, responsable_id FROM coordination WHERE publie = true ORDER BY nom", (), role=role)
    add_node("bloc_intendances", "Intendances", type_noeud="groupe",
             sous_titre=f"{len(intendances)} intendance{'s' if len(intendances) > 1 else ''}")
    add_link("role:intendant_general", "bloc_intendances", "hierarchique")
    add_node("bloc_coordinations", "Coordinations", type_noeud="groupe",
             sous_titre=f"{len(coordinations)} coordination{'s' if len(coordinations) > 1 else ''}")
    add_link("role:intendant_general", "bloc_coordinations", "hierarchique")
    def _membres_unite(parent_cle: str, unite_type: str, uid: object, eff: int | None) -> None:
        # Every unit is drillable down to a "Membres" leaf showing its real head
        # count, so an admin can open any intendance or coordination and read its size.
        mcle = f"{unite_type}_membres:{uid}"
        add_node(mcle, "Membres", type_noeud="structure", sous_titre=f"{eff or 0} membre{'s' if (eff or 0) > 1 else ''}",
                 unite_type=unite_type, unite_id=uid, effectif=eff)
        add_link(parent_cle, mcle, "hierarchique")

    for it in intendances:
        cle = f"intendance:{it['id']}"
        resp = _membre_par_id(it.get("responsable_id"), role)
        eff = eff_int.get(str(it["id"]))
        add_node(cle, str(it["nom"]), sous_titre=_nom_membre(resp) or "Intendant à désigner", membre_id=resp["id"] if resp else None,
                 fonction_cle="intendant", categorie="fonction", unite_type="intendance", unite_id=it["id"],
                 effectif=eff, statut="actif" if resp else "vacant")
        add_link("bloc_intendances", cle, "hierarchique")
        _membres_unite(cle, "intendance", it["id"], eff)
    for co in coordinations:
        cle = f"coordination:{co['id']}"
        resp = _membre_par_id(co.get("responsable_id"), role)
        eff = eff_coord.get(str(co["id"]))
        add_node(cle, str(co["nom"]), sous_titre=_nom_membre(resp) or "Coordinateur à désigner", membre_id=resp["id"] if resp else None,
                 fonction_cle="coordinateur", categorie="fonction", unite_type="coordination", unite_id=co["id"],
                 effectif=eff, statut="actif" if resp else "vacant")
        add_link("bloc_coordinations", cle, "hierarchique")
        _membres_unite(cle, "coordination", co["id"], eff)

    # 3. Full example branches under ONE intendance and ONE coordination:
    # unit -> "Responsables de commissions et de missions" -> a "Responsable <unit>"
    # per commission -> "Sous-responsables" -> "Membres" (real head count). The
    # commissions carry no unit link in the data, so they are split between the two
    # examples to make both branches complete; the other units stay foldable leaves,
    # ready to receive the same structure.
    commissions = db.fetch_all("SELECT id, nom, responsable_id FROM commission WHERE publie = true ORDER BY nom", (), role=role)
    milieu = (len(commissions) + 1) // 2
    exemples: list[tuple[str, str, list[dict[str, Any]]]] = []
    if intendances:
        exemples.append(("respgrp_int", f"intendance:{intendances[0]['id']}", commissions[:milieu]))
    if coordinations:
        exemples.append(("respgrp_coord", f"coordination:{coordinations[0]['id']}", commissions[milieu:]))
    for grp_cle, parent_cle, lot in exemples:
        if parent_cle not in ids or not lot:
            continue
        add_node(grp_cle, "Responsables de commissions et de missions", type_noeud="groupe",
                 sous_titre=f"{len(lot)} responsable{'s' if len(lot) > 1 else ''}")
        add_link(parent_cle, grp_cle, "hierarchique")
        for cm in lot:
            cle = f"commission:{cm['id']}"
            resp = _membre_par_id(cm.get("responsable_id"), role)
            nom_unite = str(cm["nom"]).replace("Commission ", "").replace("Mission ", "").strip()
            eff = eff_comm.get(str(cm["id"]))
            add_node(cle, f"Responsable {nom_unite}", sous_titre=_nom_membre(resp) or "Poste vacant",
                     membre_id=resp["id"] if resp else None, fonction_cle="responsable", categorie="fonction",
                     unite_type="commission", unite_id=cm["id"], effectif=eff, statut="actif" if resp else "vacant")
            add_link(grp_cle, cle, "hierarchique")
            sr_cle = f"sousresp:{cm['id']}"
            add_node(sr_cle, "Sous-responsables", type_noeud="structure", sous_titre=nom_unite, unite_type="commission", unite_id=cm["id"])
            add_link(cle, sr_cle, "hierarchique")
            m_cle = f"membres:{cm['id']}"
            add_node(m_cle, "Membres", type_noeud="structure", sous_titre=nom_unite, unite_type="commission", unite_id=cm["id"], effectif=eff)
            add_link(sr_cle, m_cle, "hierarchique")

    # A central separator between the main functional chain (left) and the
    # particular branches (right), like the reference diagram. It is a free
    # modeling element the administration can move, duplicate or remove.
    add_node("separateur_central", "Branches particulières reliées", type_noeud="separateur",
             sous_titre="Séparation gouvernance principale / branches particulières")

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
        eff_tr = eff_tribu.get(str(tr["id"]))
        add_node(cle, str(tr["nom"]), sous_titre=sous, membre_id=patr["id"] if patr else None,
                 fonction_cle="patriarche", categorie="fonction_particuliere", unite_type="tribu", unite_id=tr["id"],
                 effectif=eff_tr, statut="actif" if patr or tr.get("patriarche") else "vacant")
        add_link("groupe_patriarches", cle, "responsabilite_tribu")
        # Members of the tribe as a collapsible leaf.
        tm_cle = f"tribu_membres:{tr['id']}"
        add_node(tm_cle, "Membres", type_noeud="structure", sous_titre=str(tr["nom"]), unite_type="tribu", unite_id=tr["id"], effectif=eff_tr)
        add_link(cle, tm_cle, "hierarchique")

    return {"noeuds": len(ids), "chaine": len(_CHAINE)}
