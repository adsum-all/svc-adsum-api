"""The three levels of attendance must stay three nested partitions.

A partition is what makes a chart checkable: its parts do not overlap, and they add
up to the population of its level. When that stops being true, a breakdown quietly
disagrees with the headline above it and nobody can tell which figure is wrong.

These tests exercise the predicates on rows built here rather than on the database,
so they fail on the day somebody edits a predicate, not on the day the data happens
to change. Every row is a plausible consolidated row: the fixtures below are the
seven shapes the platform actually produces.
"""
from __future__ import annotations

import pytest

from app import axes_suivi as ax


class Ligne:
    """One consolidated row, as the SQL sees it."""

    def __init__(
        self, present=False, partiel=False, absent=False, scanne=False,
        a_presentiel=False, a_enligne=False,
        en_ligne_complet=False, en_ligne_partiel=False, ambigu=False,
    ):
        self.present = present
        self.partiel = partiel
        self.absent = absent
        self.scanne = scanne
        self.a_presentiel = a_presentiel
        self.a_enligne = a_enligne
        self.en_ligne_complet = en_ligne_complet
        self.en_ligne_partiel = en_ligne_partiel
        self.ambigu = ambigu


#: The seven shapes the platform produces, one per way of being counted.
POPULATION = [
    # Sur place, pointé au contrôle.
    Ligne(present=True, scanne=True, a_presentiel=True),
    # Sur place, déclaré par la personne. Même mode de suivi, source différente.
    Ligne(present=True, a_presentiel=True),
    # Distanciel, suivi en entier.
    Ligne(present=True, a_enligne=True, en_ligne_complet=True),
    # Distanciel, suivi en partie.
    Ligne(partiel=True, a_enligne=True, en_ligne_partiel=True),
    # Distanciel, degré non renseigné.
    Ligne(present=True, a_enligne=True),
    # A fait savoir qu'elle n'avait pas suivi.
    Ligne(absent=True),
    # Attendue, aucune trace.
    Ligne(),
]


def evaluer(fragment: str, ligne: Ligne) -> bool:
    """Run a SQL predicate against one row, by reading it as Python.

    The fragments are boolean algebra over column names; SQL and Python agree on
    AND, OR and NOT once the operators are spelled the same way. That makes the
    predicates testable without a database, which is what lets these tests run on
    every commit rather than only where a database is reachable.
    """
    expression = fragment.replace("cc.", "l.").replace(" AND ", " and ").replace(" OR ", " or ")
    expression = expression.replace("NOT ", "not ")
    return bool(eval(expression, {"__builtins__": {}}, {"l": ligne}))  # noqa: S307


def compter(fragment: str, population=POPULATION) -> int:
    return sum(1 for ligne in population if evaluer(fragment, ligne))


def test_niveau_1_est_une_partition_des_personnes_attendues():
    """A suivi, n'a pas suivi et sans information couvrent tout le monde, une fois."""
    total = sum(
        compter(f)
        for f in (ax.a_suivi(), ax.n_a_pas_suivi(), ax.sans_information())
    )
    assert total == len(POPULATION)


def test_niveau_1_ne_range_personne_dans_deux_groupes():
    for ligne in POPULATION:
        appartenances = sum(
            1
            for f in (ax.a_suivi(), ax.n_a_pas_suivi(), ax.sans_information())
            if evaluer(f, ligne)
        )
        assert appartenances == 1


def test_niveau_2_est_une_partition_de_ceux_qui_ont_suivi():
    """Sur place et à distance couvrent exactement les personnes ayant suivi.

    Le troisième terme, canal non enregistré, existe pour d'anciennes lignes. Il vaut
    zéro sur les données actuelles et n'est jamais affiché, mais il reste au calcul :
    l'omettre ferait que la somme des modes cesse d'égaler le nombre de suivis, ce qui
    est exactement la façon dont une ventilation cesse de tenir.
    """
    suivis = compter(ax.a_suivi())
    modes = compter(ax.sur_place()) + compter(ax.a_distance()) + compter(ax.canal_inconnu())
    assert modes == suivis


def test_niveau_3_est_une_partition_du_distanciel():
    distanciel = compter(ax.a_distance())
    degres = (
        compter(ax.en_ligne_entier())
        + compter(ax.en_ligne_partiel())
        + compter(ax.en_ligne_sans_degre())
    )
    assert degres == distanciel


def test_le_presentiel_regroupe_le_pointage_et_la_declaration():
    """Une présence est une présence. La source est une information de traçabilité.

    Les afficher comme deux catégories de participation les met au même rang que
    présentiel et distanciel, alors que l'une est une part de l'autre.
    """
    assert compter(ax.sur_place()) == 2
    assert compter(ax.prouve()) + compter(ax.declare()) == compter(ax.sur_place())


def test_une_personne_sans_trace_n_est_pas_une_absence():
    """Le silence n'est pas un fait sur quelqu'un.

    La compter absente affirmerait qu'elle n'est pas venue, ce que rien n'établit.
    La retirer du dénominateur ferait monter tous les taux sans que rien ne change
    sur le terrain.
    """
    sans_trace = Ligne()
    assert evaluer(ax.sans_information(), sans_trace)
    assert not evaluer(ax.n_a_pas_suivi(), sans_trace)
    assert not evaluer(ax.a_suivi(), sans_trace)


def test_le_suivi_partiel_n_est_jamais_une_absence():
    partielle = Ligne(partiel=True, a_enligne=True, en_ligne_partiel=True)
    assert evaluer(ax.a_suivi(), partielle)
    assert not evaluer(ax.n_a_pas_suivi(), partielle)


def test_le_distanciel_exclut_qui_est_aussi_venu():
    """Quelqu'un pointé sur place et déclarant en ligne est compté sur place, une fois."""
    les_deux = Ligne(present=True, scanne=True, a_presentiel=True, a_enligne=True)
    assert evaluer(ax.sur_place(), les_deux)
    assert not evaluer(ax.a_distance(), les_deux)


def test_la_partition_assistance_est_declaree_et_complete():
    """Le catalogue des partitions doit refléter les cinq états, sans en oublier."""
    codes = [code for code, _ in ax.PARTITIONS["assistance"]]
    assert codes == [
        "sur_place", "a_distance", "canal_inconnu", "n_a_pas_suivi", "sans_information",
    ]
    for code in codes:
        assert code in ax.PREDICATS


@pytest.mark.parametrize("axe", sorted(ax.PARTITIONS))
def test_chaque_partition_declaree_a_ses_predicats(axe):
    for code, libelle in ax.PARTITIONS[axe]:
        assert code in ax.PREDICATS, f"{axe} annonce {code} sans prédicat"
        assert libelle.strip(), f"{axe} annonce {code} sans libellé"
