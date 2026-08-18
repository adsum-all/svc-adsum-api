"""Les dimensions de répartition, décrites plutôt que codées.

Des fragments SQL et une table de correspondance : ce sont des données, pas de la
logique. Les sortir du module de calcul rassemble au même endroit tout ce qu'il
faut toucher quand une dimension s'ajoute, et rend le module de calcul lisible
d'un bout à l'autre.
"""
from __future__ import annotations

_REPARTITION_JOINS = (
    "JOIN membre m ON m.id = cc.membre_id "
    "LEFT JOIN commission c ON c.id = m.commission_id "
    "LEFT JOIN intendance i ON i.id = m.intendance_id "
    "LEFT JOIN coordination co ON co.id = i.coordination_id "
    "LEFT JOIN coordination cod ON cod.id = m.coordination_id "
    "LEFT JOIN tribu t ON t.id = m.tribu_id"
)

_AGE_EXPR = (
    "CASE WHEN m.date_naissance IS NULL THEN 'Non renseigne' "
    "WHEN extract(year FROM age(m.date_naissance)) < 18 THEN 'moins de 18' "
    "WHEN extract(year FROM age(m.date_naissance)) < 26 THEN '18-25' "
    "WHEN extract(year FROM age(m.date_naissance)) < 36 THEN '26-35' "
    "WHEN extract(year FROM age(m.date_naissance)) < 51 THEN '36-50' "
    "ELSE '51 et plus' END"
)

REPARTITION_DIMENSIONS: dict[str, str] = {
    "genre": "coalesce(m.genre, 'Non renseigne')",
    "tranche_age": _AGE_EXPR,
    "commission": "coalesce(c.nom, 'Sans commission')",
    "intendance": "coalesce(i.nom, 'Sans intendance')",
    "coordination": "coalesce(co.nom, cod.nom, 'Sans coordination')",
    "tribu": "coalesce(t.nom, 'Sans tribu')",
    "pays": "coalesce(m.pays, 'Non renseigne')",
    "region": "coalesce(m.region, 'Non renseigne')",
    "type_membre": "coalesce(m.type_membre, 'Non renseigne')",
    "cheminement": "coalesce(m.cheminement_pastoral, 'Non renseigne')",
}
