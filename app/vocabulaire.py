"""What an organisation calls its own units and its own responsibilities.

The platform names things the way one organisation does: coordination, intendance,
commission, tribu, berger, patriarche. Those are not neutral words. A parish speaks of
secteurs and services, a prayer group of cellules and responsables, a school of
promotions and referents. Handed this application as it stands, any of them would have
to adopt somebody else's vocabulary to use their own tools.

The tables keep their names, because renaming a schema in production buys nothing and
risks everything. What changes is what people read. Each unit and each responsibility
carries a singular, a plural and an article, because French needs all three to build a
correct sentence: "la tribu", "les tribus", "de la tribu".

The defaults are the words in use today, so nothing changes for the organisation
running now, and a new deployment fills in its own.
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import Any

from . import db

# One entry per thing an organisation names. The key is the internal name, which never
# changes; the value is what the interface shows.
TERMES: dict[str, dict[str, str]] = {
    "coordination": {"singulier": "coordination", "pluriel": "coordinations", "article": "la"},
    "intendance": {"singulier": "intendance", "pluriel": "intendances", "article": "l'"},
    "commission": {"singulier": "commission", "pluriel": "commissions", "article": "la"},
    "tribu": {"singulier": "tribu", "pluriel": "tribus", "article": "la"},
    "berger": {"singulier": "berger", "pluriel": "bergers", "article": "le"},
    "patriarche": {"singulier": "patriarche", "pluriel": "patriarches", "article": "le"},
    "membre": {"singulier": "membre", "pluriel": "membres", "article": "le"},
}

# The settings key for one facet of one term.
def cle(terme: str, facette: str) -> str:
    return f"org_mot_{terme}_{facette}"


def _toutes_les_cles() -> list[str]:
    return [cle(t, f) for t, facettes in TERMES.items() for f in facettes]


def vocabulaire(role: str | None = None) -> dict[str, dict[str, str]]:
    """Every term, with whatever the organisation has renamed."""
    mots = {t: dict(f) for t, f in TERMES.items()}
    try:
        lignes = db.fetch_all(
            "SELECT cle, valeur FROM integration_config WHERE cle = ANY(%s)",
            (_toutes_les_cles(),), role=role,
        )
    except Exception:  # noqa: BLE001 - vocabulary must never break a screen
        return mots
    for ligne in lignes:
        brut = (ligne.get("valeur") or "").strip()
        if not brut:
            continue
        nom = str(ligne["cle"])[len("org_mot_"):]
        terme, _, facette = nom.rpartition("_")
        if terme in mots and facette in mots[terme]:
            mots[terme][facette] = brut
    return mots


def rendu(role: str | None = None) -> dict[str, Any]:
    """The vocabulary as the interface consumes it, with capitalised forms ready to use.

    The capitalised form is built here rather than in each screen: a label at the top
    of a column and the same word mid-sentence are not written the same way, and
    leaving that to twenty call sites is how a table ends up with "Tribu" in one
    column and "tribu" in the next.
    """
    mots = vocabulaire(role)
    sortie: dict[str, Any] = {}
    for terme, f in mots.items():
        singulier = f["singulier"]
        pluriel = f["pluriel"]
        sortie[terme] = {
            "singulier": singulier,
            "pluriel": pluriel,
            "article": f["article"],
            "Singulier": singulier[:1].upper() + singulier[1:],
            "Pluriel": pluriel[:1].upper() + pluriel[1:],
            # "la tribu", "l'intendance": the apostrophe form takes no space.
            "avec_article": (
                f"{f['article']}{singulier}" if f["article"].endswith("'")
                else f"{f['article']} {singulier}"
            ),
        }
    return sortie
