"""Who the platform says it is, read from settings rather than written into it.

The organisation's name, its short form, its city and its colours were literals in the
e-mail templates, the message bodies and the signatures. That was true enough while
one organisation used the platform. It stops being true the moment a second one does:
a parish deploying this would send its members mail headed ADSUM, signed Sacerdoce
Royal, and footed with an address in Abidjan.

Everything here reads from the settings the organisation already fills in, with the
current values as fallbacks so nothing changes for the organisation running today.

The colours matter as much as the words. An organisation is recognised by its own,
and a message arriving in someone's inbox in somebody else's palette does not look
like a message from their organisation.
"""
from __future__ import annotations

from . import db

# What the platform looked like before any of this was configurable. Kept as the
# fallback so an unconfigured base behaves exactly as it did.
_DEFAUTS = {
    # The product line, shown large in the header. Distinct from the organisation's
    # own name below it: one names the platform, the other names who is writing. A
    # deployment that wants its own branding replaces this one.
    "org_marque": "ADSUM",
    "org_nom": "Sacerdoce Royal",
    "org_nom_court": "ADSUM",
    "org_ville": "Abidjan",
    "org_signature": "Sacerdoce Royal",
    "org_couleur_principale": "#2a4fad",
    "org_couleur_sombre": "#1d3470",
    "org_baseline": "plateforme d'adhésion et de présence",
}


def _valeurs() -> dict[str, str]:
    """Every branding setting in one read, so a template does not query per field."""
    valeurs = dict(_DEFAUTS)
    try:
        lignes = db.fetch_all(
            "SELECT cle, valeur FROM integration_config WHERE cle = ANY(%s)",
            (list(_DEFAUTS),),
        )
    except Exception:  # noqa: BLE001 - branding must never break a send
        return valeurs
    for ligne in lignes:
        brut = (ligne.get("valeur") or "").strip()
        if brut:
            valeurs[str(ligne["cle"])] = brut
    return valeurs


class Marque:
    """The organisation's identity, resolved once per message."""

    __slots__ = (
        "baseline", "couleur", "couleur_sombre", "initiale", "marque",
        "nom", "nom_court", "signature", "ville",
    )

    def __init__(self) -> None:
        v = _valeurs()
        self.marque = v["org_marque"]
        self.nom = v["org_nom"]
        self.nom_court = v["org_nom_court"]
        self.ville = v["org_ville"]
        self.signature = v["org_signature"]
        self.couleur = _couleur_valide(v["org_couleur_principale"], _DEFAUTS["org_couleur_principale"])
        self.couleur_sombre = _couleur_valide(v["org_couleur_sombre"], _DEFAUTS["org_couleur_sombre"])
        self.baseline = v["org_baseline"]
        # The monogram in the header: the first letter of the brand line, which is
        # what a reader recognises at a glance.
        self.initiale = (self.marque or self.nom_court or self.nom or "A")[:1].upper()

    def pied(self) -> str:
        """The one-line footer, assembled from what the organisation actually is."""
        morceaux = [self.marque, self.baseline, self.nom]
        ligne = ", ".join(p for p in morceaux if p)
        return f"{ligne}, {self.ville}." if self.ville else f"{ligne}."

    def mention_compte(self) -> str:
        """How a message refers to the recipient's account."""
        if self.nom and self.nom != self.marque:
            return f"votre compte {self.marque} ({self.nom})"
        return f"votre compte {self.marque}"


def _couleur_valide(valeur: str, defaut: str) -> str:
    """A hex colour, or the default.

    Refused rather than passed through: the value lands inside a style attribute, so
    an arbitrary string would let whoever edits the settings inject CSS into every
    message the platform sends.
    """
    v = (valeur or "").strip()
    if len(v) in (4, 7) and v.startswith("#") and all(c in "0123456789abcdefABCDEF" for c in v[1:]):
        return v
    return defaut


def marque() -> Marque:
    """The organisation's identity for the message being built."""
    return Marque()
