"""Field control endpoints (QR verify and check-in), on the real PostgreSQL.

Reserved to staff roles that operate attendance. Tokens are verified offline
with the published Ed25519 public key, then attendance is written under the
caller role so the per-role RLS policies (ADR-0002) still apply.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from . import controle_eligibilite, db, identite
from .mappers import titre_prefixe
from .permissions_rbac import require_permission
from .qr import verify_token
from .schemas import (
    CheckinMembre,
    CheckinRequest,
    CheckinResult,
    CheckoutResult,
    ControlMembre,
    EvenementOut,
    ManualCheckinRequest,
    UserMe,
    VerifyRequest,
    VerifyResult,
)

router = APIRouter(prefix="/api/v1/controle", tags=["controle"])

_IDENTITE_COLS = (
    "m.est_berger, m.nom_pastoral, m.nom_naissance, m.nom_marital, m.nom_affiche, "
    # The controller has to see who they are letting in. Without these three the
    # scan card shows a suspended member exactly like an approved one.
    "m.statut, m.statut_inscription, coalesce(m.verifie, false) AS verifie"
)


def _civil(row: dict[str, object]) -> str:
    """Civil display name for a scan/control row (family name arbitration honoured)."""
    choix = row.get("nom_affiche")
    fam = row.get("nom")
    if choix == "naissance" and row.get("nom_naissance"):
        fam = row.get("nom_naissance")
    elif choix == "marital" and row.get("nom_marital"):
        fam = row.get("nom_marital")
    return identite.nom_affichage(fam, row.get("prenoms"))  # type: ignore[arg-type]


def _pastoral(row: dict[str, object]) -> str | None:
    """Gendered pastoral appellation for a scan/control row, when the member is Berger."""
    if not row.get("est_berger"):
        return None
    return identite.nom_pastoral_affichage(row.get("genre"), row.get("nom_pastoral"))  # type: ignore[arg-type]



def _lookup_membre(membre_id: str, role: str) -> dict[str, object] | None:
    """Member identity for the scan card, with the confirmed honorific prefix
    (Berger, Coordinatrice...) resolved by gender so the controller greets the
    member properly. An unconfirmed function yields no title."""
    row = db.fetch_one(
        "SELECT m.id, m.matricule, m.nom, m.prenoms, m.photo_url, m.genre, m.fonction_confirmee, "
        f"{_IDENTITE_COLS}, "
        "fh.libelle_h AS fh, fh.libelle_f AS ff, fh.libelle_n AS fn "
        "FROM membre m LEFT JOIN fonction_honorifique fh ON fh.cle = m.fonction_cle AND fh.actif "
        "WHERE m.id = %s",
        (membre_id,),
        role=role,
    )
    if row:
        row["titre"] = titre_prefixe(
            row.get("genre"), row.get("fonction_confirmee"), row.get("fh"), row.get("ff"), row.get("fn")
        )
        # Category-aware appellation for the scan card, from the central resolver
        # (special function > title > function > particular). Additive: the legacy
        # ``titre`` field stays for the offline cache, ``appellation`` is preferred.
        from . import fonctions_membre
        choix = row.get("nom_affiche")
        fam = (row.get("nom_naissance") if choix == "naissance" and row.get("nom_naissance")
               else row.get("nom_marital") if choix == "marital" and row.get("nom_marital")
               else row.get("nom"))
        nom_civil = identite.nom_affichage(fam, row.get("prenoms"))
        fonctions = fonctions_membre.fonctions_publiques(membre_id, row.get("genre"), role)
        ident = identite.resoudre_identite(
            genre=row.get("genre"), prenoms=row.get("prenoms"), nom_civil=nom_civil,
            est_berger=bool(row.get("est_berger")), nom_pastoral=row.get("nom_pastoral"),
            fonctions=fonctions,
        )
        row["appellation"] = ident.get("appellation")
        row["appellation_formelle"] = ident.get("formel")
        row["categorie_principale"] = ident.get("categorie_principale")
    return row


def _signed_photo(path: object) -> str | None:
    """Short-lived signed URL for a member's identity photo, for the scan card.
    Best-effort: a signing failure just yields no photo, not an error."""
    if not path:
        return None
    from . import storage
    from .config import settings

    try:
        return storage.signed_download_url(settings.storage_bucket_photos, str(path))
    except storage.StorageError:
        return None


@router.get("/evenements", response_model=list[EvenementOut])
def open_or_upcoming_events(
    user: Annotated[UserMe, Depends(require_permission("controle.controler"))],
) -> list[EvenementOut]:
    rows = db.fetch_all(
        """
        SELECT e.id, e.titre, e.type, e.volet, e.debut, e.fin, e.lieu, e.mode, e.session_ouverte,
               e.cible_type, e.cible_id,
               CASE e.cible_type
                 WHEN 'coordination' THEN (SELECT nom FROM coordination WHERE id = e.cible_id)
                 WHEN 'commission' THEN (SELECT nom FROM commission WHERE id = e.cible_id)
                 WHEN 'intendance' THEN (SELECT nom FROM intendance WHERE id = e.cible_id)
                 WHEN 'tribu' THEN (SELECT nom FROM tribu WHERE id = e.cible_id)
                 WHEN 'general' THEN NULL
                 ELSE (SELECT ca.libelle FROM cible_activite ca WHERE ca.code = e.cible_type)
               END AS cible_libelle
        FROM evenement e
        WHERE e.fin IS NULL OR e.fin >= now()
        ORDER BY e.debut ASC
        LIMIT 200
        """,
        (),
        role=user.role,
    )
    return [
        EvenementOut(
            id=str(r["id"]),
            titre=r["titre"],
            type=r["type"],
            volet=r["volet"],
            debut=r["debut"],
            fin=r["fin"],
            lieu=r["lieu"],
            mode=r.get("mode"),
            session_ouverte=r["session_ouverte"],
            cible_type=r.get("cible_type") or "general",
            cible_id=str(r["cible_id"]) if r.get("cible_id") else None,
            cible_libelle=r.get("cible_libelle"),
        )
        for r in rows
    ]


@router.get("/membres", response_model=list[ControlMembre])
def directory(
    user: Annotated[UserMe, Depends(require_permission("controle.controler"))],
    q: Annotated[str | None, Query(max_length=120)] = None,
    limit: Annotated[int, Query(ge=1, le=2000)] = 1000,
) -> list[ControlMembre]:
    """Member directory the controller caches for offline lookup and manual entry."""
    where = ""
    params: tuple[object, ...] = ()
    if q:
        where = (
            "WHERE m.matricule ILIKE %s OR m.nom ILIKE %s "
            "OR m.prenoms ILIKE %s OR m.telephone ILIKE %s"
        )
        like = f"%{q}%"
        params = (like, like, like, like)
    rows = db.fetch_all(
        f"""
        SELECT m.id, m.matricule, m.nom, m.prenoms, m.statut, c.nom AS commission,
               m.genre, m.fonction_confirmee, {_IDENTITE_COLS},
               fh.libelle_h AS fh, fh.libelle_f AS ff, fh.libelle_n AS fn
        FROM membre m
        LEFT JOIN commission c ON c.id = m.commission_id
        LEFT JOIN fonction_honorifique fh ON fh.cle = m.fonction_cle AND fh.actif
        {where}
        ORDER BY m.matricule ASC
        LIMIT {limit}
        """,
        params,
        role=user.role,
    )
    return [
        ControlMembre(
            id=str(r["id"]),
            matricule=str(r["matricule"]),
            nom=r["nom"] if isinstance(r["nom"], str) else None,
            prenoms=r["prenoms"] if isinstance(r["prenoms"], str) else None,
            nom_affichage=_civil(r),
            est_berger=bool(r.get("est_berger")),
            nom_pastoral_affiche=_pastoral(r),
            commission=r["commission"] if isinstance(r["commission"], str) else None,
            statut=str(r["statut"]),
            titre=titre_prefixe(r.get("genre"), r.get("fonction_confirmee"), r.get("fh"), r.get("ff"), r.get("fn")),
        )
        for r in rows
    ]


def _mark_present_scan(membre_id: str, evenement_id: str, role: str) -> bool:
    """Record the check-in as a PROVEN on-site attendance, overriding any declaration.

    A scan is the only channel that proves somebody was physically there, so it writes
    the modality and the confidence rather than leaving them to be guessed downstream.
    Before this, the row said "present" and left ``modalite`` empty: every reader had
    to re-derive on-site status from ``source``, and any that forgot counted a proven
    presence as a modality-unknown one.

    Any absence the member had declared is cleared: they are standing at the door, so
    the reason they gave for not coming no longer describes anything.

    Returns whether the row was written. The caller needs to know: a check-in whose
    participation silently failed to record is exactly the case that produced presences
    visible in one table and absent from the other.
    """
    try:
        db.execute(
            """
            INSERT INTO participation (evenement_id, membre_id, statut, source, valide,
                                       modalite, mode_suivi, niveau_en_ligne, confiance,
                                       absence_motif, absence_commentaire, absence_qualification)
            VALUES (%s, %s, 'present', 'scan', true,
                    'presentiel', 'presentiel', NULL, 'prouvee',
                    NULL, NULL, 'sans_objet')
            ON CONFLICT (evenement_id, membre_id)
            DO UPDATE SET statut = 'present', source = 'scan', valide = true,
                          modalite = 'presentiel', mode_suivi = 'presentiel',
                          niveau_en_ligne = NULL, confiance = 'prouvee',
                          absence_motif = NULL, absence_commentaire = NULL,
                          absence_qualification = 'sans_objet',
                          qualifie_par = NULL, qualifie_le = NULL,
                          legacy_ambigu = false, maj_le = now()
            """,
            (evenement_id, membre_id),
            role=role,
        )
        return True
    except Exception:  # noqa: BLE001 - a failed mirror must not block the person at the door
        # Swallowed on purpose, reported to the caller. Letting this raise would turn a
        # database hiccup into a queue of people who cannot enter.
        return False


@router.post("/checkin-manuel", response_model=CheckinResult)
def checkin_manuel(
    payload: ManualCheckinRequest,
    user: Annotated[UserMe, Depends(require_permission("controle.controler"))],
) -> CheckinResult:
    """Manual check-in by member id, logged with the 'manuelle' method (offline fallback)."""
    membre = _lookup_membre(payload.membre_id, user.role)
    if not membre:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")
    # The control application verifies an identity before it records a presence.
    # A closed file, suspended, archived or refused, is a decision the organisation
    # already took, and the door is not the place to overturn it. Enforced here, on
    # the server, so a modified client cannot check anybody in.
    etat = controle_eligibilite.etat_pour_pointage(payload.membre_id, user.role)
    if controle_eligibilite.refuse(etat):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Pointage refusé : {etat['raison']}",
            headers={"X-Adsum-Motif": str(etat.get("code") or "refuse")},
        )
    inserted = db.execute(
        """
        INSERT INTO presence (membre_id, evenement_id, mode, arrivee, methode, cree_par)
        VALUES (%s, %s, 'presentiel', now(), 'manuelle', %s)
        ON CONFLICT (membre_id, evenement_id) DO NOTHING
        RETURNING arrivee
        """,
        (payload.membre_id, payload.evenement_id, user.id),
        role=user.role,
    )
    if inserted:
        arrivee = inserted["arrivee"]
        deja_present = False
    else:
        existing = db.fetch_one(
            "SELECT arrivee FROM presence WHERE membre_id = %s AND evenement_id = %s",
            (payload.membre_id, payload.evenement_id),
            role=user.role,
        )
        arrivee = existing["arrivee"] if existing else None
        deja_present = True
    _mark_present_scan(payload.membre_id, payload.evenement_id, user.role)
    return CheckinResult(
        deja_present=deja_present,
        membre=CheckinMembre(
            id=str(membre["id"]),
            matricule=str(membre["matricule"]),
            nom=membre["nom"] if isinstance(membre["nom"], str) else None,
            prenoms=membre["prenoms"] if isinstance(membre["prenoms"], str) else None,
            nom_affichage=_civil(membre),
            est_berger=bool(membre.get("est_berger")),
            nom_pastoral_affiche=_pastoral(membre),
            photo_url=_signed_photo(membre.get("photo_url")),
            titre=membre.get("titre") if isinstance(membre.get("titre"), str) else None,
            statut=etat.get("statut"),
            statut_inscription=etat.get("statut_inscription"),
            identite_verifiee=bool(etat.get("identite_verifiee")),
        ),
        evenement_id=payload.evenement_id,
        arrivee=arrivee,
        # An "alerte" verdict let the person through but flagged something. Returned so
        # the controller sees it on the confirmation screen rather than never.
        alerte=etat["raison"] if etat.get("verdict") == controle_eligibilite.ALERTE else None,
    )


@router.post("/checkout", response_model=CheckoutResult)
def checkout(
    payload: CheckinRequest,
    user: Annotated[UserMe, Depends(require_permission("controle.controler"))],
) -> CheckoutResult:
    """Exit mode: a second scan records the member departure for the event."""
    result = verify_token(payload.token)
    if not result["valid"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(result.get("reason") or "invalid token"),
        )
    membre_id = result["membre_id"]
    if not isinstance(membre_id, str):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="malformed payload")
    membre = _lookup_membre(membre_id, user.role)
    if not membre:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")
    updated = db.execute(
        """
        UPDATE presence SET depart = now()
        WHERE membre_id = %s AND evenement_id = %s AND depart IS NULL
        RETURNING depart
        """,
        (membre_id, payload.evenement_id),
        role=user.role,
    )
    if updated:
        depart = updated["depart"]
        deja_sorti = False
    else:
        existing = db.fetch_one(
            "SELECT depart FROM presence WHERE membre_id = %s AND evenement_id = %s",
            (membre_id, payload.evenement_id),
            role=user.role,
        )
        depart = existing["depart"] if existing else None
        deja_sorti = True
    return CheckoutResult(
        membre=CheckinMembre(
            id=str(membre["id"]),
            matricule=str(membre["matricule"]),
            nom=membre["nom"] if isinstance(membre["nom"], str) else None,
            prenoms=membre["prenoms"] if isinstance(membre["prenoms"], str) else None,
            nom_affichage=_civil(membre),
            est_berger=bool(membre.get("est_berger")),
            nom_pastoral_affiche=_pastoral(membre),
            photo_url=_signed_photo(membre.get("photo_url")),
            titre=membre.get("titre") if isinstance(membre.get("titre"), str) else None,
        ),
        evenement_id=payload.evenement_id,
        depart=depart,
        deja_sorti=deja_sorti,
    )


@router.post("/verify", response_model=VerifyResult)
def verify(
    payload: VerifyRequest,
    user: Annotated[UserMe, Depends(require_permission("controle.controler"))],
) -> VerifyResult:
    result = verify_token(payload.token)
    membre_id = result.get("membre_id")
    matricule = nom = prenoms = photo_url = titre = None
    nom_affichage = ""
    est_berger = False
    nom_pastoral_affiche = None
    # A signature that checks out only proves the badge is genuine. Whether the
    # organisation still counts this person is a separate question, answered here so
    # the controller never has to assume.
    etat: dict[str, object] = {"verdict": controle_eligibilite.REFUSE,
                               "raison": "Jeton illisible", "code": "jeton_illisible",
                               "statut": None, "statut_inscription": None,
                               "identite_verifiee": False}
    if isinstance(membre_id, str):
        etat = controle_eligibilite.etat_pour_pointage(membre_id, user.role)
        row = _lookup_membre(membre_id, user.role)
        if row:
            matricule = row["matricule"]
            nom = row["nom"]
            prenoms = row["prenoms"]
            nom_affichage = _civil(row)
            est_berger = bool(row.get("est_berger"))
            nom_pastoral_affiche = _pastoral(row)
            photo_url = _signed_photo(row.get("photo_url"))
            titre = row.get("titre") if isinstance(row.get("titre"), str) else None
    return VerifyResult(
        valid=bool(result["valid"]),
        reason=result.get("reason"),
        membre_id=membre_id if isinstance(membre_id, str) else None,
        issued_at=result.get("issued_at"),
        expires_at=result.get("expires_at"),
        key_version=result.get("key_version"),
        matricule=matricule,
        nom=nom,
        prenoms=prenoms,
        nom_affichage=nom_affichage,
        est_berger=est_berger,
        nom_pastoral_affiche=nom_pastoral_affiche,
        photo_url=photo_url,
        titre=titre,
        verdict=str(etat.get("verdict") or controle_eligibilite.REFUSE),
        verdict_raison=(str(etat["raison"]) if etat.get("raison") else None),
        verdict_code=(str(etat["code"]) if etat.get("code") else None),
        statut=(str(etat["statut"]) if etat.get("statut") else None),
        statut_inscription=(str(etat["statut_inscription"]) if etat.get("statut_inscription") else None),
        identite_verifiee=bool(etat.get("identite_verifiee")),
    )


@router.post("/checkin", response_model=CheckinResult)
def checkin(
    payload: CheckinRequest,
    user: Annotated[UserMe, Depends(require_permission("controle.controler"))],
) -> CheckinResult:
    result = verify_token(payload.token)
    if not result["valid"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(result.get("reason") or "invalid token"),
        )
    membre_id = result["membre_id"]
    if not isinstance(membre_id, str):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="malformed payload",
        )
    membre = _lookup_membre(membre_id, user.role)
    if not membre:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")
    # The control application verifies an identity before it records a presence.
    # A closed file, suspended, archived or refused, is a decision the organisation
    # already took, and the door is not the place to overturn it. Enforced here, on
    # the server, so a modified client cannot check anybody in.
    etat = controle_eligibilite.etat_pour_pointage(membre_id, user.role)
    if controle_eligibilite.refuse(etat):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Pointage refusé : {etat['raison']}",
            headers={"X-Adsum-Motif": str(etat.get("code") or "refuse")},
        )
    inserted = db.execute(
        """
        INSERT INTO presence (membre_id, evenement_id, mode, arrivee, methode, cree_par)
        VALUES (%s, %s, 'presentiel', now(), 'qr', %s)
        ON CONFLICT (membre_id, evenement_id) DO NOTHING
        RETURNING arrivee
        """,
        (membre_id, payload.evenement_id, user.id),
        role=user.role,
    )
    if inserted:
        arrivee = inserted["arrivee"]
        deja_present = False
    else:
        existing = db.fetch_one(
            "SELECT arrivee FROM presence WHERE membre_id = %s AND evenement_id = %s",
            (membre_id, payload.evenement_id),
            role=user.role,
        )
        arrivee = existing["arrivee"] if existing else None
        deja_present = True
    _mark_present_scan(membre_id, payload.evenement_id, user.role)
    return CheckinResult(
        deja_present=deja_present,
        membre=CheckinMembre(
            id=str(membre["id"]),
            matricule=str(membre["matricule"]),
            nom=membre["nom"] if isinstance(membre["nom"], str) else None,
            prenoms=membre["prenoms"] if isinstance(membre["prenoms"], str) else None,
            nom_affichage=_civil(membre),
            est_berger=bool(membre.get("est_berger")),
            nom_pastoral_affiche=_pastoral(membre),
            photo_url=_signed_photo(membre.get("photo_url")),
            titre=membre.get("titre") if isinstance(membre.get("titre"), str) else None,
            statut=etat.get("statut"),
            statut_inscription=etat.get("statut_inscription"),
            identite_verifiee=bool(etat.get("identite_verifiee")),
        ),
        evenement_id=payload.evenement_id,
        arrivee=arrivee,
        # An "alerte" verdict let the person through but flagged something. Returned so
        # the controller sees it on the confirmation screen rather than never.
        alerte=etat["raison"] if etat.get("verdict") == controle_eligibilite.ALERTE else None,
    )
