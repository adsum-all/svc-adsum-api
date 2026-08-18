"""Les dimensions et les filtres du pilotage, décrits plutôt que codés.

Ce sont des données : une expression SQL par dimension, et la liste de ce qui
est filtrable. Les sortir du module de calcul rassemble au même endroit tout ce
qu'il faut toucher quand une dimension s'ajoute, et laisse le module de calcul
se lire d'un bout à l'autre.
"""
# Les fragments SQL dépassent la largeur de ligne, et les couper au milieu d'une
# expression les rend illisibles sans rien gagner. La dispense vient du module
# d'origine, d'où ce fichier a été extrait : elle le suit.
# ruff: noqa: E501
from __future__ import annotations

_INCONNU = "Non renseigné"

#: Every axis the direction may group by, and how each is expressed.
#:
#: Split in two families on purpose. A member dimension describes the person; an
#: activity dimension describes what they attended. Crossing one of each is what
#: answers "does this commission follow online or on site", which a single axis
#: cannot say.
DIMENSIONS: dict[str, dict[str, str]] = {
    # The commission table carries two kinds of unit, commission and mission, and the
    # platform's taxonomy keeps them apart. A member is attached to one of them
    # through the same column, so grouping on it mixes both: labelling that axis
    # "Commission" contradicted the organisation screen, which counts the nine
    # commissions and not the six missions. Named for what it groups, and the type is
    # offered as its own axis so the two can be told apart or crossed.
    "commission": {"expr": f"coalesce(c.nom, '{_INCONNU}')", "libelle": "Commission ou mission", "famille": "membre"},
    "type_unite": {
        "expr": (
            "CASE c.type_organisation WHEN 'commission' THEN 'Commission' "
            "WHEN 'mission' THEN 'Mission' ELSE 'Non rattaché' END"
        ),
        "libelle": "Type d'unité", "famille": "membre",
    },
    "motif_absence": {
        "expr": (
            "CASE WHEN cc.present OR cc.partiel THEN 'A suivi' "
            "     WHEN cc.absence_motif IS NULL THEN 'Absence sans motif indiqué' "
            "     ELSE coalesce((SELECT ma.libelle FROM motif_absence ma WHERE ma.code = cc.absence_motif), cc.absence_motif) END"
        ),
        "libelle": "Motif de non-suivi", "famille": "suivi",
    },
    "qualification_absence": {
        "expr": (
            "CASE WHEN cc.present OR cc.partiel THEN 'A suivi' "
            "     WHEN cc.absence_qualification = 'excusee' THEN 'Absence excusée' "
            "     WHEN cc.absence_qualification = 'non_excusee' THEN 'Absence non excusée' "
            "     WHEN cc.absence_qualification = 'en_attente' THEN 'En attente de décision' "
            "     ELSE 'Non qualifiée' END"
        ),
        "libelle": "Qualification de l'absence", "famille": "suivi",
    },
    "intendance": {"expr": f"coalesce(i.nom, '{_INCONNU}')", "libelle": "Intendance", "famille": "membre"},
    "coordination": {"expr": f"coalesce(co.nom, cod.nom, '{_INCONNU}')", "libelle": "Coordination", "famille": "membre"},
    "tribu": {"expr": f"coalesce(t.nom, '{_INCONNU}')", "libelle": "Tribu", "famille": "membre"},
    "pays": {"expr": f"coalesce(nullif(btrim(m.pays), ''), '{_INCONNU}')", "libelle": "Pays", "famille": "membre"},
    "continent": {"expr": f"coalesce(co.continent, cod.continent, '{_INCONNU}')", "libelle": "Continent", "famille": "membre"},
    "genre": {"expr": f"coalesce(nullif(btrim(m.genre), ''), '{_INCONNU}')", "libelle": "Genre", "famille": "membre"},
    "type_membre": {"expr": f"coalesce(nullif(btrim(m.type_membre), ''), '{_INCONNU}')", "libelle": "Statut de membre", "famille": "membre"},
    "tranche_age": {
        "expr": (
            "CASE WHEN m.date_naissance IS NULL THEN 'Non renseigné' "
            "WHEN extract(year FROM age(m.date_naissance)) < 18 THEN 'Moins de 18 ans' "
            "WHEN extract(year FROM age(m.date_naissance)) < 26 THEN '18 à 25 ans' "
            "WHEN extract(year FROM age(m.date_naissance)) < 36 THEN '26 à 35 ans' "
            "WHEN extract(year FROM age(m.date_naissance)) < 51 THEN '36 à 50 ans' "
            "ELSE '51 ans et plus' END"
        ),
        "libelle": "Tranche d'âge", "famille": "membre",
    },
    "volet": {"expr": f"coalesce(e.volet, '{_INCONNU}')", "libelle": "Volet", "famille": "activite"},
    "type_activite": {"expr": f"coalesce(te.nom, nullif(btrim(e.type), ''), '{_INCONNU}')", "libelle": "Type d'activité", "famille": "activite"},
    "mois": {"expr": "to_char(e.debut, 'YYYY-MM')", "libelle": "Mois", "famille": "activite"},
    # The axis that carries the whole vocabulary. Ordered from the strongest fact to
    # the weakest, and each label says what it is: a presence proven at a checkpoint
    # and one somebody typed into a form are different things, and merging them was
    # the defect that made every attendance rate unreadable.
    "modalite": {
        "expr": (
            "CASE WHEN cc.ambigu THEN 'Donnée ancienne, non interprétable' "
            "WHEN cc.scanne THEN 'Présentiel confirmé au contrôle' "
            "WHEN cc.present AND cc.a_presentiel THEN 'Présentiel déclaré' "
            "WHEN cc.a_enligne AND cc.en_ligne_partiel THEN 'En ligne, suivi partiel' "
            "WHEN cc.a_enligne THEN 'En ligne, suivi complet' "
            "WHEN cc.present OR cc.partiel THEN 'Suivi, modalité non précisée' "
            "ELSE 'N''a pas suivi' END"
        ),
        "libelle": "Modalité de suivi", "famille": "activite",
    },
    # What the figure rests on, which a reader has to know before acting on it.
    "confiance": {
        "expr": (
            "CASE WHEN cc.ambigu THEN 'Non interprétable' "
            "WHEN cc.scanne THEN 'Prouvée au contrôle' "
            "WHEN cc.present OR cc.partiel THEN 'Déclarée par le membre' "
            "ELSE 'Sans objet' END"
        ),
        "libelle": "Fiabilité de la donnée", "famille": "activite",
    },
}

#: Filters the direction may narrow on. Each is a fragment plus the parameter it
#: binds; nothing here is built from a caller's string.
_FILTRES: dict[str, str] = {
    "coordination": "coalesce(co.id, cod.id) = %(f_coordination)s",
    "intendance": "i.id = %(f_intendance)s",
    "commission": "c.id = %(f_commission)s",
    "tribu": "t.id = %(f_tribu)s",
    "volet": "e.volet = %(f_volet)s",
    "evenement": "e.id = %(f_evenement)s",
    "pays": "m.pays = %(f_pays)s",
    "genre": "m.genre = %(f_genre)s",
    "type_membre": "m.type_membre = %(f_type_membre)s",
    "depuis": "e.debut >= %(f_depuis)s::timestamptz",
    "jusqu_a": "e.debut <= %(f_jusqu_a)s::timestamptz",
}

#: What makes a member a demonstration profile. Same predicate as the seed script, so
#: the two cannot drift: a profile the seed can touch is a profile this can exclude.
#: The accent is folded because the family names were normalised to DEMO.
_EST_DEMO = (
    "(translate(upper(m.nom), 'ÉÈÊË', 'EEEE') LIKE '%%DEMO%%' "
    "OR m.email LIKE '%%@exemple.com' OR m.email LIKE '%%@example.com')"
)

#: Filters whose value selects a fixed fragment rather than binding a parameter.
#:
#: The demonstration switch exists because the base is largely populated with
#: demonstration profiles while the organisation onboards. A direction reading a
#: credible attendance rate has no way to know it describes invented people, and
#: "toutes" stays the default only so that no figure changes silently under somebody
#: who did not ask for it.
_FILTRES_ENUMERES: dict[str, dict[str, str]] = {
    "donnees": {
        "toutes": "true",
        "reelles": f"NOT {_EST_DEMO}",
        "demonstration": _EST_DEMO,
    },
}
