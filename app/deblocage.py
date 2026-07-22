"""Catalog of elements the administration can unlock for member correction.

Single source of truth for the unlock workflow: the admin UI lists it, the
unlock endpoint validates against it, and the member-side guards consume the
same keys. Storage stays the existing ``membre.champs_deverrouilles text[]``
(the keys below), so no parallel mechanism is introduced. Adding a future
unlockable element is one entry here, never a schema change.
"""
from __future__ import annotations

# cle -> libelle (member-facing French), type (champ | photo | document),
# sensibilite (haute = identity-grade, normale = contact/administrative).
ELEMENTS: dict[str, dict[str, str]] = {
    # Identity fields
    "nom": {"libelle": "Nom", "type": "champ", "sensibilite": "haute"},
    "prenoms": {"libelle": "Prénoms", "type": "champ", "sensibilite": "haute"},
    "nom_naissance": {"libelle": "Nom de naissance", "type": "champ", "sensibilite": "haute"},
    "nom_marital": {"libelle": "Nom marital", "type": "champ", "sensibilite": "haute"},
    "nom_affiche": {"libelle": "Nom de famille affiché", "type": "champ", "sensibilite": "normale"},
    "date_naissance": {"libelle": "Date de naissance", "type": "champ", "sensibilite": "haute"},
    "naissance_annee_visible": {"libelle": "Afficher l'année de naissance", "type": "champ", "sensibilite": "normale"},
    "genre": {"libelle": "Genre", "type": "champ", "sensibilite": "haute"},
    # Member code (external organisation code, distinct from the ADSUM matricule)
    "code_membre": {"libelle": "Code membre", "type": "champ", "sensibilite": "normale"},
    # Contact and administrative fields
    "telephone": {"libelle": "Téléphone", "type": "champ", "sensibilite": "normale"},
    "indicatif_telephone": {"libelle": "Indicatif téléphonique", "type": "champ", "sensibilite": "normale"},
    "whatsapp_numero": {"libelle": "Numéro WhatsApp", "type": "champ", "sensibilite": "normale"},
    "ville": {"libelle": "Ville", "type": "champ", "sensibilite": "normale"},
    "region": {"libelle": "Région / État", "type": "champ", "sensibilite": "normale"},
    "pays": {"libelle": "Pays", "type": "champ", "sensibilite": "normale"},
    "adresse": {"libelle": "Adresse (générale)", "type": "champ", "sensibilite": "normale"},
    "adresse_complement": {"libelle": "Complément d'adresse", "type": "champ", "sensibilite": "normale"},
    # Community structure
    "commission_id": {"libelle": "Commission / Mission", "type": "champ", "sensibilite": "normale"},
    "intendance_id": {"libelle": "Intendance", "type": "champ", "sensibilite": "normale"},
    "coordination_id": {"libelle": "Coordination", "type": "champ", "sensibilite": "normale"},
    "tribu_id": {"libelle": "Tribu", "type": "champ", "sensibilite": "normale"},
    "groupe": {"libelle": "Sous-commission", "type": "champ", "sensibilite": "normale"},
    # Personal life and pastoral path
    "situation_matrimoniale": {"libelle": "Situation matrimoniale", "type": "champ", "sensibilite": "normale"},
    "type_mariage": {"libelle": "Type de mariage", "type": "champ", "sensibilite": "normale"},
    "en_cheminement": {"libelle": "En cheminement vers le mariage", "type": "champ", "sensibilite": "normale"},
    "type_membre": {"libelle": "Statut de membre", "type": "champ", "sensibilite": "normale"},
    "date_entree": {"libelle": "Date d'entrée", "type": "champ", "sensibilite": "normale"},
    "promotion": {"libelle": "Promotion", "type": "champ", "sensibilite": "normale"},
    "profession": {"libelle": "Profession", "type": "champ", "sensibilite": "normale"},
    "niveau_etudes": {"libelle": "Niveau d'études", "type": "champ", "sensibilite": "normale"},
    "baptise": {"libelle": "Baptisé", "type": "champ", "sensibilite": "normale"},
    "confirme": {"libelle": "Confirmé", "type": "champ", "sensibilite": "normale"},
    "premiere_communion": {"libelle": "Première communion", "type": "champ", "sensibilite": "normale"},
    "berger_declare": {"libelle": "Déclaration berger/bergère", "type": "champ", "sensibilite": "normale"},
    "berger_nom_declare": {"libelle": "Nom pastoral déclaré", "type": "champ", "sensibilite": "normale"},
    # Identity artifacts
    "photo_identite": {"libelle": "Photo d'identité", "type": "photo", "sensibilite": "haute"},
    "piece_identite": {"libelle": "Pièce d'identité officielle", "type": "document", "sensibilite": "haute"},
}


def libelles(cles: list[str]) -> list[str]:
    """Member-facing labels for a list of unlock keys (unknown keys as-is)."""
    return [ELEMENTS.get(c, {}).get("libelle", c) for c in cles]
