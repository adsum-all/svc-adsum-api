"""One consolidated attendance record per member, for one event.

Two tables record that someone took part: ``presence``, written by the on-site
scanner, and ``participation``, written by a declaration or by the online link. A
member can appear in both, and used to be counted twice. This module holds the
single SQL vocabulary that reconciles them, so every dashboard counts the same
thing by construction rather than by convention.

It lives apart from the queries that consume it because it is a definition, not a
computation: what a presence *is* changes for reasons that have nothing to do with
how a given screen aggregates it.
"""
from __future__ import annotations

from .visibilite import CIBLE_PREDICATE

# A self-declared participation is counted only when scanned or validated. Kept
# identical to app.participation._COMPTE so the two modules never diverge.
COMPTE = "(source = 'scan' OR valide)"

# The targeted active population of an event: the denominator of every rate. It is
# the SAME population the event actually reaches (agenda visibility, J-1 reminders,
# survey audience), so a targeted activity is measured against the members it aims
# at, not the whole base. Requires the outer query to bind %(ev)s to the event id.
POPULATION_CIBLE_CTE = f"""
cible AS (
    SELECT m.id AS membre_id
    FROM membre m
    LEFT JOIN intendance mi ON mi.id = m.intendance_id
    JOIN evenement e ON e.id = %(ev)s
    WHERE m.statut = 'actif' AND {CIBLE_PREDICATE}
)
"""

# Consolidated, deduplicated attendance for one event: one row per member across
# BOTH tables, with the strongest status kept and the modality resolved. A scan is
# on-site proof; an online-link participation is online; a declaration carries its
# own modality (or unknown). ``dans_cible`` flags whether the member belongs to the
# targeted active population, so the caller can bound numerators to it.
CONSO_CTES = f"""
{POPULATION_CIBLE_CTE},
brut AS (
    SELECT pr.membre_id,
           CASE WHEN pr.mode = 'en_ligne' THEN 'en_ligne' ELSE 'presentiel' END AS modalite,
           'present'::text AS statut,
           (pr.methode IN ('qr', 'manuelle')) AS scanne,
           NULL::text AS niveau_en_ligne,
           false AS ambigu
    FROM presence pr
    WHERE pr.evenement_id = %(ev)s
    UNION ALL
    SELECT pa.membre_id,
           CASE WHEN pa.source = 'scan' THEN 'presentiel'
                WHEN pa.modalite IN ('presentiel', 'en_ligne') THEN pa.modalite
                ELSE NULL END AS modalite,
           pa.statut,
           (pa.source = 'scan') AS scanne,
           pa.niveau_en_ligne,
           coalesce(pa.legacy_ambigu, false) AS ambigu
    FROM participation pa
    WHERE pa.evenement_id = %(ev)s AND {COMPTE}
      AND pa.statut IN ('present', 'partiel', 'absent')
),
conso AS (
    SELECT membre_id,
           bool_or(statut = 'present') AS present,
           bool_or(statut = 'partiel') AS partiel,
           bool_or(statut = 'absent') AS absent,
           coalesce(bool_or(scanne), false) AS scanne,
           -- coalesce to false: a member whose only record has an unknown modality
           -- (NULL) would otherwise yield NULL and fall into no modality bucket, so
           -- the split would not sum back to the present total. NULL means "unknown",
           -- which is the "modalite_inconnue" bucket, i.e. a_presentiel = a_enligne = false.
           coalesce(bool_or(modalite = 'presentiel'), false) AS a_presentiel,
           coalesce(bool_or(modalite = 'en_ligne'), false) AS a_enligne,
           coalesce(bool_or(niveau_en_ligne = 'complet'), false) AS en_ligne_complet,
           coalesce(bool_or(niveau_en_ligne = 'partiel'), false) AS en_ligne_partiel,
           coalesce(bool_or(ambigu), false) AS ambigu
    FROM brut
    GROUP BY membre_id
),
conso_cible AS (
    SELECT c.*, (ci.membre_id IS NOT NULL) AS dans_cible
    FROM conso c
    LEFT JOIN cible ci ON ci.membre_id = c.membre_id
)
"""
