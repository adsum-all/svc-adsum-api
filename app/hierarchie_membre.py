"""Function-based hierarchy of the connected member (feeds GET /membres/me/hierarchie).

Rebuilt from a single-rattachement + fixed apex list into a faithful view: every active
function (multi-role), a personalised upward chain N+1..N from the member's real units
then the apex, titles and particular links kept apart from the functional chain, and the
units the member belongs to. Vacant posts stay in the chain as "Poste a pourvoir".
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import Any

from . import db, interim, organigramme_builder

# Apex chain, function-based, from the highest operational level up to the founder.
_APEX = ("controleur_general", "intendant_general", "berger_missions", "moderateur", "fondateur")
# Display precedence between categories (lower wins), so the primary position is the
# member's highest-authority active function.
_CAT_RANG = {"fonction_speciale": 0, "titre": 1, "fonction": 2, "fonction_particuliere": 3}
_SEL_NOM = "SELECT prenoms, nom, nom_affiche FROM membre WHERE id = %s"


def _est_vice(fcle: str) -> bool:
    """A vice-function is support/relief, not a permanent N+1 level."""
    return fcle.lower().startswith("vice_")


def _rang_fonction(cat: dict[str, Any] | None, categorie: str) -> int:
    """Ordering rank (lower = higher authority): the fine per-function
    niveau_hierarchique when set, else a coarse rank derived from the category so
    ranked functions always sort above unranked titles/particular links."""
    nh = (cat or {}).get("niveau_hierarchique")
    if nh is not None:
        return int(nh)
    return 20 + _CAT_RANG.get(categorie, 9)


def _interim_actif(fcle: str, unite_nom: object, actifs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Active interim covering this function (and perimeter, when the interim scopes one)."""
    for it in actifs:
        if it.get("fonction_cle") != fcle.lower():
            continue
        p = it.get("perimetre")
        if not p:
            return it
        if unite_nom and (str(p).lower() in str(unite_nom).lower() or str(unite_nom).lower() in str(p).lower()):
            return it
    return None


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


def _construire_fonctions(mf_rows: list[dict[str, Any]], catalogue: dict[str, Any], genre: object) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split the member's active functions into ordinary functions (sorted by rank)
    and vice-functions (support/relief)."""
    fonctions: list[dict[str, Any]] = []
    vice_fonctions: list[dict[str, Any]] = []
    for r in mf_rows:
        fcle = str(r.get("fonction_cle") or "")
        cat = catalogue.get(fcle.lower())
        categorie = str((cat or {}).get("categorie") or "fonction")
        entree = {
            "fonction": _hlabel(cat, fcle, genre),
            "categorie": categorie,
            "perimetre": r.get("perimetre") or None,
            "principale": bool(r.get("principale")),
            "abreviation": (cat or {}).get("abreviation"),
            "rang": _rang_fonction(cat, categorie),
            "depuis": r["date_debut"].isoformat() if r.get("date_debut") else None,
        }
        (vice_fonctions if _est_vice(fcle) else fonctions).append(entree)
    fonctions.sort(key=lambda f: (0 if f["principale"] else 1, f["rang"]))
    return fonctions, vice_fonctions


def _etage_occupe(fcle: str, libelle: str, unite_nom: object, unite_row: dict[str, Any] | None,
                  role: str | None, membre_id: str, actifs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """One chain level for a unit-scoped function. None means the member holds it
    themselves (the chain starts above)."""
    noms, est_moi, vacant = _occupants_unite(unite_row, fcle, role, membre_id)
    if not noms and est_moi:
        return None
    it = _interim_actif(fcle, unite_nom, actifs)
    if it and it.get("suppleant"):
        return {"fonction": libelle, "unite": unite_nom, "occupants": [{"nom": it["suppleant"], "interim": True}],
                "vacant": False, "interim": True, "titulaire": it.get("titulaire")}
    return {"fonction": libelle, "unite": unite_nom, "occupants": [{"nom": n} for n in noms], "vacant": vacant and not noms}


def _construire_chaine(m: dict[str, Any], membre_id: str, role: str | None, genre: object,
                       catalogue: dict[str, Any], actifs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Personalised upward chain N+1..N: the member's real unit levels (with interim
    and vacant handling) then the apex, up to the founder."""
    def raw(table: str, uid: object) -> dict[str, Any] | None:
        return db.fetch_one(f"SELECT id, nom, responsable_id FROM {table} WHERE id = %s", (uid,), role=role) if uid else None

    etages: list[dict[str, Any]] = []
    niveaux_unite = (
        ("responsable", raw("commission", m.get("commission_id"))),
        ("coordinateur", raw("coordination", m.get("coordination_id"))),
        ("intendant", raw("intendance", m.get("intendance_id"))),
    )
    for fcle, unite_row in niveaux_unite:
        if not unite_row:
            continue
        etage = _etage_occupe(fcle, _hlabel(catalogue.get(fcle), fcle, genre), unite_row.get("nom"), unite_row, role, membre_id, actifs)
        if etage:
            etages.append(etage)
    for fcle in _APEX:
        h = organigramme_builder._titulaire_fonction(fcle, role)
        nom = organigramme_builder._nom_membre(h)
        if h and str(h.get("id")) == str(membre_id):
            continue
        it = _interim_actif(fcle, None, actifs)
        if it and it.get("suppleant"):
            etages.append({"fonction": _hlabel(catalogue.get(fcle), fcle, genre), "unite": None,
                           "occupants": [{"nom": it["suppleant"], "interim": True}], "vacant": False,
                           "interim": True, "titulaire": it.get("titulaire") or nom})
            continue
        etages.append({"fonction": _hlabel(catalogue.get(fcle), fcle, genre), "unite": None,
                       "occupants": ([{"nom": nom}] if nom else []), "vacant": not nom})
    return [{"rang": f"N+{i + 1}", **e} for i, e in enumerate(etages)]


def _construire_appui(vice_fonctions: list[dict[str, Any]], membre_id: str, role: str | None,
                      genre: object, catalogue: dict[str, Any]) -> list[dict[str, Any]]:
    """Support and relief: the member's own vice-functions plus any interim they cover."""
    appui: list[dict[str, Any]] = []
    for v in vice_fonctions:
        appui.append({"type": "vice", "fonction": v["fonction"], "perimetre": v.get("perimetre"),
                      "detail": None, "depuis": v.get("depuis"), "jusqu_au": None})
    mes_interims = db.fetch_all(
        "SELECT fonction_cle, perimetre, motif, date_debut, date_fin FROM organisation_interim "
        "WHERE suppleant_id = %s AND statut = 'actif' AND date_debut <= current_date "
        "AND (date_fin IS NULL OR date_fin >= current_date)",
        (membre_id,), role=role,
    )
    for it in mes_interims:
        fcle = str(it.get("fonction_cle") or "")
        appui.append({"type": "interim", "fonction": _hlabel(catalogue.get(fcle.lower()), fcle, genre),
                      "perimetre": it.get("perimetre") or None, "detail": it.get("motif") or None,
                      "depuis": it["date_debut"].isoformat() if it.get("date_debut") else None,
                      "jusqu_au": it["date_fin"].isoformat() if it.get("date_fin") else None})
    return appui


def _unites_membre(m: dict[str, Any], role: str | None) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    """The member's commission, coordination, intendance and tribe (with resolved leaders)."""
    def unite(table: str, uid: object, fonction: str) -> dict[str, Any] | None:
        if not uid:
            return None
        row = db.fetch_one(f"SELECT id, nom, responsable_id FROM {table} WHERE id = %s", (uid,), role=role)
        if not row:
            return None
        resp = db.fetch_one(_SEL_NOM, (row.get("responsable_id"),), role=role) if row.get("responsable_id") else None
        return {"id": str(row["id"]), "nom": row.get("nom"), "responsable": _hnom(resp), "fonction": fonction}

    com = unite("commission", m.get("commission_id"), "Responsable")
    coord = unite("coordination", m.get("coordination_id"), "Coordinateur")
    inten = unite("intendance", m.get("intendance_id"), "Intendant")
    tribu = None
    if m.get("tribu_id"):
        tr = db.fetch_one("SELECT nom, patriarche, patriarche_membre_id FROM tribu WHERE id = %s", (m["tribu_id"],), role=role)
        if tr:
            patr = db.fetch_one(_SEL_NOM, (tr.get("patriarche_membre_id"),), role=role) if tr.get("patriarche_membre_id") else None
            tribu = {"nom": tr.get("nom"), "patriarche": _hnom(patr) or (tr.get("patriarche") or None)}
    return com, coord, inten, tribu


def _construire_titres(m: dict[str, Any], role: str | None, genre: object) -> list[dict[str, Any]]:
    """Titles and accompaniment kept apart from the functional chain (berger dimension)."""
    titres: list[dict[str, Any]] = []
    if m.get("est_berger"):
        fem = str(genre or "").lower().startswith("f")
        titres.append({"type": "titre", "libelle": "Bergère" if fem else "Berger",
                       "detail": (str(m.get("nom_pastoral")) if m.get("nom_pastoral") else "Collège des bergers")})
        bmiss = organigramme_builder._titulaire_fonction("berger_missions", role)
        titres.append({"type": "rattachement_titre", "libelle": "Berger des missions",
                       "detail": organigramme_builder._nom_membre(bmiss) or "Poste à pourvoir"})
    if m.get("berger_referent_id"):
        ref = db.fetch_one(_SEL_NOM, (m["berger_referent_id"],), role=role)
        if _hnom(ref):
            titres.append({"type": "accompagnement", "libelle": "Suivi par", "detail": _hnom(ref)})
    return titres


def _construire_rattachements(com: dict[str, Any] | None, coord: dict[str, Any] | None,
                              inten: dict[str, Any] | None, tribu: dict[str, Any] | None,
                              fonctions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Units the member belongs to, with their role resolved from their functions' perimeter."""
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
    return rattachements


def calculer(membre_id: str, role: str | None) -> dict[str, Any]:
    m = db.fetch_one(
        "SELECT id, prenoms, nom, nom_affiche, genre, est_berger, nom_pastoral, berger_referent_id, "
        "commission_id, coordination_id, intendance_id, tribu_id FROM membre WHERE id = %s",
        (membre_id,), role=role,
    ) or {}
    genre = m.get("genre")
    cat_rows = db.fetch_all(
        "SELECT cle, libelle_h, libelle_f, libelle_n, categorie, abreviation, transversal, niveau_hierarchique FROM fonction_honorifique",
        (), role=role,
    )
    catalogue = {str(r["cle"]).lower(): r for r in cat_rows}

    # Currently-active interims, resolved once (chain annotation + own suppleances).
    actifs = interim.interims_actifs(role)

    # 1) All active functions of the member (multi-role), most authoritative first.
    # Vice-functions are routed out to the "Appui et suppleance" section instead of
    # standing as a permanent level.
    mf_rows = db.fetch_all(
        "SELECT fonction_cle, perimetre, principale, date_debut FROM membre_fonction "
        "WHERE membre_id = %s AND actif = true AND confirmee = true ORDER BY principale DESC, ordre ASC, cree_le ASC",
        (membre_id,), role=role,
    )
    fonctions, vice_fonctions = _construire_fonctions(mf_rows, catalogue, genre)
    position_principale = fonctions[0] if fonctions else None

    # 2) Units of the member + their responsible (for the rattachements section).
    com, coord, inten, tribu = _unites_membre(m, role)

    # 3) The personalised upward chain (N+1..N), keeping vacant posts and reflecting interims.
    niveaux = _construire_chaine(m, membre_id, role, genre, catalogue, actifs)
    chaine_principale = {"titre": position_principale["fonction"] if position_principale else "Ma chaîne de responsabilité", "niveaux": niveaux}

    # 4) Titles and particular links (kept apart from the functional chain).
    titres = _construire_titres(m, role, genre)
    liens_particuliers: list[dict[str, Any]] = []
    if tribu:
        liens_particuliers.append({"type": "tribu", "libelle": f"Tribu {tribu['nom']}" if tribu.get("nom") else "Tribu",
                                    "detail": (f"Patriarche : {tribu['patriarche']}" if tribu.get("patriarche") else None)})

    # 4b) Support and relief: the member's own vice-functions and any interim they cover.
    appui_suppleance = _construire_appui(vice_fonctions, membre_id, role, genre, catalogue)

    # 5) Rattachements (units), with the member's role in each.
    rattachements = _construire_rattachements(com, coord, inten, tribu, fonctions)

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
        "appui_suppleance": appui_suppleance,
        "liens_particuliers": liens_particuliers,
        "rattachements": rattachements,
        "commission": ({"nom": com["nom"], "responsable": com["responsable"], "fonction": com["fonction"]} if com else None),
        "coordination": ({"nom": coord["nom"], "responsable": coord["responsable"], "fonction": coord["fonction"]} if coord else None),
        "intendance": ({"nom": inten["nom"], "responsable": inten["responsable"], "fonction": inten["fonction"]} if inten else None),
        "tribu": tribu,
        "chaine_fonctionnelle": chaine_compat,
    }
