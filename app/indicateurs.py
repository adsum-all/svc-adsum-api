"""The one place where every attendance figure is defined.

Two faults made this necessary, and both were found by the owner rather than by a test.

**"Présent" meant two different things.** The database stores ``statut = 'present'`` for
anyone who followed an activity, whichever way they followed it. A rate built on that
column and labelled "taux de présence" therefore counted people who never came: sixty
three of them, turning a physical presence of 55,0 percent into a displayed 60,7. The
word promised a room and the number described a connection.

**The same rate was computed in six modules**, over four different denominators. Nothing
guaranteed two screens agreed, and nothing said which one was right.

The model here is three independent axes, which is exactly what the member is asked.

1. **Le suivi.** Did you follow this activity? Yes or no. Nothing else.
2. **Le canal.** If yes: on site, or online. This says nothing about how much.
3. **La complétude.** If online: in full, or partly. Partial is a degree of online
   following. It is never an absence, and it can never accompany on site.

Crossing an axis with another produces a figure; confusing two axes produces a lie. A
"présence" is the canal axis. A "suivi" is the participation axis. They are not
interchangeable, and no indicator here mixes them.

Every indicator carries its definition in plain French, its formula, its numerator and
its denominator, and is served with them. A number an organisation cannot audit is a
number it should not act on.
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import NamedTuple

from . import axes_suivi as ax
from . import db
from .direction_analyse import _CONSO, _EXPLOITABLE


class Indicateur(NamedTuple):
    """One published figure, with everything needed to check it."""

    code: str
    libelle: str
    #: What it means, in the words an administrator would use.
    definition: str
    #: The SQL predicate over the consolidated rows, or "" for a derived rate.
    predicat: str
    #: Which population it is a share of. Empty for a raw count.
    base: str
    axe: str


#: The counts. Each is a predicate over one consolidated (member, activity) row, so a
#: person counted twice by the two attendance tables is still one observation.
COMPTES: tuple[Indicateur, ...] = (
    Indicateur(
        "observations", "Observations exploitables",
        "Un membre attendu à une activité, une fois. Le dénominateur de tout le reste.",
        _EXPLOITABLE, "", "base",
    ),
    Indicateur(
        "suivis", "Ont suivi l'activité",
        "Le membre déclare avoir suivi, ou son passage a été scanné. Sur place ou à distance, indifféremment.",
        f"{ax.a_suivi()} AND {_EXPLOITABLE}", "observations", "suivi",
    ),
    Indicateur(
        "absences", "N'ont pas suivi l'activité",
        "Le membre déclare ne pas avoir suivi. Un suivi partiel en ligne n'entre jamais ici.",
        f"{ax.n_a_pas_suivi()} AND {_EXPLOITABLE}", "observations", "suivi",
    ),
    Indicateur(
        "sans_trace", "Attendus, sans réponse",
        "Ni suivi, ni absence déclarée : la personne était attendue et rien n'a été "
        "enregistré. Un état à part entière, jamais réparti d'office dans l'un des "
        "deux autres.",
        f"{ax.sans_information()} AND {_EXPLOITABLE}", "observations", "suivi",
    ),
    Indicateur(
        "presentiel", "Sur place",
        "Parmi ceux qui ont suivi, ceux qui étaient physiquement présents. C'est la seule lecture du mot présence.",
        f"{ax.sur_place()} AND {_EXPLOITABLE}", "suivis", "canal",
    ),
    Indicateur(
        "en_ligne", "À distance",
        "Parmi ceux qui ont suivi, ceux qui l'ont fait en ligne sans venir sur place.",
        f"{ax.a_distance()} AND {_EXPLOITABLE}", "suivis", "canal",
    ),
    Indicateur(
        "canal_inconnu", "Suivi sans canal précisé",
        "A suivi, sans que le mode soit renseigné. Compté à part plutôt que réparti au jugé.",
        f"{ax.canal_inconnu()} AND {_EXPLOITABLE}", "suivis", "canal",
    ),
    Indicateur(
        "presentiel_prouve", "Sur place, prouvé par un scan",
        "Le passage a été scanné au contrôle. C'est une preuve, pas une déclaration.",
        f"{ax.prouve()} AND {_EXPLOITABLE}", "presentiel", "preuve",
    ),
    Indicateur(
        "presentiel_declare", "Sur place, déclaré par le membre",
        "Le membre affirme être venu, sans scan à l'appui.",
        f"{ax.declare()} AND {_EXPLOITABLE}", "presentiel", "preuve",
    ),
    Indicateur(
        "en_ligne_complet", "À distance, en entier",
        "A suivi toute l'activité à distance.",
        f"{ax.en_ligne_entier()} AND {_EXPLOITABLE}",
        "en_ligne", "completude",
    ),
    Indicateur(
        "en_ligne_partiel", "À distance, en partie",
        "N'a suivi qu'une partie de l'activité à distance. Ce n'est pas une absence : la personne a suivi.",
        f"{ax.en_ligne_partiel()} AND {_EXPLOITABLE}",
        "en_ligne", "completude",
    ),
    Indicateur(
        "en_ligne_sans_niveau", "À distance, degré non précisé",
        "A suivi à distance sans dire si c'était en entier. Compté à part.",
        f"{ax.en_ligne_sans_degre()} AND {_EXPLOITABLE}",
        "en_ligne", "completude",
    ),
    Indicateur(
        "non_interpretables", "Enregistrements non interprétables",
        "Lignes de l'ancien modèle dont le sens est perdu. Exclues de tous les taux, jamais silencieusement.",
        "cc.ambigu", "", "qualite",
    ),
)

#: Rates, each an explicit ratio between two counts above. No rate is invented here:
#: it is always a named count over a named base, so it can be recomputed by hand.
TAUX: tuple[tuple[str, str, str, str, str], ...] = (
    ("taux_suivi", "Taux de suivi", "suivis", "observations",
     "La part des membres attendus qui ont suivi l'activité, quel que soit le canal."),
    ("taux_absence", "Taux d'absence", "absences", "observations",
     "La part qui n'a pas suivi. Avec le taux de suivi, la somme fait cent."),
    ("taux_presence_physique", "Taux de présence physique", "presentiel", "observations",
     "La part des membres attendus qui sont venus sur place. C'est ce que présence veut dire."),
    ("taux_suivi_a_distance", "Taux de suivi à distance", "en_ligne", "observations",
     "La part des membres attendus qui ont suivi en ligne sans venir."),
    ("part_presentiel", "Part du présentiel dans le suivi", "presentiel", "suivis",
     "Parmi ceux qui ont suivi, la proportion venue sur place."),
    ("part_en_ligne", "Part de la distance dans le suivi", "en_ligne", "suivis",
     "Parmi ceux qui ont suivi, la proportion restée en ligne."),
    ("part_preuve", "Part du présentiel prouvé", "presentiel_prouve", "presentiel",
     "Parmi ceux venus sur place, la proportion dont le passage a été scanné."),
    ("part_partiel_en_ligne", "Part du suivi partiel en ligne", "en_ligne_partiel", "en_ligne",
     "Parmi ceux qui ont suivi à distance, la proportion qui n'a suivi qu'une partie."),
)

#: Equalities that must hold at all times. They are published with the figures: an
#: organisation should be able to see that the arithmetic closes, not be told that it does.
CONTROLES: tuple[tuple[str, tuple[str, ...], str, str], ...] = (
    # L'énoncé disait « un suivi ou une absence », et il manquait le troisième cas :
    # la personne attendue dont personne n'a rien enregistré. Quatre cent soixante-deux
    # observations tombaient dans ce trou sur la base réelle, ce qui affichait une
    # alerte d'incohérence alors que les chiffres étaient justes et l'égalité fausse.
    # Le silence est un état, pas une anomalie : le compter à part est ce qui permet
    # de le voir au lieu de le répartir d'office.
    ("suivi_absence_silence", ("suivis", "absences", "sans_trace"), "observations",
     "Chaque observation est un suivi, une absence déclarée, ou une attente restée "
     "sans réponse. Ces trois cas se partagent la population, sans recouvrement."),
    ("canaux", ("presentiel", "en_ligne", "canal_inconnu"), "suivis",
     "Tout suivi passe par un canal, ou est explicitement compté sans canal."),
    ("preuve", ("presentiel_prouve", "presentiel_declare"), "presentiel",
     "Une présence sur place est prouvée par un scan ou déclarée. Il n'y a pas de troisième cas."),
    ("completude", ("en_ligne_complet", "en_ligne_partiel", "en_ligne_sans_niveau"), "en_ligne",
     "Un suivi à distance est entier, partiel, ou de degré non précisé."),
)


def calculer(filtres_sql: str = "", params: tuple[object, ...] = ()) -> dict[str, object]:
    """Every published figure, computed in a single pass, with its own audit.

    One query, so two indicators can never describe two different instants. The filters
    are applied identically to every count, which is what makes a filtered dashboard
    internally consistent rather than merely plausible.
    """
    ou = f" AND ({filtres_sql})" if filtres_sql.strip() else ""
    colonnes = ", ".join(
        f"count(*) FILTER (WHERE {i.predicat}{ou}) AS {i.code}" for i in COMPTES
    )
    ligne = db.fetch_one(f"WITH {_CONSO} SELECT {colonnes} FROM conso cc", params) or {}
    comptes = {i.code: int(ligne.get(i.code) or 0) for i in COMPTES}

    def part(n: int, base: int) -> float | None:
        # A rate over an empty population is not zero, it is undefined. Returning zero
        # would draw a reassuring flat line where there is simply nothing to say.
        return round(100.0 * n / base, 1) if base else None

    taux = {
        code: {
            "code": code,
            "libelle": libelle,
            "definition": definition,
            "formule": f"{num} / {den}",
            "numerateur": comptes[num],
            "denominateur": comptes[den],
            "valeur": part(comptes[num], comptes[den]),
        }
        for code, libelle, num, den, definition in TAUX
    }

    controles = []
    for code, parties, attendu, enonce in CONTROLES:
        somme = sum(comptes[p] for p in parties)
        controles.append({
            "code": code,
            "enonce": enonce,
            "detail": " + ".join(f"{p} ({comptes[p]})" for p in parties) + f" = {attendu} ({comptes[attendu]})",
            "somme": somme,
            "attendu": comptes[attendu],
            "verifie": somme == comptes[attendu],
        })

    return {
        "comptes": [
            {
                "code": i.code, "libelle": i.libelle, "definition": i.definition,
                "axe": i.axe, "base": i.base, "valeur": comptes[i.code],
            }
            for i in COMPTES
        ],
        "taux": list(taux.values()),
        "controles": controles,
        "coherent": all(c["verifie"] for c in controles),
        "brut": comptes,
    }
