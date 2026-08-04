"""Reopen, for members already validated, the fields they never filled in.

The registration form grew a great deal after the first members were validated.
Their file is closed and a validated member's fields are locked, so information the
form now asks for, whether they were baptised, whether they hold a member code, what
they do for a living, can neither be supplied by them nor requested by the
organisation. Their profile is frozen at the shape the form had the day they signed.

Two campaigns live here. One asks only for the member code, which is the field the
organisation most needs and the one every member is meant to have. The other reopens
everything a given member left empty, computed per member so nobody is asked about
something they already answered.

Both give the member exactly what the administration would have opened by hand: a
request saying what is expected, those fields unlocked on their record, a response
window and a notification. The member fills them from their own space and submits,
so the values go through the ordinary review instead of being written straight into
the base.

What they never do:

Touch a cycle the administration opened. Widening someone else's cycle would let a
member submit changes nobody asked them for, and would consume a cycle opened for a
different reason. A cycle opened by these campaigns is another matter: it belongs to
this process, and the second campaign widens it rather than notifying the same person
twice about the same errand.

Ask for documents. The photo, the identity document and the signature were provided
once and stay provided; reopening them would ask people to redo work they have done.

Assume. A member who has answered that they hold no code is left alone until they
obtain one, rather than chased for something they said they do not have.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from . import audit, db
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/admin", tags=["membres"])

#: What the member sees as the subject of the request opened for them.
_SUJET = "Communiquez votre code membre"
#: The single field this campaign unlocks. Named explicitly rather than taken from a
#: caller: a bulk unlock that can open arbitrary fields is a bulk unlock nobody can
#: safely run twice.
_CHAMP = "code_membre"


class ResultatCampagne(BaseModel):
    """What the run did, or would have done when only simulating."""

    simulation: bool
    #: Validated members with no code and no declaration that they have none.
    eligibles: int
    #: Left alone because a modification cycle was already open for them.
    cycle_deja_ouvert: int
    #: Requests opened, fields unlocked, members notified.
    ouverts: int
    delai_jours: int


def _eligibles(role: str | None) -> list[dict[str, Any]]:
    """Validated members with no code, who have not said they have none.

    A member with an open cycle is returned too, and skipped later, so the report can
    say how many were left alone rather than silently counting fewer than there are.
    """
    return [
        dict(r) for r in db.fetch_all(
            "SELECT m.id, m.prenoms, "
            "  EXISTS (SELECT 1 FROM demande d WHERE d.membre_id = m.id "
            "          AND d.statut IN ('attente_membre', 'en_validation')) AS cycle_ouvert "
            "FROM membre m "
            "WHERE m.statut_inscription = 'approuve' "
            "  AND m.statut = 'actif' "
            "  AND (m.code_membre IS NULL OR btrim(m.code_membre) = '') "
            "  AND m.a_code_membre IS DISTINCT FROM false "
            "ORDER BY m.nom, m.prenoms",
            (), role=role,
        )
    ]


def _ouvrir_pour(membre_id: str, delai: int, role: str | None) -> bool:
    """Open one member's cycle: request, unlock, window, message. True when done."""
    # Imported here rather than at module scope: this keeps the dependency one-way
    # at import time, and reuses the very helpers the administration's own unlock
    # uses, so a member sees the same thread and the same notification either way.
    from .demandes import _notify_ticket, _system_message

    cree = db.execute(
        "INSERT INTO demande (membre_id, type, sujet, champ_concerne, categorie, statut, echeance_reponse, maj_le) "
        "VALUES (%s, 'modification_info', %s, %s, 'modification_info', 'attente_membre', "
        "        now() + make_interval(days => %s), now()) RETURNING id",
        (membre_id, _SUJET, _CHAMP, delai), role=role,
    )
    if not cree:
        return False
    db.execute(
        "UPDATE membre SET champs_deverrouilles = %s WHERE id = %s",
        ([_CHAMP], membre_id), role=role,
    )
    corps = (
        "Votre code membre n'est pas encore enregistré. Il vous est propre, il est "
        "délivré par l'organisation, et il est distinct du matricule que la plateforme "
        "vous a attribué. Le champ vient d'être ouvert dans votre espace : renseignez-le "
        "puis validez. Si vous n'en avez pas encore, laissez-le vide et dites-le nous : "
        "nous rouvrirons ce champ le jour où il vous sera remis."
    )
    _system_message(str(cree["id"]), role, corps)
    _notify_ticket(str(cree["id"]), role, "Votre code membre est attendu", corps)
    return True


@router.post("/membres/campagne-code-membre", response_model=ResultatCampagne)
def campagne_code_membre(
    user: Annotated[UserMe, Depends(require_permission("membres.administrer"))],
    simulation: bool = Query(default=True, description="Ne rien écrire, seulement compter."),
    delai_jours: int = Query(default=0, ge=0, le=365, description="Fenêtre de réponse; 0 pour le délai configuré."),
) -> ResultatCampagne:
    """Open a member-code request for every validated member who still has none.

    Simulating by default is not caution for its own sake: this writes to as many
    records as there are members concerned and notifies each one, so the count is
    worth reading before the notifications go out. Pass simulation=false to run it.

    Safe to run again. A member served by an earlier run has an open cycle and is
    skipped by the next, so repeating the call chases nobody twice.
    """
    from .demandes import _delai_deblocage_defaut

    delai = delai_jours or _delai_deblocage_defaut(user.role)
    membres = _eligibles(user.role)
    deja = [m for m in membres if m.get("cycle_ouvert")]
    a_ouvrir = [m for m in membres if not m.get("cycle_ouvert")]

    ouverts = 0
    if not simulation:
        for m in a_ouvrir:
            if _ouvrir_pour(str(m["id"]), delai, user.role):
                ouverts += 1
        audit.log(
            user.id, user.role, "campagne_code_membre", "membre", None,
            {"ouverts": ouverts, "ignores_cycle_ouvert": len(deja), "delai_jours": delai},
        )

    return ResultatCampagne(
        simulation=simulation,
        eligibles=len(membres),
        cycle_deja_ouvert=len(deja),
        ouverts=ouverts,
        delai_jours=delai,
    )


#: Columns a member may be asked to complete, and how "empty" reads for each.
#:
#: Only fields, never the photo or the identity document: those were provided and
#: signed for at registration, and reopening them would ask people to redo work they
#: have already done.
#:
#: The booleans of the spiritual path are nullable with no default, so NULL there
#: genuinely means "never answered" and is not the same as "no". berger_declare is
#: the exception: it is NOT NULL and defaults to false, so false cannot be told from
#: unanswered, and it is offered to everyone rather than guessed at.
_TOUJOURS_OFFERTS = ("berger_declare",)
#: A display preference rather than a fact about the member: always meaningful, never
#: "missing", so it is not reopened.
_JAMAIS_OFFERTS = ("naissance_annee_visible",)

#: Cycles this module opened itself, recognised by their subject.
#:
#: A cycle opened by the administration is never touched: widening it would let the
#: member submit changes nobody asked them for. But a cycle opened by an earlier run
#: of these campaigns belongs to this process, and widening it is the right move,
#: because the alternative is to leave the member locked out of the very fields this
#: run exists to open, or to close their cycle and notify them a second time.
_NOS_SUJETS = (_SUJET, "Complétez votre profil")


def _champs_du_catalogue() -> list[str]:
    """The catalogue entries that are fields, in catalogue order."""
    from .deblocage import ELEMENTS

    return [
        cle for cle, meta in ELEMENTS.items()
        if meta.get("type") == "champ" and cle not in _JAMAIS_OFFERTS
    ]


def _vides(ligne: dict[str, Any], champs: list[str]) -> list[str]:
    """Which of those fields this member has left empty."""
    manquants = []
    for cle in champs:
        if cle in _TOUJOURS_OFFERTS:
            manquants.append(cle)
            continue
        valeur = ligne.get(cle)
        if valeur is None or (isinstance(valeur, str) and not valeur.strip()):
            manquants.append(cle)
    return manquants


class ResultatCompletude(BaseModel):
    """What the completeness run did, or would have done when only simulating."""

    simulation: bool
    #: Validated, active members examined.
    examines: int
    #: Left alone because a modification cycle was already open for them.
    cycle_deja_ouvert: int
    #: Left alone because they have nothing left to fill.
    deja_complets: int
    ouverts: int
    champs_ouverts: int
    delai_jours: int


@router.post("/membres/campagne-completude", response_model=ResultatCompletude)
def campagne_completude(
    user: Annotated[UserMe, Depends(require_permission("membres.administrer"))],
    simulation: bool = Query(default=True, description="Ne rien écrire, seulement compter."),
    delai_jours: int = Query(default=0, ge=0, le=365, description="Fenêtre de réponse; 0 pour le délai configuré."),
) -> ResultatCompletude:
    """Reopen every field a validated member never filled in, member by member.

    The registration form grew a great deal after the first members were validated.
    Their file is closed and their fields are locked, so information the form now
    asks for, whether they were baptised, whether they are on a path toward marriage,
    their profession, cannot be supplied by them and cannot be requested.

    Each member gets a cycle unlocking exactly what is empty on their own record, so
    nobody is asked about something they already answered. Documents and the
    signature are never part of it: those were provided once and stay provided.

    Simulates by default, and is safe to run again: a member with an open cycle is
    skipped, and a member with nothing left to fill opens nothing.
    """
    from .demandes import _delai_deblocage_defaut

    delai = delai_jours or _delai_deblocage_defaut(user.role)
    champs = _champs_du_catalogue()
    colonnes = ", ".join(f"m.{c}" for c in champs)
    lignes = db.fetch_all(
        f"SELECT m.id, {colonnes}, "
        # The cycle this process opened for them, if any: it is widened rather than
        # replaced, so nobody is notified twice about the same errand.
        "  (SELECT d.id FROM demande d WHERE d.membre_id = m.id "
        "   AND d.statut = 'attente_membre' AND d.sujet = ANY(%s) "
        "   ORDER BY d.maj_le DESC LIMIT 1) AS notre_cycle, "
        # A cycle opened by the administration for its own reasons: left untouched.
        "  EXISTS (SELECT 1 FROM demande d WHERE d.membre_id = m.id "
        "          AND d.statut IN ('attente_membre', 'en_validation') "
        "          AND d.sujet <> ALL(%s)) AS cycle_tiers "
        "FROM membre m "
        "WHERE m.statut_inscription = 'approuve' AND m.statut = 'actif' "
        "ORDER BY m.nom, m.prenoms",
        (list(_NOS_SUJETS), list(_NOS_SUJETS)), role=user.role,
    )

    deja = 0
    complets = 0
    ouverts = 0
    total_champs = 0
    for ligne in lignes:
        # A cycle the administration opened for its own reasons is left alone.
        if ligne.get("cycle_tiers"):
            deja += 1
            continue
        manquants = _vides(dict(ligne), champs)
        if not manquants:
            complets += 1
            continue
        total_champs += len(manquants)
        if simulation:
            continue
        notre = ligne.get("notre_cycle")
        fait = (
            _elargir_cycle(str(notre), str(ligne["id"]), manquants, user.role)
            if notre
            else _ouvrir_completude(str(ligne["id"]), manquants, delai, user.role)
        )
        if fait:
            ouverts += 1

    if not simulation:
        audit.log(
            user.id, user.role, "campagne_completude", "membre", None,
            {"ouverts": ouverts, "champs_ouverts": total_champs,
             "ignores_cycle_ouvert": deja, "deja_complets": complets, "delai_jours": delai},
        )

    return ResultatCompletude(
        simulation=simulation,
        examines=len(lignes),
        cycle_deja_ouvert=deja,
        deja_complets=complets,
        ouverts=len(lignes) - deja - complets if simulation else ouverts,
        champs_ouverts=total_champs,
        delai_jours=delai,
    )


def _ouvrir_completude(membre_id: str, champs: list[str], delai: int, role: str | None) -> bool:
    """Open one member's completeness cycle over exactly their empty fields."""
    from .deblocage import libelles
    from .demandes import _notify_ticket, _system_message

    cree = db.execute(
        "INSERT INTO demande (membre_id, type, sujet, categorie, statut, echeance_reponse, maj_le) "
        "VALUES (%s, 'modification_info', %s, 'modification_info', 'attente_membre', "
        "        now() + make_interval(days => %s), now()) RETURNING id",
        (membre_id, "Complétez votre profil", delai), role=role,
    )
    if not cree:
        return False
    db.execute(
        "UPDATE membre SET champs_deverrouilles = %s WHERE id = %s",
        (champs, membre_id), role=role,
    )
    # Naming the fields beats giving a count: a member told "complete your profile"
    # has to go hunting. But a member registered early has around twenty open lines,
    # and twenty labels in one sentence is a wall nobody reads, so the message names
    # the first few and says how many follow. The screen lists them all.
    noms = libelles(champs)
    apercu = ", ".join(noms[:8])
    reste = len(noms) - 8
    liste = f"{apercu}, et {reste} autre(s)" if reste > 0 else apercu
    corps = (
        "Votre profil a été ouvert pour que vous puissiez compléter les informations "
        "qui n'étaient pas encore demandées lorsque vous vous êtes inscrit. "
        f"À renseigner : {liste}. "
        "Renseignez ce qui vous concerne depuis votre espace, laissez le reste vide, "
        "puis validez. Vos pièces justificatives et votre signature restent acquises : "
        "il n'y a aucun document à refournir."
    )
    _system_message(str(cree["id"]), role, corps)
    _notify_ticket(str(cree["id"]), role, "Complétez votre profil", corps)
    return True


def _elargir_cycle(demande_id: str, membre_id: str, champs: list[str], role: str | None) -> bool:
    """Widen a cycle this process already opened, instead of opening a second one.

    An earlier run unlocked one field; this one has more to offer. Closing that cycle
    and opening another would notify the member twice about the same errand, and
    leave a resolved request in their history that they never answered. So the same
    cycle grows, and the thread says what changed.
    """
    from .deblocage import libelles
    from .demandes import _system_message

    db.execute(
        "UPDATE membre SET champs_deverrouilles = %s WHERE id = %s",
        (champs, membre_id), role=role,
    )
    db.execute(
        "UPDATE demande SET sujet = %s, maj_le = now() WHERE id = %s",
        ("Complétez votre profil", demande_id), role=role,
    )
    noms = libelles(champs)
    apercu = ", ".join(noms[:8])
    reste = len(noms) - 8
    liste = f"{apercu}, et {reste} autre(s)" if reste > 0 else apercu
    _system_message(
        demande_id, role,
        "D'autres informations viennent d'être ouvertes dans votre espace, en plus de "
        f"celle déjà demandée. À renseigner : {liste}. Renseignez ce qui vous concerne, "
        "laissez le reste vide, puis validez. Aucun document n'est à refournir.",
    )
    return True


class ResultatAnnulation(BaseModel):
    """What the rollback closed and cleared."""

    simulation: bool
    demandes_fermees: int
    membres_reverrouilles: int
    #: Members who had already answered: their proposal is left untouched and their
    #: request stays open, because closing it would discard work they have done.
    reponses_preservees: int


@router.post("/membres/campagne-annuler", response_model=ResultatAnnulation)
def annuler_campagnes(
    user: Annotated[UserMe, Depends(require_permission("membres.administrer"))],
    simulation: bool = Query(default=True, description="Ne rien écrire, seulement compter."),
) -> ResultatAnnulation:
    """Withdraw every request these campaigns opened, and relock what they unlocked.

    The completeness campaign unlocked whatever column was empty, which is not the
    same as what a member still has to answer. It asked a confirmed berger whether
    they are a berger, and asked someone unmarried what kind of marriage they had.
    A form that asks a person to confirm what the organisation already knows, or to
    answer a question that does not apply to them, is worse than one that asks
    nothing: it reads as a system that does not know who it is talking to.

    So the requests are withdrawn rather than left to be worked through. A member who
    has already answered is the exception: their proposal is theirs, and their
    request stays open so it can be reviewed. Nobody's work is discarded to tidy up
    somebody else's mistake.

    Only requests these campaigns opened, recognised by their subject. Anything the
    administration opened is untouched.
    """
    from .demandes import _notify_ticket, _system_message

    lignes = db.fetch_all(
        "SELECT d.id, d.membre_id, "
        "  EXISTS (SELECT 1 FROM modification_membre mm WHERE mm.demande_id = d.id) AS a_repondu "
        "FROM demande d WHERE d.sujet = ANY(%s) AND d.statut IN ('attente_membre', 'en_validation')",
        (list(_NOS_SUJETS),), role=user.role,
    )
    a_fermer = [r for r in lignes if not r.get("a_repondu")]
    preserves = len(lignes) - len(a_fermer)

    if simulation:
        return ResultatAnnulation(
            simulation=True, demandes_fermees=len(a_fermer),
            membres_reverrouilles=len(a_fermer), reponses_preservees=preserves,
        )

    corps = (
        "Cette demande est retirée. Elle vous demandait de compléter des informations "
        "sans tenir compte de ce que l'organisation sait déjà de vous, et certaines "
        "questions ne vous concernaient pas. Vous n'avez rien à faire. Une demande "
        "précise vous sera adressée si une information vous est réellement nécessaire."
    )
    for r in a_fermer:
        db.execute(
            "UPDATE membre SET champs_deverrouilles = NULL WHERE id = %s",
            (str(r["membre_id"]),), role=user.role,
        )
        db.execute(
            "UPDATE demande SET statut = 'resolue', motif_cloture = %s, clos_le = now(), "
            "echeance_reponse = NULL, maj_le = now() WHERE id = %s",
            ("Demande retirée : questions non pertinentes pour ce membre.", str(r["id"])),
            role=user.role,
        )
        _system_message(str(r["id"]), user.role, corps)
        _notify_ticket(str(r["id"]), user.role, "Demande retirée", corps)

    audit.log(
        user.id, user.role, "campagne_annulation", "membre", None,
        {"fermees": len(a_fermer), "reponses_preservees": preserves},
    )
    return ResultatAnnulation(
        simulation=False, demandes_fermees=len(a_fermer),
        membres_reverrouilles=len(a_fermer), reponses_preservees=preserves,
    )
