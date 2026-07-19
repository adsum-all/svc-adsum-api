"""Function-based hierarchy of the connected member (feeds GET /membres/me/hierarchie).

Rebuilt from a single-rattachement + fixed apex list into a faithful view: every active
function (multi-role), a personalised upward chain N+1..N from the member's real units
then the apex, titles and particular links kept apart from the functional chain, and the
units the member belongs to. Vacant posts stay in the chain as "Poste a pourvoir".
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import Any

from . import db, organigramme_builder

# Apex chain, function-based, from the highest operational level up to the founder.
_APEX = ("controleur_general", "intendant_general", "berger_missions", "moderateur", "fondateur")
# Display precedence between categories (lower wins), so the primary position is the
# member's highest-authority active function.
_CAT_RANG = {"fonction_speciale": 0, "titre": 1, "fonction": 2, "fonction_particuliere": 3}


def _hnom(row: dict[str, Any] | None) -> str | None:
    if not row:
        return None
    aff = row.get("nom_affiche")
    if aff:
        return str(aff)
    nom = f"{row.get('prenoms') or ''} {row.get('nom') or ''}".strip()
    return nom or None


def _hlabel(cat: dict[str, Any] | None, fcle: str, genre: object) -> str:
    """Gendered label of a catalogue function (falls back to the epicene label)."""
    if not cat:
        return fcle.replace("_", " ").capitalize()
    fem = str(genre or "").lower().startswith("f")
    lib = (cat.get("libelle_f") if fem else cat.get("libelle_h")) or cat.get("libelle_n") or cat.get("libelle_h") or cat.get("libelle_f")
    return str(lib or fcle.replace("_", " ").capitalize())


def _occupants_unite(row: dict[str, Any] | None, fcle: str, role: str | None, moi: object) -> tuple[list[str], bool, bool]:
    """Occupants of a unit level: its structural responsible plus anyone holding the
    matching function on that unit (co-responsables), excluding the caller. Returns
    (names, est_moi, vacant): vacant is true only when the post has no holder at all."""
    if not row:
        return [], False, True
    ids: list[object] = []
    if row.get("responsable_id"):
        ids.append(row["responsable_id"])
    nom_unite = row.get("nom")
    if nom_unite:
        extra = db.fetch_all(
            "SELECT mf.membre_id FROM membre_fonction mf "
            "WHERE mf.actif = true AND mf.confirmee = true AND lower(mf.fonction_cle) = %s "
            "AND mf.perimetre IS NOT NULL AND lower(mf.perimetre) LIKE %s",
            (fcle, f"%{str(nom_unite).lower()}%"), role=role,
        )
        ids.extend(r["membre_id"] for r in extra)
    uniq: list[object] = []
    seen: set[str] = set()
    est_moi = False
    for i in ids:
        s = str(i)
        if s in seen:
            continue
        seen.add(s)
        if moi and s == str(moi):
            est_moi = True
            continue
        uniq.append(i)
    vacant = not ids
    if not uniq:
        return [], est_moi, vacant
    rows = db.fetch_all("SELECT id, prenoms, nom, nom_affiche FROM membre WHERE id = ANY(%s)", (uniq,), role=role)
    by = {str(r["id"]): r for r in rows}
    noms = [n for i in uniq if (n := _hnom(by.get(str(i))))]
    return noms, est_moi, vacant


def calculer(membre_id: str, role: str | None) -> dict[str, Any]:
    m = db.fetch_one(
        "SELECT id, prenoms, nom, nom_affiche, genre, est_berger, nom_pastoral, berger_referent_id, "
        "commission_id, coordination_id, intendance_id, tribu_id FROM membre WHERE id = %s",
        (membre_id,), role=role,
    ) or {}
    genre = m.get("genre")
    cat_rows = db.fetch_all(
        "SELECT cle, libelle_h, libelle_f, libelle_n, categorie, abreviation, transversal FROM fonction_honorifique",
        (), role=role,
    )
    catalogue = {str(r["cle"]).lower(): r for r in cat_rows}

    # 1) All active functions of the member (multi-role), most authoritative first.
    mf_rows = db.fetch_all(
        "SELECT fonction_cle, perimetre, principale, date_debut FROM membre_fonction "
        "WHERE membre_id = %s AND actif = true AND confirmee = true ORDER BY principale DESC, ordre ASC, cree_le ASC",
        (membre_id,), role=role,
    )
    fonctions: list[dict[str, Any]] = []
    for r in mf_rows:
        cat = catalogue.get(str(r.get("fonction_cle") or "").lower())
        categorie = str((cat or {}).get("categorie") or "fonction")
        fonctions.append({
            "fonction": _hlabel(cat, str(r.get("fonction_cle") or ""), genre),
            "categorie": categorie,
            "perimetre": r.get("perimetre") or None,
            "principale": bool(r.get("principale")),
            "abreviation": (cat or {}).get("abreviation"),
            "depuis": r["date_debut"].isoformat() if r.get("date_debut") else None,
        })
    fonctions.sort(key=lambda f: (0 if f["principale"] else 1, _CAT_RANG.get(f["categorie"], 9)))
    position_principale = fonctions[0] if fonctions else None

    # 2) Units of the member + their responsible (for the rattachements section).
    def unite(table: str, uid: object, fonction: str) -> dict[str, Any] | None:
        if not uid:
            return None
        row = db.fetch_one(f"SELECT id, nom, responsable_id FROM {table} WHERE id = %s", (uid,), role=role)
        if not row:
            return None
        resp = db.fetch_one("SELECT prenoms, nom, nom_affiche FROM membre WHERE id = %s", (row.get("responsable_id"),), role=role) if row.get("responsable_id") else None
        return {"id": str(row["id"]), "nom": row.get("nom"), "responsable": _hnom(resp), "fonction": fonction}

    com = unite("commission", m.get("commission_id"), "Responsable")
    coord = unite("coordination", m.get("coordination_id"), "Coordinateur")
    inten = unite("intendance", m.get("intendance_id"), "Intendant")

    tribu = None
    if m.get("tribu_id"):
        tr = db.fetch_one("SELECT nom, patriarche, patriarche_membre_id FROM tribu WHERE id = %s", (m["tribu_id"],), role=role)
        if tr:
            patr = db.fetch_one("SELECT prenoms, nom, nom_affiche FROM membre WHERE id = %s", (tr.get("patriarche_membre_id"),), role=role) if tr.get("patriarche_membre_id") else None
            nom_patr = _hnom(patr) or (tr.get("patriarche") or None)
            tribu = {"nom": tr.get("nom"), "patriarche": nom_patr}

    # 3) The personalised upward chain (N+1..N), keeping vacant posts.
    raw_com = db.fetch_one("SELECT id, nom, responsable_id FROM commission WHERE id = %s", (m.get("commission_id"),), role=role) if m.get("commission_id") else None
    raw_coord = db.fetch_one("SELECT id, nom, responsable_id FROM coordination WHERE id = %s", (m.get("coordination_id"),), role=role) if m.get("coordination_id") else None
    raw_inten = db.fetch_one("SELECT id, nom, responsable_id FROM intendance WHERE id = %s", (m.get("intendance_id"),), role=role) if m.get("intendance_id") else None

    etages: list[dict[str, Any]] = []

    def ajouter_niveau(fcle: str, libelle: str, unite_nom: object, unite_row: dict[str, Any] | None) -> None:
        noms, est_moi, vacant = _occupants_unite(unite_row, fcle, role, membre_id)
        if not noms and est_moi:
            return  # the member holds this level; their chain starts above it
        etages.append({
            "fonction": libelle, "unite": unite_nom,
            "occupants": [{"nom": n} for n in noms],
            "vacant": vacant and not noms,
        })

    if raw_com:
        ajouter_niveau("responsable", _hlabel(catalogue.get("responsable"), "responsable", genre), raw_com.get("nom"), raw_com)
    if raw_coord:
        ajouter_niveau("coordinateur", _hlabel(catalogue.get("coordinateur"), "coordinateur", genre), raw_coord.get("nom"), raw_coord)
    if raw_inten:
        ajouter_niveau("intendant", _hlabel(catalogue.get("intendant"), "intendant", genre), raw_inten.get("nom"), raw_inten)
    for fcle in _APEX:
        h = organigramme_builder._titulaire_fonction(fcle, role)
        nom = organigramme_builder._nom_membre(h)
        if h and str(h.get("id")) == str(membre_id):
            continue
        etages.append({
            "fonction": _hlabel(catalogue.get(fcle), fcle, genre), "unite": None,
            "occupants": ([{"nom": nom}] if nom else []),
            "vacant": not nom,
        })
    niveaux = [{"rang": f"N+{i + 1}", **e} for i, e in enumerate(etages)]
    chaine_principale = {"titre": position_principale["fonction"] if position_principale else "Ma chaîne de responsabilité", "niveaux": niveaux}

    # 4) Titles and particular links (kept apart from the functional chain).
    titres: list[dict[str, Any]] = []
    if m.get("est_berger"):
        fem = str(genre or "").lower().startswith("f")
        titres.append({"type": "titre", "libelle": "Bergère" if fem else "Berger",
                       "detail": (str(m.get("nom_pastoral")) if m.get("nom_pastoral") else "Collège des bergers")})
        bmiss = organigramme_builder._titulaire_fonction("berger_missions", role)
        titres.append({"type": "rattachement_titre", "libelle": "Berger des missions",
                       "detail": organigramme_builder._nom_membre(bmiss) or "Poste à pourvoir"})
    if m.get("berger_referent_id"):
        ref = db.fetch_one("SELECT prenoms, nom, nom_affiche FROM membre WHERE id = %s", (m["berger_referent_id"],), role=role)
        if _hnom(ref):
            titres.append({"type": "accompagnement", "libelle": "Suivi par", "detail": _hnom(ref)})

    liens_particuliers: list[dict[str, Any]] = []
    if tribu:
        liens_particuliers.append({"type": "tribu", "libelle": f"Tribu {tribu['nom']}" if tribu.get("nom") else "Tribu",
                                    "detail": (f"Patriarche : {tribu['patriarche']}" if tribu.get("patriarche") else None)})

    # 5) Rattachements (units), with the member's role in each.
    def mon_role(unite_nom: object, defaut: str) -> str:
        if not unite_nom:
            return defaut
        for f in fonctions:
            if f.get("perimetre") and str(unite_nom).lower() in str(f["perimetre"]).lower():
                return f["fonction"]
        return defaut

    rattachements: list[dict[str, Any]] = []
    if com:
        rattachements.append({"type": "Commission / Mission", "nom": com["nom"], "mon_role": mon_role(com["nom"], "Membre"), "titulaire": com["responsable"], "principal": True})
    if coord:
        rattachements.append({"type": "Coordination", "nom": coord["nom"], "mon_role": mon_role(coord["nom"], "Membre"), "titulaire": coord["responsable"], "principal": False})
    if inten:
        rattachements.append({"type": "Intendance", "nom": inten["nom"], "mon_role": mon_role(inten["nom"], "Membre"), "titulaire": inten["responsable"], "principal": False})
    if tribu:
        rattachements.append({"type": "Tribu", "nom": tribu["nom"], "mon_role": "Membre", "titulaire": (f"Patriarche : {tribu['patriarche']}" if tribu.get("patriarche") else None), "principal": False})

    # Backward-compatible apex chain (unchanged shape) for the previous member client.
    chaine_compat = []
    for fcle in ("intendant_general", "controleur_general", "berger_missions", "moderateur", "fondateur"):
        h = organigramme_builder._titulaire_fonction(fcle, role)
        chaine_compat.append({"fonction": fcle.replace("_", " ").title(), "titulaire": organigramme_builder._nom_membre(h)})

    return {
        "moi": {"nom": _hnom(m), "est_berger": bool(m.get("est_berger")), "nom_pastoral": m.get("nom_pastoral") or None},
        "position_principale": position_principale,
        "fonctions": fonctions,
        "chaines": [chaine_principale] if niveaux else [],
        "titres": titres,
        "liens_particuliers": liens_particuliers,
        "rattachements": rattachements,
        "commission": ({"nom": com["nom"], "responsable": com["responsable"], "fonction": com["fonction"]} if com else None),
        "coordination": ({"nom": coord["nom"], "responsable": coord["responsable"], "fonction": coord["fonction"]} if coord else None),
        "intendance": ({"nom": inten["nom"], "responsable": inten["responsable"], "fonction": inten["fonction"]} if inten else None),
        "tribu": tribu,
        "chaine_fonctionnelle": chaine_compat,
    }
