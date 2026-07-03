"""Static catalog and state machine of the member request (ticket) domain.

Split out of demandes.py to keep each module under the size policy. Pure data:
the guided request catalog shown to members, the readable status labels and the
controlled status transitions enforced by the endpoints.
"""
# ruff: noqa: E501 - prewritten member-facing French messages are kept on one line.
from __future__ import annotations

# Controlled state machine of a ticket. Keys are current states, values the
# reachable states. Any other jump is rejected with a clear error.
STATUTS_LISIBLES = {
    "ouverte": "Ouverte",
    "en_cours": "En cours de traitement",
    "pieces_demandees": "Pièces demandées",
    "attente_membre": "En attente de votre réponse",
    "en_validation": "En validation",
    "resolue": "Résolue",
    "refusee": "Refusée",
}
_TRANSITIONS: dict[str, set[str]] = {
    "ouverte": {"en_cours", "pieces_demandees", "attente_membre", "resolue", "refusee"},
    "en_cours": {"pieces_demandees", "attente_membre", "en_validation", "resolue", "refusee"},
    "pieces_demandees": {"en_cours", "en_validation", "resolue", "refusee"},
    "attente_membre": {"en_cours", "resolue", "refusee"},
    "en_validation": {"en_cours", "resolue", "refusee"},
    # Reopening a closed ticket is an explicit admin decision.
    "resolue": {"en_cours"},
    "refusee": {"en_cours"},
}


# --- Structured request catalog ---------------------------------------------
# Grounded in the real member-facing features (profile, identity documents,
# attachment to commissions/tribes, card and QR, activities and presence,
# notifications and language, account and security). Every category keeps an
# "autre" entry so no need is ever blocked. Messages are prewritten so members
# never have to compose from scratch; identity is attached server-side.
CATALOGUE: list[dict[str, object]] = [
    {"categorie": "profil", "libelle": "Mon profil et mon identité", "sous": [
        {"cle": "correction_nom", "libelle": "Corriger mon nom ou mes prénoms", "sujet": "Correction de mon nom ou de mes prénoms",
         "message": "Bonjour, je souhaite faire corriger mon nom ou mes prénoms sur mon profil. Voici la correction attendue : ", "piece": "recommandée"},
        {"cle": "correction_naissance", "libelle": "Corriger ma date de naissance", "sujet": "Correction de ma date de naissance",
         "message": "Bonjour, ma date de naissance est incorrecte sur mon profil. La date exacte est : ", "piece": "recommandée"},
        {"cle": "photo", "libelle": "Changer ma photo d'identité", "sujet": "Changement de ma photo d'identité",
         "message": "Bonjour, je souhaite mettre à jour ma photo d'identité. Merci de m'indiquer la marche à suivre ou de débloquer le remplacement.", "piece": "facultative"},
        {"cle": "autre", "libelle": "Autre demande sur mon profil", "sujet": "Demande concernant mon profil",
         "message": "Bonjour, j'ai une demande concernant mon profil : ", "piece": "facultative"},
    ]},
    {"categorie": "coordonnees", "libelle": "Mes coordonnées", "sous": [
        {"cle": "telephone", "libelle": "Mettre à jour mon téléphone", "sujet": "Mise à jour de mon numéro de téléphone",
         "message": "Bonjour, mon numéro de téléphone a changé. Le nouveau numéro est : ", "piece": "facultative"},
        {"cle": "adresse", "libelle": "Mettre à jour ma ville ou mon adresse", "sujet": "Mise à jour de ma localisation",
         "message": "Bonjour, ma localisation a changé. Ma nouvelle ville (et mon quartier, si utile) est : ", "piece": "facultative"},
        {"cle": "email", "libelle": "Changer mon adresse e-mail", "sujet": "Changement de mon adresse e-mail",
         "message": "Bonjour, je souhaite changer l'adresse e-mail de mon compte. La nouvelle adresse est : ", "piece": "facultative"},
        {"cle": "autre", "libelle": "Autre demande sur mes coordonnées", "sujet": "Demande concernant mes coordonnées",
         "message": "Bonjour, j'ai une demande concernant mes coordonnées : ", "piece": "facultative"},
    ]},
    {"categorie": "rattachement", "libelle": "Commission, tribu, intendance", "sous": [
        {"cle": "commission", "libelle": "Changer de commission", "sujet": "Demande de changement de commission",
         "message": "Bonjour, je souhaite changer de commission. Commission souhaitée et raison : ", "piece": "facultative"},
        {"cle": "tribu", "libelle": "Corriger ma tribu", "sujet": "Correction de ma tribu",
         "message": "Bonjour, ma tribu n'est pas correcte sur mon profil. Ma tribu est : ", "piece": "facultative"},
        {"cle": "intendance", "libelle": "Changer d'intendance ou de groupe", "sujet": "Changement d'intendance ou de groupe",
         "message": "Bonjour, je souhaite être rattaché(e) à une autre intendance ou un autre groupe : ", "piece": "facultative"},
        {"cle": "autre", "libelle": "Autre demande de rattachement", "sujet": "Demande concernant mon rattachement",
         "message": "Bonjour, j'ai une demande concernant mon rattachement : ", "piece": "facultative"},
    ]},
    {"categorie": "documents", "libelle": "Documents et pièces", "sous": [
        {"cle": "remplacer_piece", "libelle": "Remplacer ma pièce d'identité", "sujet": "Remplacement de ma pièce d'identité",
         "message": "Bonjour, je souhaite remplacer la pièce d'identité fournie lors de mon inscription (nouvelle pièce, renouvellement ou meilleure qualité).", "piece": "recommandée"},
        {"cle": "attestation", "libelle": "Question sur mon attestation signée", "sujet": "Question sur mon attestation d'engagement",
         "message": "Bonjour, j'ai une question concernant mon attestation d'engagement signée : ", "piece": "facultative"},
        {"cle": "autre", "libelle": "Autre demande sur mes documents", "sujet": "Demande concernant mes documents",
         "message": "Bonjour, j'ai une demande concernant mes documents : ", "piece": "facultative"},
    ]},
    {"categorie": "carte", "libelle": "Ma carte et mon QR", "sous": [
        {"cle": "qr", "libelle": "Problème avec mon QR ou ma carte", "sujet": "Problème avec ma carte ou mon QR",
         "message": "Bonjour, je rencontre un problème avec ma carte de membre ou mon QR (affichage, scan refusé...). Voici ce qui se passe : ", "piece": "facultative"},
        {"cle": "autre", "libelle": "Autre demande sur ma carte", "sujet": "Demande concernant ma carte",
         "message": "Bonjour, j'ai une demande concernant ma carte de membre : ", "piece": "facultative"},
    ]},
    {"categorie": "activites", "libelle": "Activités et présences", "sous": [
        {"cle": "presence", "libelle": "Corriger une présence manquante", "sujet": "Correction d'une présence",
         "message": "Bonjour, j'ai participé à une activité mais ma présence n'apparaît pas dans mon historique. Activité et date : ", "piece": "facultative"},
        {"cle": "autre", "libelle": "Autre demande sur les activités", "sujet": "Demande concernant les activités",
         "message": "Bonjour, j'ai une demande concernant les activités : ", "piece": "facultative"},
    ]},
    {"categorie": "compte", "libelle": "Compte, sécurité et notifications", "sous": [
        {"cle": "connexion", "libelle": "Problème de connexion", "sujet": "Problème de connexion à mon compte",
         "message": "Bonjour, je rencontre un problème pour me connecter à mon compte. Voici ce qui se passe : ", "piece": "facultative"},
        {"cle": "notifications", "libelle": "Notifications ou langue", "sujet": "Demande sur mes notifications ou ma langue",
         "message": "Bonjour, j'ai une demande concernant mes notifications (canaux, fréquence) ou la langue de mon compte : ", "piece": "facultative"},
        {"cle": "autre", "libelle": "Autre demande sur mon compte", "sujet": "Demande concernant mon compte",
         "message": "Bonjour, j'ai une demande concernant mon compte : ", "piece": "facultative"},
    ]},
    {"categorie": "autre", "libelle": "Autre demande", "sous": [
        {"cle": "autre", "libelle": "Demande libre", "sujet": "Demande à l'administration",
         "message": "Bonjour, je souhaite contacter l'administration au sujet suivant : ", "piece": "facultative"},
    ]},
]
