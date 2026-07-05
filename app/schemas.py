"""Request and response models (Pydantic)."""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, EmailStr, Field

# Canonical UUID pattern, reused by several payloads to reject a malformed id
# with a clean 422 instead of a database error.
_UUID_RE = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    doit_changer_mdp: bool = False


class UserMe(BaseModel):
    id: str
    email: EmailStr
    role: str
    membre_id: str | None = None
    session_id: str | None = None


class FonctionPublique(BaseModel):
    """One function held by a member, shown in the dedicated function zone."""

    libelle: str
    perimetre: str | None = None


class MembreProfile(BaseModel):
    """Public profile of the authenticated member."""

    id: str
    matricule: str
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
    fonctions: list[FonctionPublique] = []
    telephone: str | None = None
    indicatif_telephone: str | None = None
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
    # Server-computed lifecycle (source of truth for time-gated UI actions).
    phase: str = "a_venir"  # a_venir | bientot | en_cours | termine
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

    ancien: str
    nouveau: str


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


class CreateUtilisateur(BaseModel):
    """Payload to create an application account.

    An account is always created as a plain 'membre'. Platform access (back
    office, direction, pilotage) is never set here: it is granted only by adding
    the member to an access group, so this payload carries no role.
    """

    email: EmailStr
    password: str = Field(min_length=8)
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
    password: str = Field(min_length=8)


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


class CreateEvenement(BaseModel):
    """Payload to create an event."""

    titre: str = Field(min_length=1)
    type: str | None = None
    volet: str = Field(default="A", pattern="^(A|B)$")
    debut: datetime
    fin: datetime | None = None
    lieu: str | None = None
    mode: str | None = Field(default=None, pattern="^(presentiel|en_ligne|hybride)$")
    lien_session: str | None = None
    liens: list[str] = []
    type_diffusion: str = Field(default="aucun", pattern="^(embed|externe|aucun)$")
    visibilite: str = Field(default="membres", pattern="^(public|membres|prive)$")
    # Targeting: 'general' reaches everyone; any other value must be paired with
    # cible_id (the id of the aimed coordination/commission/intendance/tribu).
    cible_type: str = Field(default="general", pattern="^(general|coordination|commission|intendance|tribu)$")
    cible_id: str | None = Field(default=None, pattern=_UUID_RE)
    # Response-window override in hours after the session end; when empty the
    # global admin parameter applies (questionnaire_fenetre_heures, default 6h).
    fenetre_reponse_heures: int | None = Field(default=None, ge=1, le=336)
    # The activity's reference IANA time zone (the zone the start/end were entered
    # in). Default is the base's home GMT zone; members still see their own time.
    fuseau_horaire: str = Field(default="Africa/Abidjan", max_length=64)


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


class CheckinResult(BaseModel):
    """Outcome of a successful check-in."""

    deja_present: bool
    membre: CheckinMembre
    evenement_id: str
    arrivee: datetime | None = None


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
