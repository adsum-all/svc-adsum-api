"""Row to response model mappers shared across routers (avoids duplication)."""
from __future__ import annotations

from typing import Any

from .schemas import MembreProfile

# Columns a member SELECT must expose for membre_row_to_profile, in order.
MEMBRE_PROFILE_SELECT = (
    "m.id, m.matricule, m.email, m.nom, m.prenoms, m.telephone, m.indicatif_telephone, m.groupe, m.photo_url, "
    "m.statut, m.verifie, m.genre, m.date_naissance, m.naissance_annee_visible, m.pays, m.region, m.ville, "
    "m.adresse, m.adresse_complement, m.date_entree, "
    "m.cheminement_pastoral, m.statut_administratif, m.intendance_id, m.berger_referent_id, "
    "m.tribu_id, m.type_membre, m.promotion, m.situation_matrimoniale, m.type_mariage, "
    "m.profession, m.niveau_etudes, m.baptise, m.confirme, m.premiere_communion, "
    "m.champs_deverrouilles, m.langue, "
    "m.commission_id, m.fonction_cle, m.fonction_confirmee, "
    "fh.libelle_h AS fonction_h, fh.libelle_f AS fonction_f, fh.libelle_n AS fonction_n, fh.est_vip AS fonction_vip, "
    "c.nom AS commission, i.nom AS intendance, bm.nom AS berger_nom, bm.prenoms AS berger_prenoms, "
    "t.nom AS tribu, t.patriarche, co.nom AS coordination, "
    "cm.nom AS coord_nom, cm.prenoms AS coord_prenoms"
)

# The joins that back the columns above. Use together with MEMBRE_PROFILE_SELECT.
MEMBRE_PROFILE_FROM = (
    "FROM membre m "
    "LEFT JOIN fonction_honorifique fh ON fh.cle = m.fonction_cle "
    "LEFT JOIN commission c ON c.id = m.commission_id "
    "LEFT JOIN intendance i ON i.id = m.intendance_id "
    "LEFT JOIN utilisateur bu ON bu.id = m.berger_referent_id "
    "LEFT JOIN membre bm ON bm.id = bu.membre_id "
    "LEFT JOIN tribu t ON t.id = m.tribu_id "
    "LEFT JOIN coordination co ON co.id = i.coordination_id "
    "LEFT JOIN utilisateur cu ON cu.id = co.responsable_id "
    "LEFT JOIN membre cm ON cm.id = cu.membre_id"
)


def _join_name(prenoms: object, nom: object) -> str | None:
    name = f"{prenoms or ''} {nom or ''}".strip()
    return name or None


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


def membre_row_to_profile(row: dict[str, Any]) -> MembreProfile:
    """Map a full member row (with all joins) to its profile."""
    return MembreProfile(
        id=str(row["id"]),
        matricule=row["matricule"],
        email=row["email"],
        nom=row["nom"],
        prenoms=row["prenoms"],
        telephone=row["telephone"],
        indicatif_telephone=row.get("indicatif_telephone"),
        groupe=row["groupe"],
        photo_url=row["photo_url"],
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
        profession=row.get("profession"),
        niveau_etudes=row.get("niveau_etudes"),
        baptise=row.get("baptise"),
        confirme=row.get("confirme"),
        premiere_communion=row.get("premiere_communion"),
        commission=row.get("commission"),
        intendance=row.get("intendance"),
        intendance_id=str(row["intendance_id"]) if row.get("intendance_id") else None,
        berger=_join_name(row.get("berger_prenoms"), row.get("berger_nom")),
        berger_referent_id=str(row["berger_referent_id"]) if row.get("berger_referent_id") else None,
        tribu=row.get("tribu"),
        tribu_id=str(row["tribu_id"]) if row.get("tribu_id") else None,
        patriarche=row.get("patriarche"),
        coordination=row.get("coordination"),
        coordinateur=_join_name(row.get("coord_prenoms"), row.get("coord_nom")),
        champs_deverrouilles=list(row.get("champs_deverrouilles") or []),
        langue=str(row.get("langue") or "fr"),
        commission_id=str(row["commission_id"]) if row.get("commission_id") else None,
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
