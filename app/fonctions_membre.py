"""Multiple functions per member (membre_fonction), shared read/sync logic.

A member holds zero, one or several functions at once, each with its own scope
(perimetre), independently of the consecration title (Berger/Bergere). The
principal function is mirrored back to membre.fonction_cle / fonction_confirmee /
fonction_perimetre so every existing display (the compact honorific prefix, the
scan card) keeps working with no change.
"""
from __future__ import annotations

from . import db

# Berger/Bergere is a consecration title, never a function.
TITRES_INTERDITS = {"berger", "bergere", "bergère"}

_SELECT = (
    "SELECT mf.id, mf.fonction_cle, mf.perimetre, mf.confirmee, mf.actif, mf.principale, mf.ordre, "
    "fh.libelle_h AS h, fh.libelle_f AS f, fh.libelle_n AS n, fh.actif AS fh_actif, "
    "fh.categorie AS categorie, fh.abreviation AS abreviation, fh.est_vip AS est_vip "
    "FROM membre_fonction mf LEFT JOIN fonction_honorifique fh ON fh.cle = mf.fonction_cle "
    "WHERE mf.membre_id = %s"
)

# Display precedence between the four attribution categories. Lower rank wins in
# compact contexts (salutations, cards). A special function outranks a title,
# which outranks an ordinary function, which outranks a transversal one.
CATEGORIE_RANG = {
    "fonction_speciale": 0,
    "titre": 1,
    "fonction": 2,
    "fonction_particuliere": 3,
}


def label_genre(genre: object, h: object, f: object, n: object) -> str | None:
    """Gendered label of a function, from the member's civil sex."""
    if genre == "homme":
        return str(h) if h else (str(n) if n else None)
    if genre == "femme":
        return str(f) if f else (str(n) if n else None)
    return str(n) if n else (str(h) if h else None)


def _rows(membre_id: str, role: str | None) -> list[dict[str, object]]:
    return db.fetch_all(
        _SELECT + " ORDER BY mf.principale DESC, mf.ordre ASC, mf.cree_le ASC",
        (membre_id,),
        role=role,
    )


def fonctions_publiques(membre_id: str, genre: object, role: str | None) -> list[dict[str, object]]:
    """Active AND confirmed functions, gendered, ordered, for public display. A title
    deactivated at the catalogue level (fonction_honorifique.actif = false) is hidden
    even for its current holders; a function with no catalogue row (fh_actif is None)
    still shows, for backward compatibility."""
    out: list[dict[str, object]] = []
    for r in _rows(membre_id, role):
        if not r.get("actif") or not r.get("confirmee"):
            continue
        if r.get("fh_actif") is False:
            continue
        libelle = label_genre(genre, r.get("h"), r.get("f"), r.get("n")) or str(r.get("fonction_cle"))
        out.append({
            "libelle": libelle,
            "perimetre": r.get("perimetre"),
            "cle": r.get("fonction_cle"),
            "categorie": r.get("categorie") or "fonction",
            "abreviation": r.get("abreviation"),
        })
    return out


def fonctions_admin(membre_id: str, genre: object, role: str | None) -> list[dict[str, object]]:
    """Every function of a member (active or not, confirmed or not), for admin management."""
    out: list[dict[str, object]] = []
    for r in _rows(membre_id, role):
        out.append(
            {
                "id": str(r["id"]),
                "fonction_cle": r.get("fonction_cle"),
                "libelle": label_genre(genre, r.get("h"), r.get("f"), r.get("n")) or str(r.get("fonction_cle")),
                "perimetre": r.get("perimetre"),
                "confirmee": bool(r.get("confirmee")),
                "actif": bool(r.get("actif")),
                "principale": bool(r.get("principale")),
                "ordre": int(r.get("ordre") or 100),
                "categorie": r.get("categorie") or "fonction",
                "abreviation": r.get("abreviation"),
            }
        )
    return out


def sync_principale(membre_id: str, role: str | None) -> None:
    """Mirror the principal active+confirmed function back onto membre.fonction_cle
    / fonction_confirmee / fonction_perimetre, so the legacy compact display stays
    correct. When no such function remains, the compact fields are cleared."""
    primary = db.fetch_one(
        "SELECT fonction_cle, perimetre FROM membre_fonction "
        "WHERE membre_id = %s AND actif = true AND confirmee = true "
        "ORDER BY principale DESC, ordre ASC, cree_le ASC LIMIT 1",
        (membre_id,),
        role=role,
    )
    if primary:
        db.execute(
            "UPDATE membre SET fonction_cle = %s, fonction_confirmee = true, fonction_perimetre = %s WHERE id = %s",
            (primary.get("fonction_cle"), primary.get("perimetre"), membre_id),
            role=role,
        )
    else:
        db.execute(
            "UPDATE membre SET fonction_cle = NULL, fonction_confirmee = false, fonction_perimetre = NULL "
            "WHERE id = %s",
            (membre_id,),
            role=role,
        )
