"""Which permission belongs to which application.

An access group can carry an application tag. The tag was meant to bound the right
it grants to that one application; it never did, because nothing said what a right
means for a given application. Without that referential, a right handed out inside
the collaboration workspace applied to the back office too.

This module is the missing half. The seed below is a starting point derived from the
permission domain, not a verdict: the referential lives in the database and is meant
to be adjusted from the permissions screen, because deciding that "consulter les
statistiques" belongs to Pilotage and not to Direction is an organisational choice,
not a technical one.

The back office is deliberately the exception: it is the administration console, so
every permission is exercisable there. Bounding it would mean an administrator could
no longer administer from the one place built to administer.
"""
from __future__ import annotations

# Applications a right can be exercised from. The member application is absent on
# purpose: what a member does for themselves comes from membres.self, never from an
# access group.
APPLICATIONS_PORTEUSES = ("back-office", "collaboration", "pilotage", "direction", "controleur")

# Domain to applications, beyond the back office which holds all of them.
# A domain absent from this map is exercisable from the back office only.
DOMAINES_PAR_APPLICATION: dict[str, tuple[str, ...]] = {
    # The collaboration workspace: boards, cards, channel, personal templates.
    "collaboration": ("collaboration",),
    "canal": ("collaboration",),
    "modeles": ("collaboration",),
    # Attendance control on the ground: scanning, counting, terminals.
    "controle": ("controleur",),
    "comptage": ("controleur",),
    "terminaux": ("controleur",),
    # Steering: reading the organisation and its activity within a perimeter.
    "statistiques": ("pilotage", "direction"),
    "participation": ("pilotage", "direction"),
    "evenements": ("pilotage", "direction"),
    "tags": ("pilotage",),
    "organisation": ("pilotage", "direction"),
    "commissions": ("pilotage", "direction"),
    "tribus": ("pilotage", "direction"),
    "bergers": ("pilotage", "direction"),
    "membres": ("pilotage", "direction"),
    "anniversaires": ("pilotage",),
    # Institutional communication is steered by the direction.
    "communication": ("direction",),
    "notifications": ("direction",),
}


def applications_de_la_permission(cle: str, domaine: str) -> tuple[str, ...]:
    """The applications a permission can be exercised from, back office included.

    Takes the key as well as the domain so a single permission can later be moved
    without dragging its whole domain along, which is exactly the kind of exception
    an organisation ends up needing.
    """
    del cle  # reserved for per-key exceptions; the domain decides for now
    return ("back-office", *DOMAINES_PAR_APPLICATION.get(domaine, ()))


def couples_initiaux() -> list[tuple[str, str]]:
    """Every (permission, application) pair the seed declares.

    Imported lazily by the migration and by the API so the catalogue stays the single
    source of the permission list.
    """
    from .permissions_data import CATALOGUE

    couples: list[tuple[str, str]] = []
    for cle, meta in CATALOGUE.items():
        # A self-service permission is never granted through a group, so pairing it
        # with an application would suggest an administration that cannot happen.
        if str(meta.get("portee")) == "self":
            continue
        for app in applications_de_la_permission(cle, str(meta.get("domaine") or "")):
            couples.append((cle, app))
    return couples
