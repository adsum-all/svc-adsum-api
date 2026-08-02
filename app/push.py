"""Reach a member on the mobile application this platform ships.

Members are reached by e-mail, Telegram, WhatsApp and SMS. The Android application
could not be reached at all, so somebody who installed it still learned about an
activity through the one channel this base has watched fail.

Delivery goes through Firebase Cloud Messaging over its HTTP v1 interface, which
authenticates with a service account rather than the retired server key. The
assertion is signed here with the library already used for this platform's own
tokens, so reaching the mobile application costs no new dependency.

Three rules hold throughout:

Never raise. This is called from the notification funnel, where an exception would
cost the member every other channel of the same message.

Never send blind. A token the service has rejected as unregistered is retired, with
the reason recorded. Retrying it forever is how a send queue silently fills with
addresses of phones that were wiped a year ago.

Do nothing, loudly, when unconfigured. Without a service account the module reports
that it is off rather than pretending to have sent.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import jwt

from . import channels, db

logger = logging.getLogger(__name__)

_OAUTH = "https://oauth2.googleapis.com/token"
_PORTEE = "https://www.googleapis.com/auth/firebase.messaging"
_DELAI_S = 10
#: Renewed a minute early: a token that expires between the check and the call is
#: indistinguishable from a misconfiguration, and produces the same confusing 401.
_MARGE_S = 60

#: What the service says when a token no longer addresses anything. The device is
#: retired on these, and only on these: a transient failure must not lose a phone.
_JETONS_MORTS = frozenset({"UNREGISTERED", "INVALID_ARGUMENT", "NOT_FOUND"})

_acces: dict[str, Any] = {"jeton": "", "expire": 0.0}


def _compte_de_service() -> dict[str, Any] | None:
    """The Firebase service account, or None when the organisation has not set one."""
    brut = (channels.integration_value("push_service_account") or "").strip()
    if not brut:
        return None
    try:
        compte = json.loads(brut)
    except json.JSONDecodeError:
        logger.warning("push: le compte de service n'est pas un JSON lisible")
        return None
    manquants = [c for c in ("client_email", "private_key", "project_id") if not compte.get(c)]
    if manquants:
        logger.warning("push: compte de service incomplet, champs manquants : %s", ", ".join(manquants))
        return None
    return compte


def configure() -> bool:
    """Whether push can be delivered at all. Cheap: no network call."""
    return _compte_de_service() is not None


def _jeton_acces(compte: dict[str, Any]) -> str | None:
    """A short-lived access token for the messaging scope, cached until it expires."""
    maintenant = time.time()
    if _acces["jeton"] and _acces["expire"] - _MARGE_S > maintenant:
        return str(_acces["jeton"])
    assertion = jwt.encode(
        {
            "iss": compte["client_email"],
            "scope": _PORTEE,
            "aud": _OAUTH,
            "iat": int(maintenant),
            "exp": int(maintenant) + 3600,
        },
        compte["private_key"],
        algorithm="RS256",
    )
    corps = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": assertion,
    }).encode()
    requete = urllib.request.Request(
        _OAUTH, data=corps, headers={"content-type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(requete, timeout=_DELAI_S) as reponse:
            charge = json.load(reponse)
    except Exception as erreur:  # noqa: BLE001 - a channel failure is never fatal
        logger.warning("push: jeton d'accès refusé (%s)", type(erreur).__name__)
        return None
    jeton = str(charge.get("access_token") or "")
    if not jeton:
        return None
    _acces["jeton"] = jeton
    _acces["expire"] = maintenant + float(charge.get("expires_in") or 3600)
    return jeton


def enregistrer_appareil(
    membre_id: str, jeton: str, plateforme: str = "android",
    libelle: str | None = None, role: str | None = None,
) -> bool:
    """Remember where to reach this member. Reassigns a token that changed hands.

    The push service reissues the same token to the same phone after a reinstall, so
    a token can legitimately move from one member to another when a device is handed
    over or a second person signs in on it. Reassigning rather than refusing is what
    stops notifications following a phone to somebody who no longer uses it.
    """
    jeton = (jeton or "").strip()
    if not jeton or not membre_id:
        return False
    if plateforme not in ("android", "ios", "web"):
        plateforme = "android"
    try:
        db.execute(
            "INSERT INTO appareil_push (membre_id, jeton, plateforme, libelle) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (jeton) DO UPDATE SET membre_id = EXCLUDED.membre_id, "
            "  plateforme = EXCLUDED.plateforme, libelle = EXCLUDED.libelle, "
            "  actif = true, motif_retrait = NULL, vu_le = now()",
            (membre_id, jeton, plateforme, libelle),
            role=role,
        )
        return True
    except Exception:  # noqa: BLE001
        logger.warning("push: enregistrement d'appareil impossible")
        return False


def retirer_appareil(jeton: str, motif: str = "retiré par le membre", role: str | None = None) -> bool:
    """Stop sending to this device. Kept as a row: why it went quiet is worth knowing."""
    jeton = (jeton or "").strip()
    if not jeton:
        return False
    try:
        db.execute(
            "UPDATE appareil_push SET actif = false, motif_retrait = %s WHERE jeton = %s",
            (motif[:200], jeton), role=role,
        )
        return True
    except Exception:  # noqa: BLE001
        return False


def appareils(membre_id: str, role: str | None = None) -> list[dict[str, Any]]:
    """This member's live devices, newest first."""
    try:
        return [
            dict(r) for r in db.fetch_all(
                "SELECT id, jeton, plateforme, libelle, cree_le, envoye_le "
                "FROM appareil_push WHERE membre_id = %s AND actif ORDER BY vu_le DESC",
                (membre_id,), role=role,
            )
        ]
    except Exception:  # noqa: BLE001
        return []


def _motif_refus(erreur: urllib.error.HTTPError) -> str:
    """The service's own reason for refusing, as a short code."""
    try:
        charge = json.loads(erreur.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return f"HTTP {erreur.code}"
    details = ((charge.get("error") or {}).get("details") or [])
    for detail in details:
        code = detail.get("errorCode")
        if code:
            return str(code)
    return str((charge.get("error") or {}).get("status") or f"HTTP {erreur.code}")


def _envoyer_a(jeton_acces: str, projet: str, jeton_appareil: str,
               titre: str, corps: str, donnees: dict[str, str]) -> tuple[bool, str]:
    """One message to one device. Returns (delivered, reason when it was not)."""
    charge = {
        "message": {
            "token": jeton_appareil,
            "notification": {"title": titre, "body": corps},
            "data": donnees,
            # High priority so a notification wakes a dozing device: an activity
            # reminder that arrives after the activity is not a reminder.
            "android": {"priority": "high"},
        },
    }
    requete = urllib.request.Request(
        f"https://fcm.googleapis.com/v1/projects/{projet}/messages:send",
        data=json.dumps(charge).encode("utf-8"),
        headers={"authorization": f"Bearer {jeton_acces}", "content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(requete, timeout=_DELAI_S):
            return True, ""
    except urllib.error.HTTPError as erreur:
        return False, _motif_refus(erreur)
    except Exception as erreur:  # noqa: BLE001
        return False, type(erreur).__name__


def envoyer(membre_id: str, titre: str, corps: str,
            type_notif: str = "", role: str | None = None) -> bool:
    """Notify every live device of this member. True when at least one was reached.

    A message the platform could not deliver to any device is not a failure of the
    notification: the other channels carry it. So this returns whether push was one
    of the channels used, and never raises.
    """
    compte = _compte_de_service()
    if not compte:
        return False
    cibles = appareils(membre_id, role=role)
    if not cibles:
        return False
    jeton_acces = _jeton_acces(compte)
    if not jeton_acces:
        return False

    # The body is trimmed rather than sent whole: a notification shade shows two
    # lines, and the service refuses payloads past 4 KB, which would lose the message
    # entirely rather than its tail.
    corps_court = corps.strip()
    if len(corps_court) > 500:
        corps_court = corps_court[:497].rstrip() + "..."

    atteint = False
    for cible in cibles:
        livre, motif = _envoyer_a(
            jeton_acces, str(compte["project_id"]), str(cible["jeton"]),
            titre, corps_court, {"type": type_notif} if type_notif else {},
        )
        if livre:
            atteint = True
            try:
                db.execute(
                    "UPDATE appareil_push SET envoye_le = now(), vu_le = now() WHERE id = %s",
                    (cible["id"],), role=role,
                )
            except Exception:  # noqa: BLE001
                pass
        elif motif in _JETONS_MORTS:
            # The device is gone. Retiring it here is what stops the send path
            # retrying a wiped phone on every notification, forever.
            retirer_appareil(str(cible["jeton"]), f"refusé par le service : {motif}", role=role)
        else:
            logger.info("push: échec transitoire (%s)", motif)
    return atteint
