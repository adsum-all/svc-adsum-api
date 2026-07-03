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
    "date_naissance": {"libelle": "Date de naissance", "type": "champ", "sensibilite": "haute"},
    # Contact and administrative fields
    "telephone": {"libelle": "Téléphone", "type": "champ", "sensibilite": "normale"},
    "ville": {"libelle": "Ville", "type": "champ", "sensibilite": "normale"},
    "pays": {"libelle": "Pays", "type": "champ", "sensibilite": "normale"},
    "situation_matrimoniale": {"libelle": "Situation matrimoniale", "type": "champ", "sensibilite": "normale"},
    "profession": {"libelle": "Profession", "type": "champ", "sensibilite": "normale"},
    "niveau_etudes": {"libelle": "Niveau d'études", "type": "champ", "sensibilite": "normale"},
    # Identity artifacts
    "photo_identite": {"libelle": "Photo d'identité", "type": "photo", "sensibilite": "haute"},
    "piece_identite": {"libelle": "Pièce d'identité officielle", "type": "document", "sensibilite": "haute"},
}


def libelles(cles: list[str]) -> list[str]:
    """Member-facing labels for a list of unlock keys (unknown keys as-is)."""
    return [ELEMENTS.get(c, {}).get("libelle", c) for c in cles]
