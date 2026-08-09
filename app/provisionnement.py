"""Bringing a new client organisation online, one verifiable step at a time.

The target is a single button: register a customer, press it, and their database exists,
their referentials are seeded, their host resolves, their licence is in force and their
first administrator can sign in. Until every step is automatic, each client costs a day
of engineering and every forgotten step becomes an incident.

The design is a sequence of steps that are **idempotent and verified rather than
assumed**. Each one is checked before it is run and checked again after, so the sequence
can be re-run at any point: a provisioning that fails halfway is the normal case, not the
exception, and one that cannot be resumed has to be undone by hand.

**What this refuses to do.** Touch a database that already holds data. A DSN pasted with
a typo can easily point at a live organisation, and seeding referentials into it would
mix two clients in the one place the whole architecture exists to keep apart. The check
is not a warning; it stops the run.

**The one step that is not automatic, and why.** Applying the schema means running the
migrations, which live in the deployment repository with their history. Reimplementing
them here would create a second definition of the schema that drifts from the first, and
a drifting schema is worse than a manual step. So the schema is applied by the migration
runner everybody already uses, and this module *verifies* the result: it reads the
target's version and refuses to continue until it matches the head this API expects. The
step is reported with the exact command, so nothing has to be remembered.
"""
# ruff: noqa: E501
from __future__ import annotations

import re
from typing import Any, NamedTuple

import psycopg
from psycopg.rows import dict_row

from . import db

#: Referentials a fresh organisation cannot work without. Copied from the base this API
#: runs on, which is the only definition that is certainly current: a hard-coded copy
#: here would be a second source of truth and would age.
REFERENTIELS = (
    "application",
    "type_evenement",
    "cible_activite",
    "motif_absence",
    "support_categorie",
    "parametre",
)

#: Never copied. integration_config carries API keys and SMTP passwords: seeding a new
#: client with them would hand one organisation's credentials to another.
JAMAIS_COPIE = ("integration_config",)

_NOM_BASE = re.compile(r"^[a-z][a-z0-9_]{2,62}$")


class Etape(NamedTuple):
    code: str
    libelle: str
    #: What a human should do when this step cannot be done automatically.
    manuel: str = ""


ETAPES: tuple[Etape, ...] = (
    Etape("connexion", "Joindre la base cible"),
    Etape("vide", "Vérifier que la base est vide"),
    Etape(
        "schema", "Appliquer le schéma",
        "Depuis deployment/database : DATABASE_URL=<dsn cible> python -m alembic upgrade head",
    ),
    Etape("referentiels", "Semer les référentiels"),
    Etape("hote", "Rattacher le domaine à l'organisation"),
    Etape("licence", "Poser la licence et les modules"),
)


def version_attendue() -> str:
    """The schema version this API is written against."""
    ligne = db.fetch_one("SELECT version_num FROM alembic_version", ())
    return str(ligne["version_num"]) if ligne else ""


def valider_nom_base(nom: str) -> str:
    """A database name safe to place in DDL, which cannot be parameterised.

    CREATE DATABASE takes no bound parameters, so the name is interpolated. Restricting
    it to lower-case letters, digits and underscores is what makes that safe, and the
    check happens before anything is opened.
    """
    propre = (nom or "").strip().lower()
    if not _NOM_BASE.match(propre):
        raise ValueError(
            "Le nom de base doit faire 3 à 63 caractères, commencer par une lettre, "
            "et ne contenir que des minuscules, des chiffres et des tirets bas."
        )
    return propre


def creer_base(nom: str) -> dict[str, Any]:
    """Create an empty database on the cluster this API is connected to.

    Only reachable when the API's role may create databases. Where it may not, the
    operator creates the database at their host and pastes its connection string, which
    is the ordinary path for a managed service.
    """
    propre = valider_nom_base(nom)
    dsn = db.dsn_actuel()
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT rolcreatedb FROM pg_roles WHERE rolname = current_user")
        ligne = cur.fetchone()
        if not ligne or not ligne[0]:
            return {"fait": False, "motif": "Le rôle de l'API n'a pas le droit de créer une base sur ce serveur."}
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (propre,))
        if cur.fetchone():
            return {"fait": True, "deja": True, "nom": propre}
        # Interpolated because CREATE DATABASE accepts no parameter. Safe because the
        # name went through valider_nom_base, which allows nothing but [a-z0-9_].
        cur.execute(f'CREATE DATABASE "{propre}"')
    return {"fait": True, "deja": False, "nom": propre}


def _ouvrir(dsn: str) -> psycopg.Connection:
    return psycopg.connect(dsn, row_factory=dict_row, connect_timeout=8)


def diagnostiquer(dsn: str) -> dict[str, Any]:
    """Where a target database stands, step by step, without changing anything.

    Read-only on purpose: an operator must be able to ask "what is missing" as often as
    they like, including on a database they are unsure about.
    """
    etats: dict[str, dict[str, Any]] = {}

    try:
        with _ouvrir(dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT current_database() AS base, version() AS version")
            info = cur.fetchone() or {}
            etats["connexion"] = {"fait": True, "detail": str(info.get("base"))}

            cur.execute("SELECT count(*) AS n FROM pg_tables WHERE schemaname = 'public'")
            tables = int((cur.fetchone() or {}).get("n") or 0)

            cur.execute(
                "SELECT to_regclass('public.alembic_version') IS NOT NULL AS a_alembic, "
                "       to_regclass('public.membre') IS NOT NULL AS a_membre"
            )
            presence = cur.fetchone() or {}

            habitee = False
            if presence.get("a_membre"):
                cur.execute("SELECT count(*) AS n FROM membre")
                habitee = int((cur.fetchone() or {}).get("n") or 0) > 0

            etats["vide"] = {
                "fait": not habitee,
                "detail": f"{tables} table(s), {'des membres existent' if habitee else 'aucun membre'}",
                "bloquant": habitee,
            }

            version = ""
            if presence.get("a_alembic"):
                cur.execute("SELECT version_num FROM alembic_version")
                version = str((cur.fetchone() or {}).get("version_num") or "")
            attendue = version_attendue()
            etats["schema"] = {
                "fait": bool(version) and version == attendue,
                "detail": f"cible {version or 'aucune'}, attendue {attendue}",
            }

            manquants: list[str] = []
            if etats["schema"]["fait"]:
                for table in REFERENTIELS:
                    cur.execute(f"SELECT count(*) AS n FROM {table}")
                    if int((cur.fetchone() or {}).get("n") or 0) == 0:
                        manquants.append(table)
            etats["referentiels"] = {
                "fait": etats["schema"]["fait"] and not manquants,
                "detail": "complets" if not manquants else f"vides : {', '.join(manquants)}",
            }
    except Exception as erreur:  # noqa: BLE001 - the reason is the payload of this call
        etats["connexion"] = {"fait": False, "detail": str(erreur)[:200]}
        for code in ("vide", "schema", "referentiels"):
            etats.setdefault(code, {"fait": False, "detail": "non vérifié"})

    return {
        "version_attendue": version_attendue(),
        "etapes": [
            {
                "code": e.code,
                "libelle": e.libelle,
                "manuel": e.manuel,
                **etats.get(e.code, {"fait": False, "detail": "à faire depuis la console"}),
            }
            for e in ETAPES
        ],
    }


def semer_referentiels(dsn: str, role: str | None = None) -> dict[str, Any]:
    """Copy the reference tables into a target that has the schema and no data.

    Copied from this API's own database rather than from a fixture, because that is the
    only copy certainly up to date: a hard-coded list here would be a second definition
    and would age silently.

    Credentials are never copied. Seeding a new client with another organisation's API
    keys would hand over its ability to send mail under its name.
    """
    diagnostic = diagnostiquer(dsn)
    par_code = {e["code"]: e for e in diagnostic["etapes"]}
    if not par_code["connexion"]["fait"]:
        return {"fait": False, "motif": f"Base injoignable : {par_code['connexion']['detail']}"}
    if par_code["vide"].get("bloquant"):
        return {"fait": False, "motif": "La base contient déjà des membres. Semer ici mêlerait deux organisations."}
    if not par_code["schema"]["fait"]:
        return {"fait": False, "motif": f"Le schéma n'est pas à la version attendue ({par_code['schema']['detail']})."}

    copiees: dict[str, int] = {}
    with _ouvrir(dsn) as cible:
        for table in REFERENTIELS:
            lignes = db.fetch_all(f"SELECT * FROM {table}", (), role=role)
            if not lignes:
                continue
            colonnes = list(lignes[0].keys())
            place = ", ".join(["%s"] * len(colonnes))
            noms = ", ".join(f'"{c}"' for c in colonnes)
            with cible.cursor() as cur:
                cur.execute(f"SELECT count(*) AS n FROM {table}")
                if int((cur.fetchone() or {}).get("n") or 0) > 0:
                    copiees[table] = 0  # already seeded; re-running must change nothing
                    continue
                for ligne in lignes:
                    cur.execute(
                        f"INSERT INTO {table} ({noms}) VALUES ({place}) ON CONFLICT DO NOTHING",
                        tuple(ligne[c] for c in colonnes),
                    )
            copiees[table] = len(lignes)
        cible.commit()
    return {"fait": True, "copiees": copiees, "jamais_copie": list(JAMAIS_COPIE)}


def etat_du_parc(role: str | None = None) -> dict[str, Any]:
    """Where every registered organisation stands against the expected schema version.

    A platform running many databases has a new failure the single-tenant one never had:
    a migration applied to some and not others. Nothing shows it, because each client's
    application works fine on its own version until a feature written for the new schema
    reaches an old one. By then the symptom is a five hundred on one client and nowhere
    else, which is the hardest kind of fault to diagnose.

    Read-only, and every target is probed independently: one unreachable database must
    not hide the state of the others, so a failure is reported as that organisation's
    state rather than as an error for the whole answer.
    """
    attendue = version_attendue()
    lignes = db.fetch_all(
        "SELECT o.code, o.nom, o.etat, h.hote, h.dsn "
        "FROM organisation_cliente o LEFT JOIN organisation_hote h ON h.organisation_id = o.id "
        "WHERE o.etat <> 'resiliee' ORDER BY o.nom, h.hote",
        (),
        role=role,
    )

    bases: list[dict[str, Any]] = []
    vues: set[str] = set()
    for r in lignes:
        dsn = str(r["dsn"] or "").strip()
        # An organisation with no dedicated connection is on the historical database,
        # which this process is already connected to and therefore already at head.
        cible = dsn or "(base historique)"
        if cible in vues:
            continue
        vues.add(cible)
        if not dsn:
            bases.append({
                "code": r["code"], "nom": r["nom"], "hote": r["hote"],
                "base": cible, "version": attendue, "a_jour": True, "joignable": True,
            })
            continue
        try:
            with _ouvrir(dsn) as conn, conn.cursor() as cur:
                cur.execute("SELECT to_regclass('public.alembic_version') IS NOT NULL AS a")
                if not (cur.fetchone() or {}).get("a"):
                    version = ""
                else:
                    cur.execute("SELECT version_num FROM alembic_version")
                    version = str((cur.fetchone() or {}).get("version_num") or "")
            bases.append({
                "code": r["code"], "nom": r["nom"], "hote": r["hote"], "base": "dédiée",
                "version": version or "aucune", "a_jour": version == attendue, "joignable": True,
            })
        except Exception as erreur:  # noqa: BLE001 - one unreachable target must not hide the rest
            bases.append({
                "code": r["code"], "nom": r["nom"], "hote": r["hote"], "base": "dédiée",
                "version": "inconnue", "a_jour": False, "joignable": False,
                "erreur": str(erreur)[:160],
            })

    en_retard = [b for b in bases if b["joignable"] and not b["a_jour"]]
    return {
        "version_attendue": attendue,
        "bases": bases,
        "total": len(bases),
        "a_jour": sum(1 for b in bases if b["a_jour"]),
        "en_retard": len(en_retard),
        "injoignables": sum(1 for b in bases if not b["joignable"]),
        "commande": "Depuis deployment/database : DATABASE_URL=<dsn> python -m alembic upgrade head",
    }
