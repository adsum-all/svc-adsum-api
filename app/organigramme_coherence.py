# ruff: noqa: E501 - long SQL statements
"""Pre-publication coherence report over the REAL organisation data (not the chart
nodes): units without a leader, vacant or duplicated apex posts, members with no
attachment, multi-role members, vice-functions covering a post with no formal interim,
and interims left open past their end date. Feeds the /valider check and a standalone
back-office report so the direction sees anomalies before publishing.
"""
from __future__ import annotations

from typing import Any

from . import db, interim

# Apex functions that must be held by exactly one active member.
_APEX_UNIQUE = ("fondateur", "moderateur", "berger_missions", "controleur_general", "intendant_general")
_BASE_DE_VICE = {"vice_coordinateur": "coordinateur", "vice_intendant": "intendant"}


def _noms_echantillon(rows: list[dict[str, Any]], limite: int = 6) -> str:
    noms = []
    for r in rows[:limite]:
        aff = r.get("nom_affiche")
        noms.append(str(aff) if aff else f"{r.get('prenoms') or ''} {r.get('nom') or ''}".strip() or r.get("nom") or "?")
    suffixe = "..." if len(rows) > limite else ""
    return ", ".join(n for n in noms if n) + suffixe


def _unites_sans_responsable(role: str | None) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    plans = (
        ("commission", "Commission / mission", "SELECT nom FROM commission WHERE responsable_id IS NULL ORDER BY nom"),
        ("coordination", "Coordination", "SELECT nom FROM coordination WHERE responsable_id IS NULL AND coalesce(statut, 'actif') = 'actif' ORDER BY nom"),
        ("intendance", "Intendance", "SELECT nom FROM intendance WHERE responsable_id IS NULL AND coalesce(statut, 'actif') = 'actif' ORDER BY nom"),
    )
    for code, libelle, sql in plans:
        rows = db.fetch_all(sql, (), role=role)
        if rows:
            echantillon = ", ".join(str(r.get("nom")) for r in rows[:6] if r.get("nom")) + ("..." if len(rows) > 6 else "")
            findings.append({"code": f"unite_sans_responsable.{code}", "niveau": "avertissement",
                             "message": f"{len(rows)} {libelle.lower()}(s) sans responsable : {echantillon}."})
    return findings


def _apex(role: str | None) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for cle in _APEX_UNIQUE:
        rows = db.fetch_all(
            "SELECT m.prenoms, m.nom, m.nom_affiche FROM membre_fonction mf JOIN membre m ON m.id = mf.membre_id "
            "WHERE mf.actif = true AND mf.confirmee = true AND lower(mf.fonction_cle) = %s AND m.statut = 'actif'",
            (cle,), role=role,
        )
        label = cle.replace("_", " ").title()
        if not rows:
            findings.append({"code": f"apex_vacant.{cle}", "niveau": "avertissement",
                             "message": f"Poste clé vacant : {label} (aucun titulaire actif)."})
        elif len(rows) > 1:
            findings.append({"code": f"apex_multiple.{cle}", "niveau": "avertissement",
                             "message": f"{label} tenu par {len(rows)} personnes : {_noms_echantillon(rows)} (poste attendu unique)."})
    return findings


def _interims_expires(role: str | None) -> list[dict[str, Any]]:
    rows = db.fetch_all(
        "SELECT fonction_cle, perimetre, date_fin FROM organisation_interim "
        "WHERE statut = 'actif' AND date_fin IS NOT NULL AND date_fin < current_date ORDER BY date_fin",
        (), role=role,
    )
    if not rows:
        return []
    ech = ", ".join(f"{r.get('fonction_cle')}{(' (' + str(r.get('perimetre')) + ')') if r.get('perimetre') else ''}" for r in rows[:6])
    return [{"code": "interim_expire", "niveau": "avertissement",
             "message": f"{len(rows)} intérim(s) échu(s) mais toujours actif(s), à clôturer : {ech}{'...' if len(rows) > 6 else ''}."}]


def _vice_sans_interim(role: str | None, actifs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Vice-functions covering a base post that has no active holder and no formal interim."""
    vices = db.fetch_all(
        "SELECT mf.perimetre, lower(mf.fonction_cle) AS cle, m.prenoms, m.nom, m.nom_affiche FROM membre_fonction mf "
        "JOIN membre m ON m.id = mf.membre_id WHERE mf.actif = true AND mf.confirmee = true AND lower(mf.fonction_cle) LIKE %s",
        ("vice\\_%",), role=role,
    )
    problematiques: list[dict[str, Any]] = []
    for v in vices:
        base = _BASE_DE_VICE.get(str(v.get("cle")), str(v.get("cle"))[5:])
        perim = v.get("perimetre")
        titulaire = db.fetch_one(
            "SELECT 1 FROM membre_fonction mf JOIN membre m ON m.id = mf.membre_id "
            "WHERE mf.actif = true AND mf.confirmee = true AND lower(mf.fonction_cle) = %s AND m.statut = 'actif' "
            "AND (%s::text IS NULL OR mf.perimetre IS NULL OR lower(mf.perimetre) LIKE %s)",
            (base, perim, f"%{str(perim).lower()}%" if perim else "%"), role=role,
        )
        if titulaire:
            continue
        couvert = any(it.get("fonction_cle") == base and (not it.get("perimetre") or not perim or str(it["perimetre"]).lower() in str(perim).lower() or str(perim).lower() in str(it["perimetre"]).lower()) for it in actifs)
        if not couvert:
            problematiques.append(v)
    if not problematiques:
        return []
    return [{"code": "vice_sans_interim", "niveau": "avertissement",
             "message": f"{len(problematiques)} vice-fonction(s) assurant un poste vacant sans intérim formalisé : {_noms_echantillon(problematiques)}. Formalisez une suppléance datée."}]


def _rattachement_et_roles(role: str | None) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    orphelins = db.fetch_one(
        "SELECT count(*) AS n FROM membre m WHERE m.statut = 'actif' "
        "AND m.commission_id IS NULL AND m.coordination_id IS NULL AND m.intendance_id IS NULL AND m.tribu_id IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM membre_fonction mf WHERE mf.membre_id = m.id AND mf.actif = true)",
        (), role=role,
    ) or {}
    n_orph = int(orphelins.get("n") or 0)
    if n_orph:
        findings.append({"code": "membre_sans_rattachement", "niveau": "info",
                         "message": f"{n_orph} membre(s) actif(s) sans aucun rattachement (unité, tribu ou fonction)."})
    multi = db.fetch_one(
        "SELECT count(*) AS n FROM (SELECT membre_id FROM membre_fonction WHERE actif = true AND confirmee = true "
        "GROUP BY membre_id HAVING count(*) >= 2) t",
        (), role=role,
    ) or {}
    n_multi = int(multi.get("n") or 0)
    if n_multi:
        findings.append({"code": "membre_multi_roles", "niveau": "info",
                         "message": f"{n_multi} membre(s) cumulent plusieurs fonctions actives (multi-rôles)."})
    return findings


def rapport(role: str | None) -> dict[str, Any]:
    """Full coherence report. `bloquants` non-empty forbids publication; `avertissements`
    and `infos` are advisory. Findings are grouped by level for the UI."""
    actifs = interim.interims_actifs(role)
    findings: list[dict[str, Any]] = []
    findings.extend(_unites_sans_responsable(role))
    findings.extend(_apex(role))
    findings.extend(_interims_expires(role))
    findings.extend(_vice_sans_interim(role, actifs))
    findings.extend(_rattachement_et_roles(role))
    bloquants = [f for f in findings if f["niveau"] == "bloquant"]
    avertissements = [f for f in findings if f["niveau"] == "avertissement"]
    infos = [f for f in findings if f["niveau"] == "info"]
    return {"findings": findings, "bloquants": bloquants, "avertissements": avertissements, "infos": infos,
            "resume": {"bloquants": len(bloquants), "avertissements": len(avertissements), "infos": len(infos)}}
