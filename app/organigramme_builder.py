# ruff: noqa: E501 - builder carries long SQL lines
"""Build an organisation-chart draft from the real data.

The chart is never invented: every node comes from an existing function holder or
a published structure (commission, coordination, intendance, tribe) and every link
encodes a real relation. Vacant roles are shown as such rather than hidden, so the
skeleton of the organisation is always readable even before it is fully staffed.

Structure, top to bottom:

- the functional apex chain (founder -> ... -> general steward);
- two grouping blocks "Intendances" and "Coordinations" at the same level;
- under EACH intendance AND EACH coordination, the SAME sub-structure: a
  "Responsables de commissions et de missions" group holding EVERY published
  commission and mission. Each commission shows its responsible, then, only when a
  sub-responsible is actually in post, a "Sous-responsables" level; the members
  hang under the sub-responsibles when they exist, otherwise directly under the
  commission responsible (the member's n+1 is dynamic). A commission with nobody
  still appears, ready to receive members;
- the particular branches: the College of shepherds, the Patriarchs group and the
  twelve tribes with their members.

All nodes carry a client-generated id so the whole draft is written with two batch
inserts (nodes, then links) over a single connection, instead of hundreds of
per-node round trips: at this size (hundreds of commission cells) that is the
difference between a fast build and a serverless timeout.
"""
from __future__ import annotations

import uuid
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


def _label_poste(catalogue: dict[str, dict[str, Any]], cle: str, holder: dict[str, Any] | None = None) -> str:
    """Label of the FUNCTION exercised on a post, agreed with its holder.

    The neutral label is deliberately not used here: for some functions it names the
    unit rather than the office (``coordinateur`` carries the neutral label
    "Coordination"), and writing it on the post would confuse the coordination with
    the coordinator. The gendered label always names the office itself.
    """
    row = catalogue.get(cle)
    if not row:
        return cle.replace("_", " ").title()
    feminin = str((holder or {}).get("genre") or "").lower().startswith("f")
    libelle = row.get("libelle_f") if feminin else row.get("libelle_h")
    return str(libelle or row.get("libelle_h") or row.get("libelle_n") or cle.replace("_", " ").title())


def _titulaire_fonction(cle: str, role: str | None) -> dict[str, Any] | None:
    """First confirmed, active holder of a catalogue function (by principal then order)."""
    return db.fetch_one(
        "SELECT m.id, m.prenoms, m.nom, m.genre, m.nom_affiche AS nom_affiche_calc "
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


def _effectifs_paire(colonne_unite: str, role: str | None) -> dict[tuple[str, str], int]:
    """Active members per (commission, unit) pair, so each commission cell shown under
    a unit carries the real local head count (never a global one)."""
    rows = db.fetch_all(
        f"SELECT commission_id AS cm, {colonne_unite} AS u, count(*) AS n FROM membre "
        f"WHERE statut = 'actif' AND commission_id IS NOT NULL AND {colonne_unite} IS NOT NULL GROUP BY 1, 2",
        (), role=role,
    )
    return {(str(r["cm"]), str(r["u"])): int(r["n"]) for r in rows}


def _membres_par_ids(ids: set[object], role: str | None) -> dict[str, dict[str, Any]]:
    """Resolve every referenced member in a single query, so the build never fires one
    lookup per commission cell (which, at hundreds of cells, would time out)."""
    clean = [i for i in ids if i]
    if not clean:
        return {}
    rows = db.fetch_all(
        "SELECT id, prenoms, nom, genre, nom_affiche AS nom_affiche_calc FROM membre WHERE id = ANY(%s)",
        (clean,), role=role,
    )
    return {str(r["id"]): r for r in rows}


def construire(version_id: str, role: str | None) -> dict[str, int]:
    """Populate a draft version with nodes and links derived from the real data.

    Returns a small summary (counts) for the change log. Idempotent per call: it
    only ever appends to the given draft (callers pass a fresh draft).
    """
    catalogue = {
        str(r["cle"]): r
        for r in db.fetch_all(
            "SELECT cle, libelle_h, libelle_f, libelle_n, categorie, abreviation FROM fonction_honorifique", (), role=role
        )
    }
    eff_int = _effectifs("intendance_id", role)
    eff_coord = _effectifs("coordination_id", role)
    eff_tribu = _effectifs("tribu_id", role)
    eff_comm_int = _effectifs_paire("intendance_id", role)
    eff_comm_coord = _effectifs_paire("coordination_id", role)

    intendances = db.fetch_all("SELECT id, nom, responsable_id FROM intendance WHERE publie = true ORDER BY nom", (), role=role)
    coordinations = db.fetch_all("SELECT id, nom, responsable_id FROM coordination WHERE publie = true ORDER BY nom", (), role=role)
    # Commissions first, then missions; both are the same table split by type.
    commissions = db.fetch_all(
        "SELECT id, nom, responsable_id, type_organisation FROM commission WHERE publie = true "
        "ORDER BY (type_organisation = 'mission'), nom",
        (), role=role,
    )
    tribus = db.fetch_all("SELECT id, nom, patriarche, patriarche_membre_id FROM tribu ORDER BY nom", (), role=role)
    # Occupied sub-responsibles only (a vacant one is not a level: members then hang
    # directly on the commission responsible). Grouped by commission.
    sousresp: dict[str, list[dict[str, Any]]] = {}
    for r in db.fetch_all("SELECT id, nom, commission_id, responsable_id FROM sous_commission WHERE responsable_id IS NOT NULL", (), role=role):
        sousresp.setdefault(str(r["commission_id"]), []).append(r)

    # Resolve every referenced holder once, in a single query.
    ref_ids: set[object] = set()
    for row in (*intendances, *coordinations, *commissions):
        if row.get("responsable_id"):
            ref_ids.add(row["responsable_id"])
    for tr in tribus:
        if tr.get("patriarche_membre_id"):
            ref_ids.add(tr["patriarche_membre_id"])
    for lst in sousresp.values():
        for r in lst:
            if r.get("responsable_id"):
                ref_ids.add(r["responsable_id"])
    membres = _membres_par_ids(ref_ids, role)

    ids: dict[str, str] = {}
    ordre = [0]
    pending_nodes: list[tuple[Any, ...]] = []
    pending_links: list[tuple[str, str, str, str | None]] = []

    def add_node(cle: str, nom: str, **kw: Any) -> str:
        ordre[0] += 10
        nid = str(uuid.uuid4())
        ids[cle] = nid
        pending_nodes.append((
            nid, version_id, cle, kw.get("type_noeud", "structure"), nom, kw.get("sous_titre"),
            kw.get("membre_id"), kw.get("fonction_cle"), kw.get("categorie"),
            kw.get("unite_type"), kw.get("unite_id"), kw.get("effectif"),
            kw.get("statut", "actif"), ordre[0],
        ))
        return nid

    def add_link(src_cle: str, dst_cle: str, type_lien: str, libelle: str | None = None) -> None:
        pending_links.append((src_cle, dst_cle, type_lien, libelle))

    def eff_label(n: int) -> str:
        return f"{n} membre{'s' if n > 1 else ''}"

    # 1. Functional apex chain (person nodes; vacant when no confirmed holder).
    precedent: str | None = None
    for cle, fcle in _CHAINE:
        holder = _titulaire_fonction(fcle, role)
        nom = _nom_membre(holder) or _label_fonction(catalogue, fcle)
        add_node(
            cle, nom, type_noeud="personne" if holder else "structure",
            sous_titre=_label_poste(catalogue, fcle, holder),
            membre_id=holder["id"] if holder else None, fonction_cle=fcle,
            categorie=(catalogue.get(fcle) or {}).get("categorie"),
            statut="actif" if holder else "vacant",
        )
        if precedent:
            add_link(precedent, cle, "hierarchique")
        precedent = cle

    # 2. Two grouping blocks under the general steward: "Intendances" and
    # "Coordinations", at the same level. Each holds its units, folded by default.
    add_node("bloc_intendances", "Intendances", type_noeud="groupe",
             sous_titre=f"{len(intendances)} intendance{'s' if len(intendances) > 1 else ''}")
    add_link("role:intendant_general", "bloc_intendances", "hierarchique")
    add_node("bloc_coordinations", "Coordinations", type_noeud="groupe",
             sous_titre=f"{len(coordinations)} coordination{'s' if len(coordinations) > 1 else ''}")
    add_link("role:intendant_general", "bloc_coordinations", "hierarchique")

    n_comm = sum(1 for c in commissions if c.get("type_organisation") != "mission")
    n_miss = len(commissions) - n_comm
    grp_sous_titre = f"{n_comm} commission{'s' if n_comm > 1 else ''}, {n_miss} mission{'s' if n_miss > 1 else ''}"

    def _sous_unite(unite_cle: str, unite_type: str, unite_id: object, eff_pairs: dict[tuple[str, str], int]) -> None:
        """Full sub-structure under one intendance or coordination: a responsibles
        group holding EVERY commission and mission, each drilled down to its members.
        The members are NEVER a sibling of the responsibles: they live at the bottom of
        each commission, under the sub-responsibles when those exist, else directly
        under the commission responsible."""
        uid = str(unite_id)
        grp_cle = f"respgrp:{unite_type}:{uid}"
        add_node(grp_cle, "Responsables de commissions et de missions", type_noeud="groupe", sous_titre=grp_sous_titre)
        add_link(unite_cle, grp_cle, "hierarchique")
        for cm in commissions:
            cm_id = str(cm["id"])
            resp = membres.get(str(cm.get("responsable_id"))) if cm.get("responsable_id") else None
            nom_unite = str(cm["nom"]).replace("Commission ", "").replace("Mission ", "").strip()
            eff = eff_pairs.get((cm_id, uid), 0)
            c_cle = f"commission:{unite_type}:{uid}:{cm_id}"
            # The unit keeps its own name; the function goes in the subtitle and the
            # holder stays in membre_id. A vacant post is carried by statut alone and
            # never overwrites the name of the unit.
            add_node(c_cle, nom_unite, sous_titre=_label_poste(catalogue, "responsable", resp),
                     membre_id=resp["id"] if resp else None, fonction_cle="responsable", categorie="fonction",
                     unite_type="commission", unite_id=cm["id"], effectif=eff, statut="actif" if resp else "vacant")
            add_link(grp_cle, c_cle, "hierarchique")
            # Dynamic n+1: members hang under the sub-responsibles only when at least
            # one is in post; otherwise they attach straight to the commission head.
            parent_membres = c_cle
            occupied = sousresp.get(cm_id) or []
            if occupied:
                sr_cle = f"sousresp:{unite_type}:{uid}:{cm_id}"
                add_node(sr_cle, "Sous-responsables", type_noeud="structure",
                         sous_titre=f"{len(occupied)} sous-responsable{'s' if len(occupied) > 1 else ''}",
                         unite_type="commission", unite_id=cm["id"])
                add_link(c_cle, sr_cle, "hierarchique")
                parent_membres = sr_cle
            m_cle = f"membres:{unite_type}:{uid}:{cm_id}"
            add_node(m_cle, "Membres", type_noeud="structure", sous_titre=eff_label(eff),
                     unite_type="commission", unite_id=cm["id"], effectif=eff)
            add_link(parent_membres, m_cle, "hierarchique")

    for it in intendances:
        cle = f"intendance:{it['id']}"
        resp = membres.get(str(it.get("responsable_id"))) if it.get("responsable_id") else None
        add_node(cle, str(it["nom"]), sous_titre=_label_poste(catalogue, "intendant", resp), membre_id=resp["id"] if resp else None,
                 fonction_cle="intendant", categorie="fonction", unite_type="intendance", unite_id=it["id"],
                 effectif=eff_int.get(str(it["id"])), statut="actif" if resp else "vacant")
        add_link("bloc_intendances", cle, "hierarchique")
        _sous_unite(cle, "intendance", it["id"], eff_comm_int)
    for co in coordinations:
        cle = f"coordination:{co['id']}"
        resp = membres.get(str(co.get("responsable_id"))) if co.get("responsable_id") else None
        add_node(cle, str(co["nom"]), sous_titre=_label_poste(catalogue, "coordinateur", resp), membre_id=resp["id"] if resp else None,
                 fonction_cle="coordinateur", categorie="fonction", unite_type="coordination", unite_id=co["id"],
                 effectif=eff_coord.get(str(co["id"])), statut="actif" if resp else "vacant")
        add_link("bloc_coordinations", cle, "hierarchique")
        _sous_unite(cle, "coordination", co["id"], eff_comm_coord)

    # A central separator between the main functional chain (left) and the
    # particular branches (right), like the reference diagram.
    add_node("separateur_central", "Branches particulières reliées", type_noeud="separateur",
             sous_titre="Séparation gouvernance principale / branches particulières")

    # 3. Special branch: College of shepherds, coordinated by the mission shepherd.
    n_bergers = int((db.fetch_one("SELECT count(*) AS n FROM membre WHERE est_berger = true AND statut = 'actif'", (), role=role) or {}).get("n") or 0)
    add_node("college_bergers", "Collège des bergers", type_noeud="groupe", sous_titre="Titre : Berger / Bergère",
             unite_type="college", effectif=n_bergers)
    add_link("role:berger_missions", "college_bergers", "coordination", "Coordonne le Collège des bergers")

    # 4. Special branch: Patriarchs group, supervised by the founder and moderator,
    # with the twelve tribes under it (tribal responsibility links).
    add_node("groupe_patriarches", "Groupe des Patriarches", type_noeud="groupe", sous_titre="Fonction particulière",
             unite_type="groupe")
    add_link("role:fondateur", "groupe_patriarches", "supervision", "Supervision directe")
    add_link("role:moderateur", "groupe_patriarches", "supervision", "Coordination directe")
    for tr in tribus:
        cle = f"tribu:{tr['id']}"
        patr = membres.get(str(tr.get("patriarche_membre_id"))) if tr.get("patriarche_membre_id") else None
        eff_tr = eff_tribu.get(str(tr["id"]))
        # tribu.patriarche holds the biblical origin of the tribe ("Aser, fils de
        # Jacob et Zilpa"), not the name of a person: it must never be presented as
        # the holder, and it must not make the post look filled.
        add_node(cle, str(tr["nom"]), sous_titre=_label_poste(catalogue, "patriarche", patr), membre_id=patr["id"] if patr else None,
                 fonction_cle="patriarche", categorie="fonction_particuliere", unite_type="tribu", unite_id=tr["id"],
                 effectif=eff_tr, statut="actif" if patr else "vacant")
        add_link("groupe_patriarches", cle, "responsabilite_tribu")
        tm_cle = f"tribu_membres:{tr['id']}"
        add_node(tm_cle, "Membres", type_noeud="structure", sous_titre=eff_label(eff_tr or 0), unite_type="tribu", unite_id=tr["id"], effectif=eff_tr)
        add_link(cle, tm_cle, "hierarchique")

    # Single-connection flush: all nodes, then all links whose ends both resolved.
    # One multi-row INSERT each (a single, unprepared statement), NOT executemany:
    # over the Supabase transaction pooler a named prepared statement from one request
    # collides with a leftover on a reused backend ("_pg3_0 already exists"), so a
    # one-off statement is the reliable way to write hundreds of rows at once.
    link_rows = [(version_id, ids[s], ids[d], t, lb) for (s, d, t, lb) in pending_links if s in ids and d in ids]

    def _flush(cur: Any, sql_head: str, cols: int, rows: list[tuple[Any, ...]]) -> None:
        if not rows:
            return
        tuple_sql = "(" + ", ".join(["%s"] * cols) + ")"
        values_sql = ", ".join([tuple_sql] * len(rows))
        flat = [value for row in rows for value in row]
        cur.execute(sql_head + values_sql, flat)

    with db.connection(role) as conn, conn.cursor() as cur:
        _flush(
            cur,
            "INSERT INTO organisation_node (id, version_id, cle, type_noeud, nom, sous_titre, membre_id, "
            "fonction_cle, categorie, unite_type, unite_id, effectif, statut, ordre) VALUES ",
            14, pending_nodes,
        )
        _flush(
            cur,
            "INSERT INTO organisation_link (version_id, source_id, cible_id, type_lien, libelle) VALUES ",
            5, link_rows,
        )

    return {"noeuds": len(pending_nodes), "liens": len(link_rows), "chaine": len(_CHAINE)}
