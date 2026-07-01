"""Seed the four blocking consent documents of the Sacerdoce Royal.

Inserts the initial (version 1) bilingual documents into ``document_consentement``
with ``ON CONFLICT (cle, version) DO NOTHING``, so the script is idempotent and
never overwrites a version the administration may have edited. The documents are
real, professional content that the administration can revise later through the
admin endpoint (which publishes a new version and keeps this one as evidence).

Run once, out of band, with the same DSN as the API:

    .venv/Scripts/python.exe scripts/seed_consent_docs.py
"""
# ruff: noqa: E501
from __future__ import annotations

import json

import psycopg

s = json.load(open(r"C:/Users/kouas/Documents/deepl-test/95-sr-adsum/.secret/supabase-secret-adsum.json"))["supabase"]
DSN = f"postgresql://postgres.{s['project_id']}:{s['db_password']}@aws-0-{s.get('region', 'eu-west-3')}.pooler.supabase.com:5432/postgres?sslmode=require"


# --- Document 1: data protection (RGPD) -------------------------------------

RGPD_TITRE = "Protection des données personnelles (RGPD)"
RGPD_TITRE_EN = "Personal Data Protection (GDPR)"
RGPD_FR = """Le Sacerdoce Royal accorde une importance essentielle à la protection de vos données personnelles. La présente notice vous informe, conformément au Règlement général sur la protection des données (RGPD), de la manière dont vos données sont collectées et utilisées.

Finalités. Vos données sont traitées pour trois finalités précises : la gestion de votre adhésion et de votre parcours de membre, la tenue de l'annuaire interne de la communauté, et les communications pastorales et administratives de l'apostolat (convocations, événements, accompagnement spirituel).

Base légale. Le traitement repose sur votre consentement, que vous exprimez en signant le présent document, et sur l'intérêt légitime de l'apostolat à organiser sa vie communautaire et à accompagner ses membres.

Destinataires. Vos données sont exclusivement destinées à l'administration du Sacerdoce Royal et aux responsables habilités de la communauté. Elles ne font l'objet d'aucune cession, location ou transfert à des tiers à des fins commerciales.

Durée de conservation. Vos données sont conservées pendant toute la durée de votre adhésion, puis archivées ou supprimées dans un délai raisonnable après votre départ, sauf obligation légale de conservation plus longue.

Vos droits. Vous disposez d'un droit d'accès, de rectification, d'effacement, de limitation et d'opposition sur vos données. Vous pouvez exercer ces droits directement depuis votre espace membre ou en contactant l'administration.

Sécurité. L'apostolat met en œuvre des mesures techniques et organisationnelles appropriées (contrôle des accès, chiffrement, journalisation des opérations sensibles) afin de protéger vos données contre tout accès non autorisé, perte ou divulgation.

En signant, vous reconnaissez avoir lu et compris cette notice et consentez au traitement de vos données pour les finalités décrites."""
RGPD_EN = """The Sacerdoce Royal attaches essential importance to the protection of your personal data. This notice informs you, in accordance with the General Data Protection Regulation (GDPR), of how your data is collected and used.

Purposes. Your data is processed for three specific purposes: managing your membership and your journey as a member, maintaining the community's internal directory, and the pastoral and administrative communications of the apostolate (convocations, events, spiritual accompaniment).

Legal basis. Processing relies on your consent, expressed by signing this document, and on the legitimate interest of the apostolate in organising its community life and accompanying its members.

Recipients. Your data is intended solely for the administration of the Sacerdoce Royal and the community's authorised officers. It is never sold, rented or transferred to third parties for commercial purposes.

Retention. Your data is kept for the entire duration of your membership, then archived or deleted within a reasonable period after your departure, unless a longer retention is required by law.

Your rights. You have the right to access, rectify, erase, restrict and object to the processing of your data. You may exercise these rights directly from your member space or by contacting the administration.

Security. The apostolate implements appropriate technical and organisational measures (access control, encryption, logging of sensitive operations) to protect your data against any unauthorised access, loss or disclosure.

By signing, you acknowledge that you have read and understood this notice and consent to the processing of your data for the purposes described."""


# --- Document 2: confidentiality charter ------------------------------------

CONF_TITRE = "Charte de confidentialité"
CONF_TITRE_EN = "Confidentiality Charter"
CONF_FR = """La vie communautaire du Sacerdoce Royal repose sur la confiance mutuelle entre ses membres. Cette confiance suppose le respect strict de la confidentialité des informations partagées au sein de la communauté.

Engagement de discrétion. En signant cette charte, vous vous engagez à ne pas divulguer, communiquer ni exploiter les informations personnelles concernant les autres membres dont vous pourriez avoir connaissance dans le cadre de votre appartenance à l'apostolat. Cela concerne notamment les coordonnées, les situations familiales, professionnelles ou spirituelles, ainsi que toute confidence reçue.

Usage de l'annuaire. L'annuaire interne et les outils de la communauté vous sont donnés pour faciliter la vie fraternelle et l'entraide. Vous vous engagez à en faire un usage strictement interne et personnel, et à ne jamais les reproduire, extraire ou transmettre à des personnes extérieures à la communauté.

Réseaux sociaux et messagerie. Vous vous abstenez de publier ou de partager, sur les réseaux sociaux ou par messagerie, des informations, des photographies ou des échanges relatifs à d'autres membres sans leur accord explicite.

Situations sensibles. Les informations relatives à l'accompagnement spirituel, à la santé ou aux difficultés personnelles d'un membre relèvent d'une confidentialité renforcée. Elles ne doivent en aucun cas être relayées.

Durée. Cet engagement de confidentialité demeure valable pendant toute la durée de votre adhésion et se poursuit après votre départ éventuel de la communauté.

Manquement. Tout manquement grave à cette charte peut donner lieu à une mesure disciplinaire, sans préjudice des recours prévus par la loi.

En signant, vous reconnaissez l'importance de la confidentialité et vous engagez à la respecter pleinement."""
CONF_EN = """The community life of the Sacerdoce Royal rests on the mutual trust between its members. This trust requires the strict respect of the confidentiality of the information shared within the community.

Commitment to discretion. By signing this charter, you undertake not to disclose, communicate or exploit the personal information concerning other members that you may become aware of through your membership in the apostolate. This includes, in particular, contact details, family, professional or spiritual situations, and any confidence received.

Use of the directory. The internal directory and the community's tools are given to you to facilitate fraternal life and mutual aid. You undertake to use them strictly internally and personally, and never to reproduce, extract or forward them to persons outside the community.

Social media and messaging. You refrain from publishing or sharing, on social media or through messaging, any information, photographs or exchanges relating to other members without their explicit consent.

Sensitive situations. Information relating to a member's spiritual accompaniment, health or personal difficulties is subject to reinforced confidentiality. It must under no circumstances be relayed.

Duration. This confidentiality commitment remains valid for the entire duration of your membership and continues after any departure from the community.

Breach. Any serious breach of this charter may give rise to a disciplinary measure, without prejudice to the remedies provided for by law.

By signing, you acknowledge the importance of confidentiality and undertake to respect it fully."""


# --- Document 3: member engagement letter -----------------------------------

ENG_TITRE = "Lettre d'engagement du membre"
ENG_TITRE_EN = "Member Engagement Letter"
ENG_FR = """En demandant mon adhésion au Sacerdoce Royal, je prends librement l'engagement spirituel et communautaire qui suit, conscient de la mission d'évangélisation de cet apostolat.

Engagement spirituel. Je m'engage à nourrir ma vie de foi par la prière, la participation aux sacrements et la vie liturgique, et à laisser la Parole de Dieu orienter mes choix. J'accueille l'accompagnement spirituel qui me sera proposé comme une aide à ma croissance.

Engagement communautaire. Je m'engage à prendre part activement à la vie de la communauté : rencontres, formations, temps de prière et actions d'évangélisation, dans la mesure de mes possibilités et de mon état de vie. Je veille à la communion fraternelle, au respect et au soutien de mes frères et sœurs.

Témoignage. Je m'efforce de témoigner de l'Évangile par ma vie personnelle, familiale et professionnelle, et de porter le souci des personnes éloignées de la foi, dans un esprit de service et d'humilité.

Fidélité et persévérance. Je reconnais que cet engagement demande de la constance. Je m'engage à le vivre dans la durée, à demander pardon lorsque je faillis, et à me laisser reprendre fraternellement.

Liberté. Je prends cet engagement librement, sans contrainte, et je sais que je pourrai à tout moment faire part à l'administration de mes difficultés ou de mon souhait de me retirer.

En signant cette lettre, je confirme mon désir sincère de servir le Christ au sein du Sacerdoce Royal et d'y vivre ma vocation de baptisé."""
ENG_EN = """In requesting my membership in the Sacerdoce Royal, I freely make the following spiritual and community commitment, aware of the evangelising mission of this apostolate.

Spiritual commitment. I undertake to nourish my life of faith through prayer, participation in the sacraments and liturgical life, and to let the Word of God guide my choices. I welcome the spiritual accompaniment that will be offered to me as a help to my growth.

Community commitment. I undertake to take an active part in the life of the community: gatherings, formation, times of prayer and evangelisation activities, according to my possibilities and my state of life. I care for fraternal communion, respect and the support of my brothers and sisters.

Witness. I strive to bear witness to the Gospel through my personal, family and professional life, and to carry a concern for those who are far from the faith, in a spirit of service and humility.

Faithfulness and perseverance. I recognise that this commitment requires constancy. I undertake to live it over time, to ask forgiveness when I fail, and to let myself be corrected in a fraternal spirit.

Freedom. I make this commitment freely, without constraint, and I know that I may at any time share with the administration my difficulties or my wish to withdraw.

By signing this letter, I confirm my sincere desire to serve Christ within the Sacerdoce Royal and to live there my vocation as a baptised person."""


# --- Document 4: internal rules ---------------------------------------------

REG_TITRE = "Règlement intérieur"
REG_TITRE_EN = "Internal Rules"
REG_FR = """Le présent règlement intérieur précise les règles de vie qui organisent la communauté du Sacerdoce Royal. Chaque membre s'engage à les connaître et à les respecter.

Règles de vie. Le membre adopte une conduite conforme à l'esprit de l'Évangile et aux valeurs de l'apostolat : charité, honnêteté, respect de la personne et recherche de la paix. Il évite tout comportement, propos ou publication susceptible de porter atteinte à la dignité d'autrui ou à la réputation de la communauté.

Participation. Le membre participe, selon son état de vie, aux rencontres, formations, temps de prière et activités de la communauté. Il prévient les responsables en cas d'empêchement et s'efforce d'honorer les engagements pris.

Respect des responsables et des décisions. Le membre reconnaît l'autorité des responsables légitimement établis et se conforme aux décisions prises pour le bien commun, tout en pouvant exprimer son avis dans un esprit constructif et fraternel.

Usage des biens et des outils. Le membre fait un usage soigneux et loyal des biens, des locaux et des outils numériques mis à disposition. Il respecte les règles de sécurité et de confidentialité qui s'y appliquent.

Discipline. Les manquements aux présentes règles sont traités avec discernement et charité. Selon leur gravité, ils peuvent donner lieu à un rappel fraternel, à un entretien, à une mesure d'accompagnement ou, en dernier recours, à une suspension ou à une exclusion décidée par l'administration.

Évolution du règlement. Ce règlement peut être révisé par l'administration pour tenir compte de la vie de la communauté. Les membres sont informés de toute modification.

En signant, je déclare avoir pris connaissance du règlement intérieur et m'engage à le respecter."""
REG_EN = """These internal rules set out the rules of life that organise the community of the Sacerdoce Royal. Each member undertakes to know and respect them.

Rules of life. The member adopts conduct consistent with the spirit of the Gospel and the values of the apostolate: charity, honesty, respect for the person and the pursuit of peace. The member avoids any behaviour, remark or publication likely to harm the dignity of others or the reputation of the community.

Participation. The member takes part, according to their state of life, in the gatherings, formation, times of prayer and activities of the community. The member informs the officers in case of impediment and strives to honour the commitments made.

Respect for officers and decisions. The member recognises the authority of the legitimately established officers and complies with the decisions taken for the common good, while being able to express an opinion in a constructive and fraternal spirit.

Use of property and tools. The member makes careful and loyal use of the property, premises and digital tools made available. The member respects the security and confidentiality rules that apply to them.

Discipline. Breaches of these rules are handled with discernment and charity. Depending on their seriousness, they may give rise to a fraternal reminder, an interview, an accompaniment measure or, as a last resort, a suspension or exclusion decided by the administration.

Evolution of the rules. These rules may be revised by the administration to reflect the life of the community. Members are informed of any change.

By signing, I declare that I have read the internal rules and undertake to respect them."""


DOCUMENTS = [
    ("consentement_rgpd", RGPD_TITRE, RGPD_TITRE_EN, RGPD_FR, RGPD_EN, True, 10),
    ("confidentialite", CONF_TITRE, CONF_TITRE_EN, CONF_FR, CONF_EN, True, 20),
    ("lettre_engagement", ENG_TITRE, ENG_TITRE_EN, ENG_FR, ENG_EN, True, 30),
    ("reglement_interieur", REG_TITRE, REG_TITRE_EN, REG_FR, REG_EN, True, 40),
]


def main() -> None:
    inserted = 0
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        for cle, titre, titre_en, contenu, contenu_en, bloquant, ordre in DOCUMENTS:
            cur.execute(
                "INSERT INTO document_consentement (cle, version, titre, titre_en, contenu, contenu_en, bloquant, ordre) "
                "VALUES (%s, 1, %s, %s, %s, %s, %s, %s) ON CONFLICT (cle, version) DO NOTHING",
                (cle, titre, titre_en, contenu, contenu_en, bloquant, ordre),
            )
            inserted += cur.rowcount
        conn.commit()
    print(f"seed_consent_docs: {inserted} document(s) inserted, {len(DOCUMENTS) - inserted} already present")


if __name__ == "__main__":
    main()
