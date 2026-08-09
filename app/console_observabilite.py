"""What the platform is failing to deliver, visible to the publisher's console.

The delivery journal has been filling up for weeks and nothing reads it. Two thousand
six hundred events sit in the database, of which several hundred are failures, and the
only way anyone learns a member stopped receiving anything is when that member says so.

Three questions are answered here, in the order an operator actually asks them.

**Is it broken right now?** Counts by outcome over a window, with the failure rate.
**What is failing?** Reasons, grouped, because forty addresses rejected for one reason
is a configuration fault and forty different reasons is forty different problems.
**Who is affected?** Recipients that keep failing, since an address that bounces every
time has stopped being reachable and no amount of resending will change that.

On personal data. A failing recipient is a member, and the console is not allowed to
read member records. The local part of the address is therefore masked and the domain
is kept: the domain is what carries the diagnosis (one provider refusing everything, a
typed domain that does not exist), and the local part is what identifies a person. An
operator who genuinely needs the full address opens the back office, where that access
is governed and audited.
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from . import db
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/support/console/envois", tags=["support-console"])

#: Outcomes that mean the message did not reach its destination.
ECHECS = ("rebondi", "rejete", "echoue", "expire")
#: Outcomes that mean it did.
SUCCES = ("delivre", "ouvert")

#: Domains reserved for documentation and seeding, which can never receive anything.
#: They are counted, then set aside from the rate: a base seeded with them shows a
#: catastrophic failure rate that describes the seed, not the platform, and an operator
#: who learns to ignore a red figure will ignore it the day it is real. Withheld
#: entirely they would hide a genuine fault, so they are reported as their own number.
DOMAINES_FICTIFS = ("example.com", "example.org", "example.net", "exemple.com", "exemple.fr", "test.com")
_FICTIF_SQL = (
    "(lower(split_part(destinataire, '@', 2)) = ANY(%s) "
    " OR lower(destinataire) LIKE '%%.invalid' OR lower(destinataire) LIKE '%%.test' "
    " OR lower(destinataire) LIKE '%%.localhost')"
)


def _masquer(adresse: str) -> str:
    """Keep the domain, hide who it is.

    ``jean.dupont@exemple.fr`` becomes ``j***t@exemple.fr``. The domain answers the
    operational question, the local part only answers "which member", which the
    console has no business answering.
    """
    if "@" not in adresse:
        return "***"
    local, _, domaine = adresse.partition("@")
    if len(local) <= 2:
        return f"{local[:1]}***@{domaine}"
    return f"{local[0]}***{local[-1]}@{domaine}"


@router.get("")
def resume(
    user: Annotated[UserMe, Depends(require_permission("support.traiter"))],
    jours: int = Query(default=7, ge=1, le=90),
) -> dict[str, object]:
    """Delivery health over a window, and what is going wrong in it."""
    par_statut = {
        str(r["statut_normalise"]): int(r["n"])
        for r in db.fetch_all(
            "SELECT statut_normalise, count(*) AS n FROM email_delivery_event "
            "WHERE survenu_le >= now() - make_interval(days => %s) GROUP BY 1",
            (jours,),
            role=user.role,
        )
    }
    echecs = sum(par_statut.get(s, 0) for s in ECHECS)
    succes = sum(par_statut.get(s, 0) for s in SUCCES)

    fictifs_row = db.fetch_one(
        f"SELECT count(*) AS n FROM email_delivery_event "
        f"WHERE statut_normalise = ANY(%s) AND survenu_le >= now() - make_interval(days => %s) AND {_FICTIF_SQL}",
        (list(ECHECS), jours, list(DOMAINES_FICTIFS)),
        role=user.role,
    ) or {}
    echecs_fictifs = int(fictifs_row.get("n") or 0)
    echecs_reels = max(0, echecs - echecs_fictifs)

    # The rate is over messages whose fate is known, and only over addresses that could
    # ever have received anything. Counting those still in flight as successes would
    # make a broken provider look healthy for as long as it stays slow.
    tranches = echecs_reels + succes

    motifs = [
        {"motif": str(r["motif"] or "non précisé"), "n": int(r["n"])}
        for r in db.fetch_all(
            "SELECT coalesce(nullif(trim(motif), ''), 'non précisé') AS motif, count(*) AS n "
            "FROM email_delivery_event "
            "WHERE statut_normalise = ANY(%s) AND survenu_le >= now() - make_interval(days => %s) "
            "GROUP BY 1 ORDER BY 2 DESC LIMIT 12",
            (list(ECHECS), jours),
            role=user.role,
        )
    ]

    # Grouped by domain, because a provider refusing everything is one problem and not
    # forty. This is also the level at which the console may legitimately look.
    domaines = [
        {"domaine": str(r["domaine"]), "echecs": int(r["n"]), "adresses": int(r["adresses"])}
        for r in db.fetch_all(
            "SELECT lower(split_part(destinataire, '@', 2)) AS domaine, count(*) AS n, "
            "  count(DISTINCT destinataire) AS adresses "
            "FROM email_delivery_event "
            "WHERE statut_normalise = ANY(%s) AND survenu_le >= now() - make_interval(days => %s) "
            "GROUP BY 1 ORDER BY 2 DESC LIMIT 10",
            (list(ECHECS), jours),
            role=user.role,
        )
    ]

    jour_par_jour = [
        {
            "jour": r["jour"].date().isoformat(),
            "echecs": int(r["echecs"]),
            "succes": int(r["succes"]),
        }
        for r in db.fetch_all(
            "SELECT date_trunc('day', survenu_le) AS jour, "
            "  count(*) FILTER (WHERE statut_normalise = ANY(%s)) AS echecs, "
            "  count(*) FILTER (WHERE statut_normalise = ANY(%s)) AS succes "
            "FROM email_delivery_event WHERE survenu_le >= now() - make_interval(days => %s) "
            "GROUP BY 1 ORDER BY 1",
            (list(ECHECS), list(SUCCES), jours),
            role=user.role,
        )
    ]

    return {
        "jours": jours,
        "par_statut": par_statut,
        "echecs": echecs,
        "succes": succes,
        "en_vol": sum(v for k, v in par_statut.items() if k not in ECHECS and k not in SUCCES),
        "echecs_adresses_fictives": echecs_fictifs,
        "echecs_reels": echecs_reels,
        "taux_echec": round(100.0 * echecs_reels / tranches, 1) if tranches else 0.0,
        "motifs": motifs,
        "domaines": domaines,
        "jour_par_jour": jour_par_jour,
    }


@router.get("/destinataires")
def destinataires_en_echec(
    user: Annotated[UserMe, Depends(require_permission("support.traiter"))],
    jours: int = Query(default=30, ge=1, le=365),
    limite: int = Query(default=25, ge=1, le=100),
) -> dict[str, object]:
    """Recipients that keep failing, masked, with the last reason given.

    An address that has bounced repeatedly has stopped being reachable: resending is
    not a fix, and the organisation needs to correct or retire it. Listing them is the
    only way that ever gets noticed.
    """
    rows = db.fetch_all(
        "SELECT destinataire, count(*) AS echecs, max(survenu_le) AS dernier, "
        "  (array_agg(coalesce(nullif(trim(motif), ''), 'non précisé') ORDER BY survenu_le DESC))[1] AS motif "
        "FROM email_delivery_event "
        "WHERE statut_normalise = ANY(%s) AND survenu_le >= now() - make_interval(days => %s) "
        "GROUP BY 1 ORDER BY 2 DESC, 3 DESC LIMIT %s",
        (list(ECHECS), jours, limite),
        role=user.role,
    )
    total = db.fetch_one(
        "SELECT count(DISTINCT destinataire) AS n FROM email_delivery_event "
        "WHERE statut_normalise = ANY(%s) AND survenu_le >= now() - make_interval(days => %s)",
        (list(ECHECS), jours),
        role=user.role,
    ) or {}
    return {
        "total": int(total.get("n") or 0),
        "affiches": len(rows),
        "note": (
            "Les adresses sont masquées : la console diagnostique la plateforme, elle "
            "n'identifie pas les membres. Le domaine reste lisible car c'est lui qui porte "
            "le diagnostic. L'adresse complète se consulte dans le back-office."
        ),
        "destinataires": [
            {
                "adresse": _masquer(str(r["destinataire"])),
                "echecs": int(r["echecs"]),
                "motif": str(r["motif"]),
                "dernier": r["dernier"].isoformat() if r["dernier"] else None,
            }
            for r in rows
        ],
    }
