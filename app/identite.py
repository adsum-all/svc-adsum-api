"""Single source of truth for member identity display and normalisation.

The civil identity is kept strictly separate from functions, roles and pastoral
(Berger/Bergere) appellations. Every view (profile, member list, scan, back
office, digital card) must build the displayed name from here, never by local
string concatenation, so the rules stay consistent everywhere.

Rules:
- Family name: UPPERCASE (accents kept), hyphens and apostrophes preserved.
- Given names: title case per word (each hyphen/apostrophe part capitalised).
- Displayed civil name: ``NOM Prenom1 [Prenom2]`` (family name first, then at most
  the first two given names), so a member with many given names never produces
  an endless title. Functions and pastoral names are NEVER part of it.
"""
from __future__ import annotations

import re

_SPACES = re.compile(r"\s+")
# Word separators inside a given name that each start a new capitalised segment.
_PARTS = re.compile(r"([-'’])")
# Name particles that stay lowercase unless they open the name ("David de Jesus").
_PARTICULES = {"de", "du", "des", "la", "le", "les", "da", "di", "del", "della", "dos", "das", "van", "von", "d"}

MAX_PRENOMS_AFFICHES = 2  # NOM + up to two given names = at most three tokens.


def _collapse(value: str | None) -> str:
    return _SPACES.sub(" ", (value or "").strip())


def normaliser_nom(nom: str | None) -> str:
    """Family name in uppercase, spacing cleaned, accents/hyphens/apostrophes kept."""
    return _collapse(nom).upper()


def _cap_segment(segment: str) -> str:
    if not segment or not segment[0].isalpha():
        return segment
    return segment[0].upper() + segment[1:].lower()


def _cap_word(word: str) -> str:
    """Capitalise a word, keeping each hyphen/apostrophe part capitalised too."""
    return "".join(_cap_segment(part) for part in _PARTS.split(word))


def normaliser_prenoms(prenoms: str | None) -> str:
    """Given names / proper names in title case, particles kept lowercase.

    ``marie claire`` -> ``Marie Claire``; ``jean-paul`` -> ``Jean-Paul``;
    ``n'guessan`` -> ``N'Guessan``; ``david de jesus`` -> ``David de Jesus``.
    """
    cleaned = _collapse(prenoms)
    if not cleaned:
        return ""
    mots = cleaned.split(" ")
    out: list[str] = []
    for i, mot in enumerate(mots):
        if i > 0 and mot.lower() in _PARTICULES:
            out.append(mot.lower())
        else:
            out.append(_cap_word(mot))
    return " ".join(out)


def liste_prenoms(prenoms: str | None) -> list[str]:
    """Normalised given names as a list (order preserved)."""
    norm = normaliser_prenoms(prenoms)
    return [p for p in norm.split(" ") if p]


def nom_affichage(
    nom: str | None,
    prenoms: str | None,
    max_prenoms: int = MAX_PRENOMS_AFFICHES,
) -> str:
    """Civil display name: ``NOM Prenom1 [Prenom2]`` (family name first, capped).

    Never includes any function, role or pastoral name. Returns whatever parts
    exist (family name alone, or given names alone) rather than an empty string
    when possible.
    """
    fam = normaliser_nom(nom)
    prenoms_ok = liste_prenoms(prenoms)[: max(0, max_prenoms)]
    return " ".join(part for part in [fam, *prenoms_ok] if part).strip()


def nom_pastoral_affichage(genre: str | None, nom_pastoral: str | None) -> str | None:
    """Gendered pastoral appellation, e.g. ``Berger David`` / ``Bergere Marie``.

    Returns None when the member has no pastoral name. The label is chosen from
    the civil sex; anything other than homme/femme uses the masculine form.
    """
    pastoral = normaliser_prenoms(nom_pastoral)
    if not pastoral:
        return None
    label = "Bergère" if genre == "femme" else "Berger"
    return f"{label} {pastoral}"
