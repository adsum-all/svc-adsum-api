"""The vocabulary of attendance, written once for every scope that counts.

Six modules computed a "taux de présence" over four different denominators, and one of
them counted people who had never come. Nothing said which was right, because nothing
said what the words meant.

Three axes, and they are the three questions the member is asked. Confusing two of them
is what produced a presence rate of 60,7 percent where the truth was 55,0.

============  ==========================================  ====================
Axe           Question                                    Valeurs
============  ==========================================  ====================
suivi         La personne a-t-elle suivi l'activité ?     oui, non
canal         Par quel moyen l'a-t-elle suivie ?          sur place, à distance
complétude    Son suivi à distance était-il entier ?      entier, partiel
============  ==========================================  ====================

Two rules follow from the shape and are enforced by the predicates rather than by
convention. **Partiel is a degree of following online. It is never an absence, and it
cannot accompany on site.** **Présence means the canal axis.** A person who followed
online followed; they were not present.

Every scope, whether it counts one activity, one member or a whole period, builds its
counts from these predicates. A scope that writes its own is how the six versions
appeared, so there is nothing to write: pass the alias of the consolidated row and take
the fragment.

The consolidated row must expose: ``present``, ``partiel``, ``absent``, ``scanne``,
``a_presentiel``, ``a_enligne``, and optionally ``en_ligne_complet`` and
``en_ligne_partiel``. Both consolidations in the codebase already do.
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import NamedTuple


class Axe(NamedTuple):
    code: str
    libelle: str
    question: str
    #: The values it can take, in the order a reader expects them.
    modalites: tuple[str, ...]


AXES: tuple[Axe, ...] = (
    Axe("suivi", "Le suivi", "La personne a-t-elle suivi l'activité ?", ("A suivi", "N'a pas suivi")),
    Axe("canal", "Le canal", "Par quel moyen l'a-t-elle suivie ?", ("Sur place", "À distance")),
    Axe("completude", "La complétude", "Son suivi à distance était-il entier ?", ("En entier", "En partie")),
)


def a_suivi(a: str = "cc") -> str:
    """Followed the activity, by any means. The participation axis."""
    return f"({a}.present OR {a}.partiel)"


def n_a_pas_suivi(a: str = "cc") -> str:
    """Did not follow. A partial online follow-up never lands here."""
    return f"({a}.absent AND NOT {a}.present AND NOT {a}.partiel)"


def sur_place(a: str = "cc") -> str:
    """Followed, physically present. This, and only this, is "présence"."""
    return f"({a_suivi(a)} AND {a}.a_presentiel)"


def a_distance(a: str = "cc") -> str:
    """Followed online without coming. Excludes anyone also recorded on site."""
    return f"({a_suivi(a)} AND {a}.a_enligne AND NOT {a}.a_presentiel)"


def canal_inconnu(a: str = "cc") -> str:
    """Followed, with no channel recorded. Counted apart, never split by guesswork."""
    return f"({a_suivi(a)} AND NOT {a}.a_presentiel AND NOT {a}.a_enligne)"


def prouve(a: str = "cc") -> str:
    """On site with a scan behind it. Evidence, as opposed to assertion."""
    return f"({sur_place(a)} AND {a}.scanne)"


def declare(a: str = "cc") -> str:
    """On site, asserted by the member, with no scan."""
    return f"({sur_place(a)} AND NOT {a}.scanne)"


def en_ligne_entier(a: str = "cc") -> str:
    return f"({a_distance(a)} AND {a}.en_ligne_complet)"


def en_ligne_partiel(a: str = "cc") -> str:
    return f"({a_distance(a)} AND {a}.en_ligne_partiel)"


def en_ligne_sans_degre(a: str = "cc") -> str:
    """Online, with no degree stated. Filing it as complete inflated the complete count."""
    return f"({a_distance(a)} AND NOT {a}.en_ligne_complet AND NOT {a}.en_ligne_partiel)"


def exploitable(a: str = "cc") -> str:
    """Rows the current model can express. The ambiguous legacy ones are excluded.

    "Partiel sur place" cannot be read: nobody knows whether it meant arriving late or
    following intermittently, and averaging a guess is how a dashboard lies quietly.
    """
    return f"(NOT {a}.ambigu)"


#: The complete partition, per axis. Each tuple is exhaustive and mutually exclusive
#: over its parent, which is what makes the published equalities hold rather than
#: happen to hold.
PARTITIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "suivi": (("a_suivi", "A suivi"), ("n_a_pas_suivi", "N'a pas suivi")),
    "canal": (("sur_place", "Sur place"), ("a_distance", "À distance"), ("canal_inconnu", "Canal non précisé")),
    "preuve": (("prouve", "Prouvé par un scan"), ("declare", "Déclaré par le membre")),
    "completude": (
        ("en_ligne_entier", "En entier"),
        ("en_ligne_partiel", "En partie"),
        ("en_ligne_sans_degre", "Degré non précisé"),
    ),
}

#: Resolves a partition entry to its predicate builder, so a caller iterates a partition
#: without knowing the function names.
PREDICATS = {
    "a_suivi": a_suivi,
    "n_a_pas_suivi": n_a_pas_suivi,
    "sur_place": sur_place,
    "a_distance": a_distance,
    "canal_inconnu": canal_inconnu,
    "prouve": prouve,
    "declare": declare,
    "en_ligne_entier": en_ligne_entier,
    "en_ligne_partiel": en_ligne_partiel,
    "en_ligne_sans_degre": en_ligne_sans_degre,
}


def partition_sql(axe: str, alias: str = "cc", suffixe: str = "") -> str:
    """The FILTER columns for one whole axis, ready to drop into a SELECT.

    Built here so a scope cannot accidentally omit one branch of a partition, which is
    exactly how a breakdown stops adding up to its own total.
    """
    parties = PARTITIONS[axe]
    return ", ".join(
        f"count(*) FILTER (WHERE {PREDICATS[code](alias)}{suffixe}) AS {code}" for code, _ in parties
    )
