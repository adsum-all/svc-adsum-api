"""Request and response models (Pydantic)."""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, EmailStr, Field


def _fuseau_defaut() -> str:
    """The organisation's own time zone, resolved when a payload is built.

    Imported inside the function rather than at module scope: schemas is imported by
    almost everything, and reaching for the settings reader at import time would put
    a database module underneath the whole application's type definitions. Never
    raises, because a settings read must not be able to reject a request.
    """
    from .temps import fuseau_organisation

    return fuseau_organisation()

# Canonical UUID pattern, reused by several payloads to reject a malformed id
# with a clean 422 instead of a database error.
_UUID_RE = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


class LoginRequest(BaseModel):
    # The member signs in with an identifier: their e-mail (default), OR their ADSUM
    # matricule, OR their member code. ``email`` is kept for backward compatibility
    # (older clients) and may now carry any of the three; ``identifiant`` is the
    # explicit field for the alternative methods. One of the two must be provided.
    email: str | None = Field(default=None, max_length=200)
    identifiant: str | None = Field(default=None, max_length=200)
    password: str = Field(min_length=1, max_length=128)

    def resolve_identifiant(self) -> str:
        return (self.identifiant or self.email or "").strip()


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    doit_changer_mdp: bool = False


class LoginResponse(BaseModel):
    """Login outcome. When the second factor is required, otp_required is true and
    the token fields stay empty; the caller then verifies the code at /login-verify.
    When no code is needed (trusted device or 2FA off) the token is returned
    directly, so an admin/back-office client that only reads access_token keeps
    working unchanged."""

    otp_required: bool = False
    access_token: str | None = None
    token_type: str = "bearer"
    role: str | None = None
    doit_changer_mdp: bool = False
    # Which channel the code was sent on (telegram / email), for a clear message.
    canal: str | None = None
    # Canonical e-mail of the authenticated account. Returned only once the
    # password has been validated, so a client that signed in with a matricule or
    # member code can drive the e-mail based first-login OTP flow with the real
    # address instead of the typed identifier. Never returned on the otp_required
    # branch (no token, first factor only).
    email: str | None = None
    # Set when the code went out by e-mail to a mailbox the provider has been
    # refusing recently. The screen used to say "a code has been sent" while every
    # message bounced, so the member pressed resend and waited again. This carries
    # the reason in plain terms so they can act instead of waiting.
    alerte_email: str | None = None


class UserMe(BaseModel):
    id: str
    email: EmailStr
    role: str
    membre_id: str | None = None
    session_id: str | None = None
    # A technical/support super-admin (applicative account, not a member) with a stable,
    # server-side global-access authorization. Grants a total content bypass across apps
    # and Collaboration, WITHOUT membership. Never derived from the role or an e-mail: a
    # member super-admin has this false and stays membership-scoped for content.
    acces_technique_global: bool = False
    # Graduated privilege level of a technical account (lecteur, developpeur, mainteneur,
    # admin, super); None for a non-technical account. Governs who may administer the
    # technical-user roster and lifecycle (admin/super only) and protects the top level.
    niveau_technique: str | None = None


class FonctionPublique(BaseModel):
    """One function held by a member, shown in the dedicated function zone.

    ``categorie`` lets the client group the four attribution kinds (titre,
    fonction_speciale, fonction, fonction_particuliere) without re-deriving it.
    """

    libelle: str
    perimetre: str | None = None
    cle: str | None = None
    categorie: str = "fonction"
    abreviation: str | None = None


class MembreProfile(BaseModel):
    """Public profile of the authenticated member."""

    id: str
    matricule: str
    code_membre: str | None = None
    #: Whether the member says they hold an organisation-issued code. Null means the
    #: question was never put to them, which is where every earlier registration
    #: stands, and is distinct from having answered no.
    a_code_membre: bool | None = None
    email: EmailStr
    nom: str | None = None
    prenoms: str | None = None
    # Civil identity (single source of truth for the displayed name), kept
    # strictly separate from function and pastoral appellations.
    nom_affichage: str = ""
    nom_naissance: str | None = None
    nom_marital: str | None = None
    nom_affiche: str | None = None
    est_berger: bool = False
    nom_pastoral: str | None = None
    nom_pastoral_affiche: str | None = None
    fonction_perimetre: str | None = None
    # Shepherd SELF-DECLARATION made at registration, pending administration
    # review; distinct from the granted est_berger consecration flag.
    berger_declare: bool = False
    berger_nom_declare: str | None = None
    fonctions: list[FonctionPublique] = []
    # Resolved organisational appellation (central resolver, single precedence:
    # fonction_speciale > titre > fonction > fonction_particuliere > civil), so the
    # card and directory never re-derive the display rule client-side.
    appellation: str = ""
    appellation_formelle: str = ""
    categorie_principale: str = "civil"
    telephone: str | None = None
    indicatif_telephone: str | None = None
    whatsapp_numero: str | None = None
    groupe: str | None = None
    photo_url: str | None = None
    photo_pending: bool = False
    photo_focus_x: int | None = None
    photo_focus_y: int | None = None
    statut: str
    verifie: bool
    genre: str | None = None
    date_naissance: date | None = None
    naissance_annee_visible: bool = False
    pays: str | None = None
    region: str | None = None
    ville: str | None = None
    adresse: str | None = None
    adresse_complement: str | None = None
    date_entree: date | None = None
    cheminement_pastoral: str | None = None
    statut_administratif: str | None = None
    type_membre: str | None = None
    promotion: str | None = None
    situation_matrimoniale: str | None = None
    type_mariage: str | None = None
    # Relational journey toward marriage; only meaningful for celibataire/en_couple.
    en_cheminement: bool | None = None
    profession: str | None = None
    niveau_etudes: str | None = None
    baptise: bool | None = None
    confirme: bool | None = None
    premiere_communion: bool | None = None
    commission: str | None = None
    commission_type: str | None = None  # 'commission' | 'mission' | ... for the display prefix
    intendance: str | None = None
    intendance_id: str | None = None
    berger: str | None = None
    berger_referent_id: str | None = None
    tribu: str | None = None
    #: The colour the tribe is known by, hexadecimal, or None when the organisation
    #: has chosen none. Sent with the name because a member recognises their tribe by
    #: its colour before they read it.
    tribu_couleur: str | None = None
    tribu_id: str | None = None
    patriarche: str | None = None  # current human patriarche of the tribe, resolved (blank if none)
    coordination: str | None = None
    coordination_id: str | None = None
    # Responsables resolved from the structure the member belongs to, with a
    # gender-aware title (Intendant/Intendante, Coordinateur/Coordinatrice).
    coordinateur: str | None = None
    coordinateur_titre: str | None = None
    intendant: str | None = None
    intendant_titre: str | None = None
    champs_deverrouilles: list[str] = []
    langue: str = "fr"
    theme: str = "light"
    commission_id: str | None = None
    anniversaire_visible_annuaire: bool = True
    fonction_cle: str | None = None
    fonction_confirmee: bool = False
    titre: str | None = None


class EvenementOut(BaseModel):
    """A scheduled event visible to the member."""

    id: str
    titre: str
    type: str | None = None
    # Administrable event type from the catalogue, its display name and its unique colour
    # (used to distinguish events on the member calendar). None = no catalogue type set.
    type_evenement_id: str | None = None
    type_evenement_nom: str | None = None
    couleur: str | None = None
    volet: str
    debut: datetime
    fin: datetime | None = None
    lieu: str | None = None
    mode: str | None = None
    session_ouverte: bool
    lien_session: str | None = None
    liens: list[str] = []  # all broadcast links (one per platform), time-gated
    type_diffusion: str = "aucun"
    visibilite: str = "membres"
    # Targeting: 'general' (everyone) or a single organisational unit. cible_libelle
    # is the human-readable name of the aimed unit, for display only.
    cible_type: str = "general"
    cible_id: str | None = None
    cible_libelle: str | None = None
    cible_genre: str | None = None
    cible_age_min: int | None = None
    cible_age_max: int | None = None
    cible_emails: list[str] = []
    tags: list[dict[str, str]] = []  # catalogue tags, so members can filter the agenda
    annule: bool = False  # a cancelled activity is kept for history but never runs
    annule_motif: str | None = None
    fuseau_horaire: str = "Africa/Abidjan"  # the activity's own zone, for editing
    serie_id: str | None = None  # set when the activity belongs to a recurring series
    # Per-activity response-window override (hours after end); None = global default.
    # Surfaced so the edit form preserves it instead of silently resetting it.
    fenetre_reponse_heures: int | None = None
    fenetre_reponse_minutes: int | None = None
    # Editorial content and human contributors, edited from the back office or the
    # collaboration app and shown in the activity detail.
    description: str | None = None
    intervenant_principal: str | None = None
    intervenants: list[str] = []
    # Server-computed lifecycle (source of truth for time-gated UI actions).
    phase: str = "a_venir"  # a_venir | bientot | en_cours | a_declarer | termine
    joignable: bool = False  # the join button may show (in window and a link is available)
    formulaire_ouvert: bool = False  # the participation form may show (session started)


class PresenceOut(BaseModel):
    """One attendance record of the member, with its event title."""

    evenement_id: str
    evenement_titre: str
    debut: datetime | None = None
    arrivee: datetime | None = None
    depart: datetime | None = None
    methode: str | None = None


class NotificationOut(BaseModel):
    """A notification addressed to the member."""

    id: str
    type: str | None = None
    titre: str | None = None
    corps: str | None = None
    lu: bool
    cree_le: datetime | None = None


class ChangePasswordIn(BaseModel):
    """Change the member's own password (first login and settings)."""

    ancien: str = Field(min_length=1, max_length=128)
    nouveau: str = Field(min_length=8, max_length=128)


class EngagementAcceptIn(BaseModel):
    """Record the member's acceptance of one engagement."""

    type: str
    version: str = "v1"


class DocumentSubmitIn(BaseModel):
    """Member submission of a requested document (metadata, not the binary)."""

    type: str = "justificatif"
    libelle: str | None = None


class ParticipationIn(BaseModel):
    """Validate an online session participation, with an optional rating."""

    evenement_id: str
    note: int | None = None
    commentaire: str | None = None


class DocumentOut(BaseModel):
    """One piece of the member's verification dossier and its processing status."""

    id: str
    type: str | None = None
    statut: str
    demande_le: datetime | None = None
    recu_le: datetime | None = None
    traite_le: datetime | None = None


class EngagementOut(BaseModel):
    """An engagement the member has signed (or has yet to sign)."""

    id: str
    type: str | None = None
    version: str
    signe: bool
    signe_le: datetime | None = None


class RecensementOut(BaseModel):
    """The current annual census, and whether the member already answered."""

    id: str
    annee: int
    statut: str
    ouvert: bool
    deja_repondu: bool


class RecensementReponseIn(BaseModel):
    """The member's answer to the annual census."""

    confirme_engagement: bool
    infos_a_jour: bool
    reaccepte_engagements: bool


class QrToken(BaseModel):
    """A signed, short-lived QR token the member shows for check-in."""

    token: str
    membre_id: str
    issued_at: datetime
    expires_at: datetime
    key_version: int


_CHEMINEMENT = "^(nouveau|en_accompagnement|membre_actif|responsable|a_relancer|en_pause|ancien_membre)$"
# Engagement level: a lowercase slug governed by the admin-managed niveau_engagement
# catalogue (migration 0064), no longer a fixed enum. Rejects garbage/injection
# while accepting any level the administration creates.
_TYPE_MEMBRE = "^[a-z][a-z0-9_]{1,39}$"
_SITUATION = "^(celibataire|en_couple|fiance|marie|veuf|divorce)$"
_MARIAGE = "^(dot|religieux|dot_et_religieux|civil)$"


class MembreFields(BaseModel):
    """Shared optional member fields, used by create and update payloads."""

    nom: str | None = None
    prenoms: str | None = None
    # External member code (distinct from the app matricule): optional, uppercased,
    # loose format (letters, digits and hyphens) so real-world codes fit.
    code_membre: str | None = Field(default=None, max_length=32, pattern=r"^[A-Za-z0-9\- ]*$")
    # Whether the member says they hold such a code. Asked before the code itself,
    # because an empty column otherwise means both "not filled in yet" and "has
    # none", and the organisation cannot tell who to chase from who to wait for.
    a_code_membre: bool | None = None
    telephone: str | None = None
    commission_id: str | None = None
    groupe: str | None = None
    genre: str | None = Field(default=None, pattern="^(homme|femme|autre)$")
    date_naissance: date | None = None
    pays: str | None = None
    ville: str | None = None
    intendance_id: str | None = None
    # A member belongs to a coordination OR an intendance, never both (enforced by
    # a DB CHECK and validated in the update handler).
    coordination_id: str | None = None
    berger_referent_id: str | None = None
    date_entree: date | None = None
    cheminement_pastoral: str | None = Field(default=None, pattern=_CHEMINEMENT)
    tribu_id: str | None = None
    type_membre: str | None = Field(default=None, pattern=_TYPE_MEMBRE)
    fonction_cle: str | None = None
    promotion: str | None = None
    situation_matrimoniale: str | None = Field(default=None, pattern=_SITUATION)
    type_mariage: str | None = Field(default=None, pattern=_MARIAGE)
    en_cheminement: bool | None = None
    profession: str | None = None
    niveau_etudes: str | None = None
    baptise: bool | None = None
    confirme: bool | None = None
    premiere_communion: bool | None = None


class CreateMembre(MembreFields):
    """Payload to register a new member from the back office."""

    email: EmailStr
    matricule: str | None = None


class UpdateMembre(MembreFields):
    """Partial update of a member. Only provided fields are written."""

    statut: str | None = Field(default=None, pattern="^(actif|inactif|suspendu)$")
    verifie: bool | None = None
    # Civil identity refinements (managed by the administration).
    nom_naissance: str | None = None
    nom_marital: str | None = None
    nom_affiche: str | None = Field(default=None, pattern="^(nom|naissance|marital)$")
    est_berger: bool | None = None
    nom_pastoral: str | None = None
    berger_depuis: str | None = None
    fonction_perimetre: str | None = None
    # Governance (admin only, never returned to the member).
    appartenance: str | None = Field(default=None, pattern="^(actif|parti|retire|suspendu|archive)$")
    note_confidentielle: str | None = None


class CoordinationOut(BaseModel):
    """A coordination row. Independent by default; parent is optional.

    Carries its own descriptive and geographic identity so the administration
    understands its nature, scope and location, not just a bare name.
    """

    id: str
    nom: str
    description: str | None = None
    pays_code: str | None = None  # ISO 3166-1 alpha-2, source of truth for the country
    pays: str | None = None  # display name, kept for backward compatibility
    continent: str | None = None
    ville: str | None = None
    statut: str = "actif"  # actif | archive
    publie: bool = True
    parent_id: str | None = None
    parent: str | None = None
    responsable: str | None = None  # resolved coordinateur name (via the function)
    responsable_titre: str | None = None  # Coordinateur/Coordinatrice per gender


class CreateCoordination(BaseModel):
    nom: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    pays_code: str | None = Field(default=None, max_length=2)
    continent: str | None = Field(default=None, max_length=40)
    ville: str | None = Field(default=None, max_length=120)
    statut: str = Field(default="actif", pattern="^(actif|archive)$")
    parent_id: str | None = None


class UpdateCoordination(BaseModel):
    """Partial update of a coordination. Only provided fields are written."""

    nom: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    pays_code: str | None = Field(default=None, max_length=2)
    continent: str | None = Field(default=None, max_length=40)
    ville: str | None = Field(default=None, max_length=120)
    statut: str | None = Field(default=None, pattern="^(actif|archive)$")
    parent_id: str | None = None


class IntendanceOut(BaseModel):
    """An intendance row (geographic structure). Coordination and parent are
    both optional: an intendance is an independent structure by default."""

    id: str
    nom: str
    description: str | None = None
    pays_code: str | None = None  # ISO 3166-1 alpha-2, source of truth
    pays: str | None = None  # display name, kept for backward compatibility
    continent: str | None = None
    ville: str | None = None
    statut: str = "actif"  # actif | archive
    coordination_id: str | None = None
    coordination: str | None = None
    publie: bool = True
    parent_id: str | None = None
    parent: str | None = None
    responsable: str | None = None  # resolved intendant name (via the function)
    responsable_titre: str | None = None  # Intendant/Intendante per gender


class CreateIntendance(BaseModel):
    nom: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    pays_code: str | None = Field(default=None, max_length=2)
    pays: str | None = Field(default=None, max_length=120)
    continent: str | None = Field(default=None, max_length=40)
    ville: str | None = Field(default=None, max_length=120)
    statut: str = Field(default="actif", pattern="^(actif|archive)$")
    coordination_id: str | None = None
    parent_id: str | None = None


class UpdateIntendance(BaseModel):
    """Partial update of an intendance. Only provided fields are written."""

    nom: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    pays_code: str | None = Field(default=None, max_length=2)
    pays: str | None = Field(default=None, max_length=120)
    continent: str | None = Field(default=None, max_length=40)
    ville: str | None = Field(default=None, max_length=120)
    statut: str | None = Field(default=None, pattern="^(actif|archive)$")
    coordination_id: str | None = None
    parent_id: str | None = None


class SousCommissionOut(BaseModel):
    """A sous-commission row, with its commission name."""

    id: str
    nom: str
    commission_id: str | None = None
    commission: str | None = None
    publie: bool = True


class CreateSousCommission(BaseModel):
    nom: str = Field(min_length=1)
    commission_id: str | None = None


class BergerOut(BaseModel):
    """A user that can be set as a member shepherd (berger referent)."""

    id: str
    nom: str
    role: str


class TribuOut(BaseModel):
    """One of the twelve tribes: its biblical reference and its current human
    patriarche (resolved), at most one active per tribe."""

    id: str
    nom: str
    description: str | None = None
    publie: bool = True
    #: The colour the tribe is known by, hexadecimal, or None when the organisation
    #: has not chosen one. A member recognises their tribe by its colour before they
    #: read its name, so it travels with the name everywhere the name goes.
    couleur: str | None = None
    patriarche: str | None = None  # biblical reference (kept for context)
    patriarche_membre_id: str | None = None
    patriarche_nom: str | None = None  # current human titulaire, resolved


class SetPatriarche(BaseModel):
    """Assign (membre_id set) or revoke (membre_id null) the patriarche of a tribe."""

    membre_id: str | None = Field(default=None, pattern=_UUID_RE)
    motif: str | None = Field(default=None, max_length=300)


class UtilisateurOut(BaseModel):
    """An application account with its role, for rights management."""

    id: str
    email: EmailStr
    role: str
    actif: bool
    double_facteur: bool
    membre_id: str | None = None
    membre_nom: str | None = None
    dernier_login: datetime | None = None
    # Number of ACTIVE global group memberships. The platform roster derives from
    # this too, so a member holding only a permission-mode group (cached role stays
    # 'membre') is still listed as having platform access.
    groupes_globaux: int = 0


class CreateUtilisateur(BaseModel):
    """Payload to create an application account.

    An account is always created as a plain 'membre'. Platform access (back
    office, direction, pilotage) is never set here: it is granted only by adding
    the member to an access group, so this payload carries no role.
    """

    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    membre_id: str | None = None
    double_facteur: bool = True


class UpdateUtilisateur(BaseModel):
    """Partial update of an account: activation and 2FA only.

    A role is never written here: it is derived from the member's access groups,
    so this endpoint cannot be used to grant platform access outside a group.
    """

    actif: bool | None = None
    double_facteur: bool | None = None


class BulkCompte(BaseModel):
    """One account in a bulk creation request. Always created as a plain 'membre'."""

    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class BulkCreateUtilisateurs(BaseModel):
    """Bulk account creation: one line per person, unique email."""

    comptes: list[BulkCompte] = Field(min_length=1, max_length=1000)


class BulkResult(BaseModel):
    """Outcome of a bulk account creation."""

    crees: int
    doublons: list[str]
    erreurs: list[dict[str, str]]


class DoublonGroupe(BaseModel):
    """A group of members that look like duplicates of the same person."""

    critere: str
    valeur: str
    membres: list[MembreProfile]


class CreateComptage(BaseModel):
    """A manual counting line for a volet B (public) event."""

    evenement_id: str = Field(min_length=1)
    segment: str | None = None
    total_membres: int = Field(default=0, ge=0)
    total_anonyme: int = Field(default=0, ge=0)


class ComptageLigne(BaseModel):
    id: str
    segment: str | None = None
    total_membres: int
    total_anonyme: int
    horodatage: datetime | None = None


class ComptageResume(BaseModel):
    """Aggregated attendance for a volet B event: members + non-members."""

    evenement_id: str
    titre: str | None = None
    membres_scannes: int
    membres_comptes_manuellement: int = 0
    non_membres: int
    total_participants: int
    lignes: list[ComptageLigne]


class PublicEvent(BaseModel):
    """Minimal public information about a volet B event for self check-in."""

    id: str
    titre: str
    ouvert: bool
    lien_session: str | None = None
    type_diffusion: str = "aucun"


class AuditEntry(BaseModel):
    """One audit log entry tracing a sensitive action."""

    id: int
    acteur_role: str | None = None
    acteur_nom: str | None = None
    action: str
    objet_type: str | None = None
    objet_id: str | None = None
    horodatage: datetime | None = None


class TerminalOut(BaseModel):
    """A scan terminal registered for the control app."""

    id: str
    nom: str | None = None
    appareil_id: str | None = None
    autorise: bool
    appaire_le: datetime | None = None
    dernier_sync: datetime | None = None


class CreateTerminal(BaseModel):
    nom: str = Field(min_length=1)
    appareil_id: str = Field(min_length=1)


class UpdateTerminal(BaseModel):
    autorise: bool | None = None
    nom: str | None = None


class StatistiquesOut(BaseModel):
    """Aggregated figures for the dashboard and direction views."""

    membres_total: int
    membres_actifs: int
    membres_verifies: int
    membres_en_attente: int
    evenements_total: int
    presences_total: int
    commissions_total: int
    missions_total: int = 0
    intendances_total: int
    par_commission: list[dict[str, object]]
    par_cheminement: list[dict[str, object]]
    entrees_mensuelles: list[dict[str, object]]
    membres_a_verifier: list[dict[str, object]]
    # Distribution of activities by administrable event type (nom, couleur, total,
    # pourcentage), so the dashboard shows how activities split across types over time.
    par_type_evenement: list[dict[str, object]] = []


class CommissionOut(BaseModel):
    """A structural unit under "Commission & mission" (a commission, a mission,
    or any other kind), carrying its type so the interface can prefix the name."""

    id: str
    nom: str
    description: str | None = None
    publie: bool = True
    type_organisation: str = "commission"


class CreateCommission(BaseModel):
    """Payload to create a unit. ``type_organisation`` is a lowercase slug
    ('commission', 'mission', or a custom kind); the interface prefixes the name
    with its capitalised label."""

    nom: str = Field(min_length=1)
    description: str | None = None
    type_organisation: str = Field(default="commission", pattern="^[a-z][a-z_]{1,29}$")


class OccurrenceIn(BaseModel):
    """One additional occurrence of a recurring activity (absolute instants).

    ``mode`` lets an intermittent series vary the mode per date (e.g. some days in
    person, some online); when omitted the series' base mode applies.
    """

    debut: datetime
    fin: datetime | None = None
    mode: str | None = Field(default=None, pattern="^(presentiel|en_ligne|hybride)$")


class CreateEvenement(BaseModel):
    """Payload to create an event."""

    titre: str = Field(min_length=1)
    type: str | None = None
    # Administrable event type from the catalogue (drives the calendar colour).
    type_evenement_id: str | None = Field(default=None, pattern=_UUID_RE)
    volet: str = Field(default="A", pattern="^(A|B)$")
    debut: datetime
    fin: datetime | None = None
    lieu: str | None = None
    mode: str | None = Field(default=None, pattern="^(presentiel|en_ligne|hybride)$")
    lien_session: str | None = None
    liens: list[str] = []
    type_diffusion: str = Field(default="aucun", pattern="^(embed|externe|aucun)$")
    visibilite: str = Field(default="membres", pattern="^(public|membres|prive)$")
    # Targeting has a primary audience and optional refinements that combine (AND).
    # The primary audience is a STABLE CODE from the administrable referential
    # ``cible_activite`` (seeded with general, the four organisational units,
    # bergers, responsables, patriarches, liste; extensible without code change).
    # Only the slug shape is enforced here; existence and 'actif' status are
    # validated server-side against the referential, and the database FK plus the
    # coherence trigger guarantee integrity in depth.
    cible_type: str = Field(default="general", pattern="^[a-z][a-z0-9_]{1,39}$")
    cible_id: str | None = Field(default=None, pattern=_UUID_RE)
    # Refinements: restrict the primary audience by gender and/or age range.
    cible_genre: str | None = Field(default=None, pattern="^(homme|femme)$")
    cible_age_min: int | None = Field(default=None, ge=0, le=120)
    cible_age_max: int | None = Field(default=None, ge=0, le=120)
    # Ad-hoc audience for cible_type = 'liste': the e-mail addresses to reach.
    cible_emails: list[str] = Field(default_factory=list, max_length=500)
    # Response-window override in hours after the session end; when empty the
    # global admin parameter applies (questionnaire_fenetre_heures, default 6h).
    fenetre_reponse_heures: int | None = Field(default=None, ge=1, le=336)
    # The same window in MINUTES, which is what the interface now offers: an activity
    # running forty minutes could not be expressed while hours were the only unit.
    # Zero is allowed and means the window closes exactly at the activity's end.
    fenetre_reponse_minutes: int | None = Field(default=None, ge=0, le=20160)
    # The activity's reference IANA time zone (the zone the start/end were entered
    # in). Defaults to the organisation's own zone, read when the payload is built
    # rather than fixed here: a literal made every organisation create its activities
    # in this one's time, and members would then see every hour shifted.
    fuseau_horaire: str = Field(default_factory=_fuseau_defaut, max_length=64)
    # Recurrence: when `occurrences` is non-empty, the event becomes a SERIES. The
    # first occurrence is (debut, fin); each extra occurrence is one more real
    # activity row sharing a serie_id, so participation/questionnaire/survey keep
    # working per date. `recurrence` records the rule for display, no computation.
    occurrences: list[OccurrenceIn] = Field(default_factory=list, max_length=103)
    recurrence: dict[str, object] | None = None
    # Editorial content: a rich description (a constrained HTML subset, sanitised
    # server side) and the human contributors (main speaker plus secondary ones).
    description: str | None = Field(default=None, max_length=20000)
    intervenant_principal: str | None = Field(default=None, max_length=200)
    intervenants: list[str] = Field(default_factory=list, max_length=30)


class VerifyResult(BaseModel):
    """Outcome of verifying a member QR token."""

    valid: bool
    reason: str | None = None
    membre_id: str | None = None
    issued_at: datetime | None = None
    expires_at: datetime | None = None
    key_version: int | None = None
    matricule: str | None = None
    nom: str | None = None
    prenoms: str | None = None
    nom_affichage: str = ""
    est_berger: bool = False
    nom_pastoral_affiche: str | None = None
    photo_url: str | None = None
    titre: str | None = None
    # The state of the file behind the QR. A valid signature says the badge is
    # genuine; it says nothing about whether the organisation still counts this
    # person as a member. The controller has to see both before letting anyone in.
    verdict: str = "autorise"
    verdict_raison: str | None = None
    verdict_code: str | None = None
    statut: str | None = None
    statut_inscription: str | None = None
    identite_verifiee: bool = False


class CheckinRequest(BaseModel):
    """Payload to record a check-in from a QR token at an event."""

    token: str = Field(min_length=1)
    evenement_id: str = Field(min_length=1)


class CheckinMembre(BaseModel):
    """Minimal member identity returned with a check-in result."""

    id: str
    matricule: str
    nom: str | None = None
    prenoms: str | None = None
    nom_affichage: str = ""
    est_berger: bool = False
    nom_pastoral_affiche: str | None = None
    photo_url: str | None = None
    titre: str | None = None
    statut: str | None = None
    statut_inscription: str | None = None
    identite_verifiee: bool = False


class CheckinResult(BaseModel):
    """Outcome of a successful check-in."""

    deja_present: bool
    membre: CheckinMembre
    evenement_id: str
    arrivee: datetime | None = None
    # Warnings that did not block the check-in but that the controller must see, for
    # instance a file whose registration is not yet approved. Silence here means the
    # profile was fully conform.
    alerte: str | None = None


class VerifyRequest(BaseModel):
    """Payload to verify a QR token without recording attendance."""

    token: str = Field(min_length=1)


class ManualCheckinRequest(BaseModel):
    """Payload to record a manual check-in by member id (no QR scan)."""

    membre_id: str = Field(min_length=1)
    evenement_id: str = Field(min_length=1)


class ControlMembre(BaseModel):
    """Member directory entry cached by the controller app for offline use."""

    id: str
    matricule: str
    nom: str | None = None
    prenoms: str | None = None
    nom_affichage: str = ""
    est_berger: bool = False
    nom_pastoral_affiche: str | None = None
    commission: str | None = None
    statut: str
    titre: str | None = None


class CheckoutResult(BaseModel):
    """Outcome of recording a member departure (exit mode)."""

    membre: CheckinMembre
    evenement_id: str
    depart: datetime | None = None
    deja_sorti: bool = False


class ConsentDocSummary(BaseModel):
    """A consent document reduced to what a member needs before reading it."""

    cle: str
    version: int
    titre: str
    bloquant: bool
    ordre: int


class ConsentDocOut(BaseModel):
    """A full consent document, title and body resolved to one language."""

    cle: str
    version: int
    titre: str
    contenu: str


class ConsentDocPublishIn(BaseModel):
    """Payload to publish a new version of a consent document (admin)."""

    titre: str = Field(min_length=1)
    titre_en: str | None = None
    contenu: str = Field(min_length=1)
    contenu_en: str | None = None
    bloquant: bool = True
    ordre: int = 100


class SignatureDocRef(BaseModel):
    """One document the member intends to sign, pinned to its version."""

    cle: str = Field(min_length=1)
    version: int = Field(ge=1)


class SignatureDemandeIn(BaseModel):
    """Request a one-time code to electronically sign the listed documents."""

    documents: list[SignatureDocRef] = Field(min_length=1, max_length=50)


class SignatureVerifIn(BaseModel):
    """Confirm the electronic signature with the one-time code."""

    code: str = Field(min_length=1, max_length=12)
