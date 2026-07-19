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
    """Civil display name: ``Prenom1 [Prenom2] NOM`` (given names first, then the
    family name in uppercase), capped so a member with many given names never
    produces an endless title.

    Never includes any function, role or pastoral name. Returns whatever parts
    exist (given names alone, or family name alone) rather than an empty string
    when possible.
    """
    fam = normaliser_nom(nom)
    prenoms_ok = liste_prenoms(prenoms)[: max(0, max_prenoms)]
    return " ".join(part for part in [*prenoms_ok, fam] if part).strip()


# Leading consecration title a name must not repeat (case and accent tolerant).
_re_prefixe_berger = re.compile(r"^\s*berg(?:er|ère|ere)\s+", re.IGNORECASE)


def nom_pastoral_affichage(genre: str | None, nom_pastoral: str | None) -> str | None:
    """Gendered pastoral appellation, e.g. ``Berger David`` / ``Bergere Marie``.

    Returns None when the member has no pastoral name. The label is chosen from
    the civil sex; anything other than homme/femme uses the masculine form.
    """
    pastoral = normaliser_prenoms(nom_pastoral)
    if not pastoral:
        return None
    # Defensive: strip any leading "Berger"/"Bergere"/"Bergere" the member (or old
    # data) may have typed, so the gendered title is never doubled ("Berger Berger X").
    pastoral = _re_prefixe_berger.sub("", pastoral).strip()
    if not pastoral:
        return None
    label = "Bergère" if genre == "femme" else "Berger"
    return f"{label} {pastoral}"


# --- Central organisational identity resolver -------------------------------
#
# A member accumulates at most one title (Berger/Bergere), plus any number of
# special functions, ordinary functions and transversal (particular) functions.
# Every display surface (notifications, birthdays, directory, QR card) must build
# the shown appellation from HERE, following one single precedence, so the rules
# never drift between screens.
#
# Display precedence (compact contexts): special function > title > function >
# particular function > civil name. A special-function holder who also has a
# title is shown as "Moderateur (Berger Christ Marie)". Titles and special
# functions are always written in full; ordinary/particular functions may use a
# configured abbreviation in compact contexts, never an arbitrary truncation.

_ORDRE_CATEGORIES = ("fonction_speciale", "titre", "fonction", "fonction_particuliere")


def _premiere(fonctions: list[dict[str, object]], categorie: str) -> dict[str, object] | None:
    """First confirmed function of a category (input is pre-ordered principale/ordre)."""
    for f in fonctions:
        if (f.get("categorie") or "fonction") == categorie:
            return f
    return None


def _avec_perimetre(base: str, perimetre: object) -> str:
    per = _collapse(str(perimetre)) if perimetre else ""
    return f"{base} ({per})" if per else base


def resoudre_identite(
    *,
    genre: str | None,
    prenoms: str | None,
    nom_civil: str,
    est_berger: bool,
    nom_pastoral: str | None,
    fonctions: list[dict[str, object]],
) -> dict[str, object]:
    """Build every display variant of a member's organisational identity.

    ``fonctions`` is the member's list of confirmed, active functions, each a dict
    with ``categorie``, ``libelle`` (already gendered), optional ``abreviation``
    and ``perimetre``, pre-ordered by principale then ordre. Pure, DB-free and
    fully testable. Returns keys:

    - ``appellation``: compact salutation form ("Moderateur (Berger X)", "Berger X",
      "Resp. Jean", first name).
    - ``formel``: full form with civil name and scope, no abbreviation.
    - ``anniversaire``: birthday label.
    - ``annuaire``: directory label.
    - ``detail``: ordered list of all attributions (title then functions with scope).
    - ``titre``: the title text if any; ``categorie_principale``: the winning category.
    """
    prenom = (liste_prenoms(prenoms) or [""])[0]
    pastoral = nom_pastoral_affichage(genre, nom_pastoral) if est_berger else None
    titre_fn = _premiere(fonctions, "titre")
    titre_text = pastoral or (str(titre_fn.get("libelle")) if titre_fn else None)

    special = _premiere(fonctions, "fonction_speciale")
    fonction = _premiere(fonctions, "fonction")
    particuliere = _premiere(fonctions, "fonction_particuliere")

    def _label_court(f: dict[str, object]) -> str:
        return str(f.get("abreviation") or f.get("libelle") or "")

    # Compact appellation (used by "Bonjour {appellation}" and short cards).
    if special:
        base = str(special.get("libelle") or "")
        appellation = f"{base} ({titre_text})" if titre_text else base
        categorie_principale = "fonction_speciale"
    elif titre_text:
        appellation = titre_text
        categorie_principale = "titre"
    elif fonction:
        appellation = f"{_label_court(fonction)} {prenom}".strip()
        categorie_principale = "fonction"
    elif particuliere:
        appellation = f"{_label_court(particuliere)} {prenom}".strip()
        categorie_principale = "fonction_particuliere"
    else:
        appellation = prenom or nom_civil
        categorie_principale = "civil"

    # Formal / directory form: full label + civil name + scope, never abbreviated.
    if special:
        inner = titre_text or nom_civil
        formel = f"{special.get('libelle')} ({inner})"
    elif titre_text:
        formel = titre_text
    elif fonction:
        formel = _avec_perimetre(f"{fonction.get('libelle')} {nom_civil}".strip(), fonction.get("perimetre"))
    elif particuliere:
        formel = _avec_perimetre(f"{particuliere.get('libelle')} {nom_civil}".strip(), particuliere.get("perimetre"))
    else:
        formel = nom_civil

    # Birthday label: special keeps the title in parentheses; functions use the
    # compact label with the full civil name; particular functions carry scope.
    if special:
        anniversaire = f"{special.get('libelle')} ({titre_text or nom_civil})"
    elif titre_text:
        anniversaire = titre_text
    elif fonction:
        anniversaire = f"{_label_court(fonction)} {nom_civil}".strip()
    elif particuliere:
        base_part = f"{_label_court(particuliere)} {nom_civil}".strip()
        anniversaire = _avec_perimetre(base_part, particuliere.get("perimetre"))
    else:
        anniversaire = nom_civil

    detail: list[str] = []
    if titre_text:
        detail.append(titre_text)
    for f in fonctions:
        cat = f.get("categorie") or "fonction"
        if cat in ("fonction_speciale", "fonction", "fonction_particuliere"):
            detail.append(_avec_perimetre(str(f.get("libelle") or ""), f.get("perimetre")))

    return {
        "appellation": appellation,
        "formel": formel,
        "anniversaire": anniversaire,
        "annuaire": formel,
        "detail": detail,
        "titre": titre_text,
        "categorie_principale": categorie_principale,
    }
