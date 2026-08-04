"""Row to response model mappers shared across routers (avoids duplication)."""
# ruff: noqa: E501 - long SQL SELECT/FROM constants
from __future__ import annotations

from typing import Any

from . import identite
from .fonctions_membre import label_genre
from .schemas import MembreProfile

# Columns a member SELECT must expose for membre_row_to_profile, in order.
MEMBRE_PROFILE_SELECT = (
    "m.id, m.matricule, m.code_membre, m.email, m.nom, m.prenoms, m.telephone, m.indicatif_telephone, m.groupe, m.photo_url, "
    "m.photo_pending_url, m.photo_focus_x, m.photo_focus_y, "
    "m.nom_naissance, m.nom_marital, m.nom_affiche, m.est_berger, m.nom_pastoral, m.fonction_perimetre, "
    "m.berger_declare, m.berger_nom_declare, "
    "m.statut, m.verifie, m.genre, m.date_naissance, m.naissance_annee_visible, m.pays, m.region, m.ville, "
    "m.adresse, m.adresse_complement, m.date_entree, "
    "m.cheminement_pastoral, m.statut_administratif, m.intendance_id, m.berger_referent_id, "
    "m.tribu_id, m.type_membre, m.promotion, m.situation_matrimoniale, m.type_mariage, m.en_cheminement, "
    "m.profession, m.niveau_etudes, m.baptise, m.confirme, m.premiere_communion, "
    "m.champs_deverrouilles, m.langue, m.theme, m.anniversaire_visible_annuaire, m.whatsapp_numero, "
    "m.commission_id, m.fonction_cle, m.fonction_confirmee, "
    "fh.libelle_h AS fonction_h, fh.libelle_f AS fonction_f, fh.libelle_n AS fonction_n, fh.est_vip AS fonction_vip, "
    "c.nom AS commission, c.type_organisation AS commission_type, i.nom AS intendance, bm.nom AS berger_nom, bm.prenoms AS berger_prenoms, "
    "t.nom AS tribu, t.couleur AS tribu_couleur, patr.patr_prenoms, patr.patr_nom, "
    "COALESCE(codirect.nom, co.nom) AS coordination, m.coordination_id, "
    "cm.nom AS coord_nom, cm.prenoms AS coord_prenoms, cm.genre AS coord_resp_genre, "
    "intd.int_prenoms, intd.int_nom, intd.int_genre, "
    "coordf.coordf_prenoms, coordf.coordf_nom, coordf.coordf_genre, "
    "fint.libelle_h AS fint_h, fint.libelle_f AS fint_f, fint.libelle_n AS fint_n, "
    "fcoord.libelle_h AS fcoord_h, fcoord.libelle_f AS fcoord_f, fcoord.libelle_n AS fcoord_n"
)

# The joins that back the columns above. Use together with MEMBRE_PROFILE_SELECT.
MEMBRE_PROFILE_FROM = (
    "FROM membre m "
    "LEFT JOIN fonction_honorifique fh ON fh.cle = m.fonction_cle "
    # Catalog rows for the responsable titles, so the gendered label
    # (Intendant/Intendante, Coordinateur/Coordinatrice) comes from one source.
    "LEFT JOIN fonction_honorifique fint ON fint.cle = 'intendant' "
    "LEFT JOIN fonction_honorifique fcoord ON fcoord.cle = 'coordinateur' "
    "LEFT JOIN commission c ON c.id = m.commission_id "
    "LEFT JOIN intendance i ON i.id = m.intendance_id "
    "LEFT JOIN coordination codirect ON codirect.id = m.coordination_id "
    "LEFT JOIN utilisateur bu ON bu.id = m.berger_referent_id "
    "LEFT JOIN membre bm ON bm.id = bu.membre_id "
    "LEFT JOIN tribu t ON t.id = m.tribu_id "
    # Patriarche of the member's tribe: the tribe member who holds the
    # 'patriarche' function (primary mirror or an active membre_fonction).
    "LEFT JOIN LATERAL ("
    "  SELECT pm2.prenoms AS patr_prenoms, pm2.nom AS patr_nom FROM membre pm2 "
    "  WHERE pm2.tribu_id = t.id AND (pm2.fonction_cle = 'patriarche' "
    "    OR EXISTS (SELECT 1 FROM membre_fonction mf WHERE mf.membre_id = pm2.id AND lower(mf.fonction_cle) = 'patriarche' AND mf.actif)) "
    "  ORDER BY pm2.nom LIMIT 1"
    ") patr ON true "
    # Intendant of the member's intendance: same pattern as the patriarche, scoped
    # to the intendance instead of the tribe. Gender is carried for the title.
    "LEFT JOIN LATERAL ("
    "  SELECT im.prenoms AS int_prenoms, im.nom AS int_nom, im.genre AS int_genre FROM membre im "
    "  WHERE im.intendance_id = i.id AND (im.fonction_cle = 'intendant' "
    "    OR EXISTS (SELECT 1 FROM membre_fonction mf WHERE mf.membre_id = im.id AND lower(mf.fonction_cle) = 'intendant' AND mf.actif)) "
    "  ORDER BY im.nom LIMIT 1"
    ") intd ON true "
    "LEFT JOIN coordination co ON co.id = i.coordination_id "
    "LEFT JOIN utilisateur cu ON cu.id = co.responsable_id "
    "LEFT JOIN membre cm ON cm.id = cu.membre_id "
    # Coordinateur of the member's coordination (direct membre.coordination_id, or
    # the one reached transitively through the intendance): the coordination member
    # holding the 'coordinateur' function. This is the primary source; the legacy
    # coordination.responsable_id (cm above) is kept only as a fallback.
    "LEFT JOIN LATERAL ("
    "  SELECT com.prenoms AS coordf_prenoms, com.nom AS coordf_nom, com.genre AS coordf_genre FROM membre com "
    "  WHERE com.coordination_id = COALESCE(codirect.id, co.id) AND (com.fonction_cle = 'coordinateur' "
    "    OR EXISTS (SELECT 1 FROM membre_fonction mf WHERE mf.membre_id = com.id AND lower(mf.fonction_cle) = 'coordinateur' AND mf.actif)) "
    "  ORDER BY com.nom LIMIT 1"
    ") coordf ON true"
)


def _join_name(prenoms: object, nom: object) -> str | None:
    name = f"{prenoms or ''} {nom or ''}".strip()
    return name or None


def _resolve_responsables(row: dict[str, Any]) -> tuple[str | None, str | None, str | None, str | None]:
    """Resolve the intendant and coordinateur names and gender-aware titles.

    Returns ``(intendant, intendant_titre, coordinateur, coordinateur_titre)``.
    The coordinateur prefers the function-resolved holder and falls back to the
    legacy ``coordination.responsable_id`` link so previously-set values still
    show. Each title is None when there is no holder.
    """
    intendant = _join_name(row.get("int_prenoms"), row.get("int_nom"))
    intendant_titre = (
        label_genre(row.get("int_genre"), row.get("fint_h"), row.get("fint_f"), row.get("fint_n"))
        if intendant
        else None
    )
    coordf_present = bool(row.get("coordf_prenoms") or row.get("coordf_nom"))
    coordinateur = (
        _join_name(row.get("coordf_prenoms"), row.get("coordf_nom"))
        or _join_name(row.get("coord_prenoms"), row.get("coord_nom"))
    )
    coord_genre = row.get("coordf_genre") if coordf_present else row.get("coord_resp_genre")
    coordinateur_titre = (
        label_genre(coord_genre, row.get("fcoord_h"), row.get("fcoord_f"), row.get("fcoord_n"))
        if coordinateur
        else None
    )
    return intendant, intendant_titre, coordinateur, coordinateur_titre


def titre_prefixe(genre: object, confirmee: object, h: object, f: object, n: object) -> str | None:
    """Resolve the honorific prefix by gender, only once an admin confirmed it.

    An unconfirmed function never yields a public prefix, so a member cannot
    display an unearned title. Gender maps homme -> masculine, femme -> feminine,
    anything else -> the epicene form.
    """
    if not confirmee:
        return None
    if genre == "homme":
        return str(h) if h else None
    if genre == "femme":
        return str(f) if f else None
    return str(n) if n else (str(h) if h else None)


def _nom_civil_source(row: dict[str, Any]) -> str | None:
    """The family name chosen for the civil display (married-woman arbitration)."""
    choix = row.get("nom_affiche")
    if choix == "naissance" and row.get("nom_naissance"):
        return row.get("nom_naissance")
    if choix == "marital" and row.get("nom_marital"):
        return row.get("nom_marital")
    return row.get("nom")


def _identite_org(row: dict[str, Any], fonctions: list[dict[str, Any]]) -> dict[str, str]:
    """Resolved organisational appellation for the profile, via the central resolver."""
    nom_civil = identite.nom_affichage(_nom_civil_source(row), row.get("prenoms"))
    ident = identite.resoudre_identite(
        genre=row.get("genre"),
        prenoms=row.get("prenoms"),
        nom_civil=nom_civil,
        est_berger=bool(row.get("est_berger")),
        nom_pastoral=row.get("nom_pastoral"),
        fonctions=[
            {
                "categorie": fn.get("categorie") or "fonction",
                "libelle": fn.get("libelle"),
                "abreviation": fn.get("abreviation"),
                "perimetre": fn.get("perimetre"),
            }
            for fn in (fonctions or [])
        ],
    )
    return {
        "appellation": str(ident.get("appellation") or ""),
        "appellation_formelle": str(ident.get("formel") or ""),
        "categorie_principale": str(ident.get("categorie_principale") or "civil"),
    }


def membre_row_to_profile(row: dict[str, Any], fonctions: list[dict[str, Any]] | None = None) -> MembreProfile:
    """Map a full member row (with all joins) to its profile.

    ``fonctions`` (the member's active, confirmed functions) is passed only for
    single-member reads (profile, member file); list endpoints leave it empty to
    avoid an N+1 query, and rely on the compact primary function instead.
    """
    intendant, intendant_titre, coordinateur, coordinateur_titre = _resolve_responsables(row)
    return MembreProfile(
        id=str(row["id"]),
        matricule=row["matricule"],
        code_membre=row.get("code_membre"),
        email=row["email"],
        nom=row["nom"],
        prenoms=row["prenoms"],
        nom_affichage=identite.nom_affichage(_nom_civil_source(row), row.get("prenoms")),
        nom_naissance=row.get("nom_naissance"),
        nom_marital=row.get("nom_marital"),
        nom_affiche=row.get("nom_affiche"),
        est_berger=bool(row.get("est_berger")),
        berger_declare=bool(row.get("berger_declare")),
        berger_nom_declare=row.get("berger_nom_declare"),
        nom_pastoral=row.get("nom_pastoral"),
        nom_pastoral_affiche=(
            identite.nom_pastoral_affichage(row.get("genre"), row.get("nom_pastoral"))
            if row.get("est_berger")
            else None
        ),
        fonction_perimetre=row.get("fonction_perimetre"),
        fonctions=[  # type: ignore[misc]
            {
                "libelle": str(fn.get("libelle")),
                "perimetre": fn.get("perimetre"),
                "cle": fn.get("cle"),
                "categorie": fn.get("categorie") or "fonction",
                "abreviation": fn.get("abreviation"),
            }
            for fn in (fonctions or [])
        ],
        **_identite_org(row, fonctions or []),
        telephone=row["telephone"],
        indicatif_telephone=row.get("indicatif_telephone"),
        whatsapp_numero=row.get("whatsapp_numero"),
        groupe=row["groupe"],
        photo_url=row["photo_url"],
        photo_pending=bool(row.get("photo_pending_url")),
        photo_focus_x=row.get("photo_focus_x"),
        photo_focus_y=row.get("photo_focus_y"),
        statut=row["statut"],
        verifie=row["verifie"],
        genre=row.get("genre"),
        date_naissance=row.get("date_naissance"),
        naissance_annee_visible=bool(row.get("naissance_annee_visible")),
        pays=row.get("pays"),
        region=row.get("region"),
        ville=row.get("ville"),
        adresse=row.get("adresse"),
        adresse_complement=row.get("adresse_complement"),
        date_entree=row.get("date_entree"),
        cheminement_pastoral=row.get("cheminement_pastoral"),
        statut_administratif=row.get("statut_administratif"),
        type_membre=row.get("type_membre"),
        promotion=row.get("promotion"),
        situation_matrimoniale=row.get("situation_matrimoniale"),
        type_mariage=row.get("type_mariage"),
        en_cheminement=row.get("en_cheminement"),
        profession=row.get("profession"),
        niveau_etudes=row.get("niveau_etudes"),
        baptise=row.get("baptise"),
        confirme=row.get("confirme"),
        premiere_communion=row.get("premiere_communion"),
        commission=row.get("commission"),
        commission_type=row.get("commission_type"),
        intendance=row.get("intendance"),
        intendance_id=str(row["intendance_id"]) if row.get("intendance_id") else None,
        berger=_join_name(row.get("berger_prenoms"), row.get("berger_nom")),
        berger_referent_id=str(row["berger_referent_id"]) if row.get("berger_referent_id") else None,
        tribu=row.get("tribu"),
        # The colour travels with the name: a member recognises their tribe by it
        # before they read anything, so the two are never sent apart.
        tribu_couleur=row.get("tribu_couleur"),
        tribu_id=str(row["tribu_id"]) if row.get("tribu_id") else None,
        # "patriarche" is the tribe member holding the 'patriarche' function,
        # resolved and shown in civil form; None (blank) when no one holds it.
        patriarche=(
            identite.nom_affichage(row.get("patr_nom"), row.get("patr_prenoms")) or None
            if (row.get("patr_prenoms") or row.get("patr_nom"))
            else None
        ),
        coordination=row.get("coordination"),
        coordination_id=str(row["coordination_id"]) if row.get("coordination_id") else None,
        coordinateur=coordinateur,
        coordinateur_titre=coordinateur_titre,
        intendant=intendant,
        intendant_titre=intendant_titre,
        champs_deverrouilles=list(row.get("champs_deverrouilles") or []),
        langue=str(row.get("langue") or "fr"),
        theme=str(row.get("theme") or "light"),
        commission_id=str(row["commission_id"]) if row.get("commission_id") else None,
        anniversaire_visible_annuaire=bool(row.get("anniversaire_visible_annuaire", True)),
        fonction_cle=row.get("fonction_cle"),
        fonction_confirmee=bool(row.get("fonction_confirmee")),
        titre=titre_prefixe(
            row.get("genre"),
            row.get("fonction_confirmee"),
            row.get("fonction_h"),
            row.get("fonction_f"),
            row.get("fonction_n"),
        ),
    )
