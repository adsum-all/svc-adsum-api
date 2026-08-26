"""The help centre, read side: one corpus, filtered by who is asking.

There is no second catalogue for the central desk. Asking without naming an
application returns everything the reader may see; naming one narrows it. A second
catalogue would drift from the first within a month, and the reader would land on
the stale one.

Three filters decide what comes back, and all three run in SQL rather than in the
browser.

**Side.** Only ``cote = 'client'`` is ever served here. Editor guides live behind
``require_capacite`` in the console, never behind a permission: a permission is a
role inside a client organisation, and treating one as editor authority is the exact
hole that ``frontiere.py`` was written to close.

**Permission.** An article documenting a screen the reader cannot open is noise at
best and a support ticket at worst. The column mirrors the navigation registry, and
the match happens server side: the back office caches its session in localStorage,
so a browser-side filter would serve yesterday's rights.

**Subscribed module.** An article describing a module the organisation did not buy
generates precisely the ticket the help centre exists to avoid. Such an article is
excluded from lists and from search rather than refused with a status code, because
a refusal on a help page teaches the reader nothing.

Reading is open to a visitor with no token. The person who cannot sign in is the one
who most needs the page, so an absent or expired token yields the public corpus
instead of a refusal.
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

from . import db, modules_souscrits
from .auth import current_user
from .permissions_rbac import permissions_effectives
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/aide", tags=["aide"])

#: Accent folding, character for character the same table as migration 0200. The
#: stored vector is folded, so the query has to be folded too: without this, a
#: search for "presence" never matches an article titled "Presence" written with
#: its accent, which is every article written properly.
ACCENTUES = "ÉÈÊËéèêëÀÂÁàâáÎÏîïÔÖÓôöóÙÛÜùûüÇç"
PLATS = "EEEEeeeeAAAaaaIIiiOOOoooUUUuuuCc"
_PLIAGE = str.maketrans(ACCENTUES, PLATS)

#: Below two characters every query matches half the corpus, which reads as a
#: broken search rather than a broad one.
LONGUEUR_MIN_RECHERCHE = 2
LIMITE_RECHERCHE = 25

LANGUES = ("fr", "en")

#: Articles belonging to no single application: signing in, two factor
#: authentication, personal data rights. They are always added to whichever
#: application is asked for, because the reader looking for how to sign in is in
#: front of one application and has no reason to guess that the answer was filed
#: under another.
TRANSVERSE = "transverse"

#: A governance article is visible to an administrator only. This is a base role
#: and not a delegated permission on purpose: delegation is what let a tagged
#: membership widen its own reach in the past.
ROLES_GOUVERNANCE = ("super_admin", "admin")


def plier(texte: str) -> str:
    """Fold accents the way the stored search vector was folded."""
    return texte.translate(_PLIAGE)


def lecteur_eventuel(request: Request) -> UserMe | None:
    """The reader, when there is one. Never a refusal.

    An expired token must not close the help centre: someone whose session just
    died is exactly the person looking for how to sign in again. They get the
    public corpus, which is what a visitor sees.
    """
    entete = request.headers.get("authorization", "")
    if not entete.lower().startswith("bearer "):
        return None
    identifiants = HTTPAuthorizationCredentials(
        scheme="Bearer", credentials=entete[7:].strip())
    try:
        return current_user(identifiants)
    except HTTPException:
        return None


class Rubrique(BaseModel):
    code: str
    titre: str
    description: str = ""
    application_code: str
    ordre: int = 50
    articles: int = 0


class ArticleResume(BaseModel):
    cle: str
    slug: str
    titre: str
    extrait: str = ""
    rubrique: str
    application_code: str
    ordre: int = 50


class Bloc(BaseModel):
    """One typed block of an article body.

    Typed blocks rather than Markdown rendered raw: the reference project stores
    Markdown and prints it verbatim, so its readers see the asterisks. Typing the
    blocks also keeps the renderer a short component instead of a parser.
    """

    type: str
    texte: str = ""
    elements: list[str] = Field(default_factory=list)
    ecran: str = ""


class Article(ArticleResume):
    blocs: list[Bloc] = Field(default_factory=list)
    version: int = 0
    publie_le: str | None = None


class EvenementUsage(BaseModel):
    type: str
    application: str = ""
    cle_ecran: str = ""
    article: str = ""
    requete: str = ""
    resultats: int | None = None
    utile: bool | None = None
    commentaire: str = Field(default="", max_length=2000)


def _contexte(lecteur: UserMe | None) -> tuple[list[str], list[str], bool]:
    """What the reader may see: permissions, subscribed modules, governance."""
    if lecteur is None:
        return [], sorted(modules_souscrits.souscriptions()), False
    return (
        sorted(permissions_effectives(lecteur)),
        sorted(modules_souscrits.souscriptions()),
        lecteur.role in ROLES_GOUVERNANCE,
    )


def _visibilites(lecteur: UserMe | None, gouvernance: bool) -> list[str]:
    if lecteur is None:
        return ["public"]
    return ["public", "membres", "gouvernance"] if gouvernance else ["public", "membres"]


def _clause_lisible(
    lecteur: UserMe | None, permissions: list[str], modules: list[str], gouvernance: bool,
) -> tuple[str, list[Any]]:
    """The SQL predicate every read shares, plus its bound parameters.

    Written once and reused so a list and a search can never disagree about what is
    visible. Two predicates drift, and the one that drifts is the one that leaks.
    """
    clause = (
        " a.cote = 'client'"
        " AND a.statut = 'publie'"
        " AND a.visibilite = ANY(%s)"
        " AND (a.permission_requise IS NULL OR a.permission_requise = ANY(%s))"
        " AND (a.module_requis IS NULL OR a.module_requis = ANY(%s))"
        " AND NOT EXISTS ("
        "   SELECT 1 FROM aide_reglage_local r WHERE r.cle_article = a.cle AND r.masque)"
    )
    return clause, [
        _visibilites(lecteur, gouvernance),
        permissions,
        modules,
    ]


def _lignes_en_resumes(lignes: list[dict[str, Any]]) -> list[ArticleResume]:
    return [
        ArticleResume(
            cle=str(ligne["cle"]), slug=str(ligne["slug"]), titre=str(ligne["titre"]),
            extrait=str(ligne.get("extrait") or ""),
            rubrique=str(ligne.get("rubrique") or ""),
            application_code=str(ligne["application_code"]),
            ordre=int(ligne.get("ordre_effectif") or ligne.get("ordre") or 50),
        )
        for ligne in lignes
    ]


@router.get("/rubriques", response_model=list[Rubrique])
def rubriques(
    request: Request,
    application: Annotated[str, Query(max_length=40)] = "",
    langue: Annotated[str, Query(max_length=2)] = "fr",
) -> list[Rubrique]:
    """The rubrics that actually hold something the reader may see.

    An empty rubric is never served: it promises an answer and gives none, and the
    reader concludes the help centre is empty rather than that this corner of it is.
    """
    lecteur = lecteur_eventuel(request)
    permissions, modules, gouvernance = _contexte(lecteur)
    clause, params = _clause_lisible(lecteur, permissions, modules, gouvernance)

    sql = (
        "SELECT r.code, r.titre, r.titre_en, r.description, r.application_code, r.ordre,"
        "       count(a.id) AS articles"
        " FROM aide_rubrique r"
        " JOIN aide_article a ON a.rubrique_id = r.id AND a.langue = %s AND" + clause +
        " WHERE r.actif AND r.cote = 'client'"
    )
    valeurs: list[Any] = [_langue(langue), *params]
    if application:
        sql += " AND r.application_code IN (%s, %s)"
        valeurs.extend([application, TRANSVERSE])
    sql += " GROUP BY r.code, r.titre, r.titre_en, r.description, r.application_code, r.ordre"
    sql += " HAVING count(a.id) > 0 ORDER BY r.ordre, r.titre"

    lignes = db.fetch_all(sql, tuple(valeurs), role=_role(lecteur))
    return [
        Rubrique(
            code=str(ligne["code"]),
            titre=(
                str(ligne["titre_en"] or ligne["titre"]) if langue == "en"
                else str(ligne["titre"])
            ),
            description=str(ligne.get("description") or ""),
            application_code=str(ligne["application_code"]),
            ordre=int(ligne["ordre"]), articles=int(ligne["articles"]),
        )
        for ligne in lignes
    ]


@router.get("/articles", response_model=list[ArticleResume])
def articles(
    request: Request,
    application: Annotated[str, Query(max_length=40)] = "",
    rubrique: Annotated[str, Query(max_length=80)] = "",
    langue: Annotated[str, Query(max_length=2)] = "fr",
) -> list[ArticleResume]:
    """The catalogue. Without ``application`` this is the central desk."""
    lecteur = lecteur_eventuel(request)
    permissions, modules, gouvernance = _contexte(lecteur)
    clause, params = _clause_lisible(lecteur, permissions, modules, gouvernance)

    sql = (
        "SELECT a.cle, a.slug, a.titre, a.extrait, a.application_code, r.code AS rubrique,"
        # A local reordering wins over the catalogue order, and the catalogue order
        # is the fallback rather than an error when none was set.
        "       coalesce(l.ordre_local, a.ordre) AS ordre_effectif"
        " FROM aide_article a"
        " JOIN aide_rubrique r ON r.id = a.rubrique_id"
        " LEFT JOIN aide_reglage_local l ON l.cle_article = a.cle"
        " WHERE a.langue = %s AND" + clause
    )
    valeurs: list[Any] = [_langue(langue), *params]
    if application:
        sql += " AND a.application_code IN (%s, %s)"
        valeurs.extend([application, TRANSVERSE])
    if rubrique:
        sql += " AND r.code = %s"
        valeurs.append(rubrique)
    sql += " ORDER BY r.ordre, ordre_effectif, a.titre"

    return _lignes_en_resumes(db.fetch_all(sql, tuple(valeurs), role=_role(lecteur)))


@router.get("/ecran/{cle_ecran}", response_model=list[ArticleResume])
def par_ecran(
    cle_ecran: str, request: Request,
    langue: Annotated[str, Query(max_length=2)] = "fr",
) -> list[ArticleResume]:
    """What answers the screen the reader is standing on.

    This is the difference between a help centre and a catalogue: the reference
    project never had it, and its readers had to search for the page they were
    already looking at.
    """
    lecteur = lecteur_eventuel(request)
    permissions, modules, gouvernance = _contexte(lecteur)
    clause, params = _clause_lisible(lecteur, permissions, modules, gouvernance)

    sql = (
        "SELECT a.cle, a.slug, a.titre, a.extrait, a.application_code, r.code AS rubrique,"
        "       n.position AS ordre_effectif"
        " FROM aide_ancrage n"
        " JOIN aide_article a ON a.id = n.article_id AND a.langue = %s"
        " JOIN aide_rubrique r ON r.id = a.rubrique_id"
        " WHERE n.cle_ecran = %s AND" + clause +
        " ORDER BY n.est_principal DESC, n.position, a.titre"
    )
    valeurs = [_langue(langue), cle_ecran[:120], *params]
    return _lignes_en_resumes(db.fetch_all(sql, tuple(valeurs), role=_role(lecteur)))


@router.get("/recherche", response_model=list[ArticleResume])
def recherche(
    request: Request,
    q: Annotated[str, Query(max_length=200)] = "",
    application: Annotated[str, Query(max_length=40)] = "",
    langue: Annotated[str, Query(max_length=2)] = "fr",
) -> list[ArticleResume]:
    """Full-text search, ranked, under the same visibility rules as the lists.

    An ordinary query under RLS, deliberately not a SECURITY DEFINER function: such
    a function escapes row level security, and in the reference project that is
    exactly how the titles of internal guides ended up in public search results.
    """
    terme = q.strip()
    if len(terme) < LONGUEUR_MIN_RECHERCHE:
        return []

    lecteur = lecteur_eventuel(request)
    permissions, modules, gouvernance = _contexte(lecteur)
    clause, params = _clause_lisible(lecteur, permissions, modules, gouvernance)

    sql = (
        "SELECT a.cle, a.slug, a.titre, a.extrait, a.application_code, r.code AS rubrique,"
        "       ts_rank(a.recherche, websearch_to_tsquery('french', %s)) AS rang"
        " FROM aide_article a"
        " JOIN aide_rubrique r ON r.id = a.rubrique_id"
        " WHERE a.langue = %s"
        "   AND a.recherche @@ websearch_to_tsquery('french', %s) AND" + clause
    )
    plie = plier(terme)
    valeurs: list[Any] = [plie, _langue(langue), plie, *params]
    if application:
        sql += " AND a.application_code IN (%s, %s)"
        valeurs.extend([application, TRANSVERSE])
    sql += " ORDER BY rang DESC, a.titre LIMIT %s"
    valeurs.append(LIMITE_RECHERCHE)

    lignes = db.fetch_all(sql, tuple(valeurs), role=_role(lecteur))
    return _lignes_en_resumes(lignes)


@router.get("/articles/{cle}", response_model=Article)
def article(
    cle: str, request: Request,
    langue: Annotated[str, Query(max_length=2)] = "fr",
) -> Article:
    """One article and its published body.

    The body comes from the latest published version rather than from the article
    row: what a reader sees has to be something someone chose to publish, not a
    draft that happened to be saved.
    """
    lecteur = lecteur_eventuel(request)
    permissions, modules, gouvernance = _contexte(lecteur)
    clause, params = _clause_lisible(lecteur, permissions, modules, gouvernance)

    ligne = db.fetch_one(
        "SELECT a.id, a.cle, a.slug, a.titre, a.extrait, a.application_code, a.ordre,"
        "       a.publie_le, r.code AS rubrique"
        " FROM aide_article a"
        " JOIN aide_rubrique r ON r.id = a.rubrique_id"
        " WHERE a.cle = %s AND a.langue = %s AND" + clause,
        (cle[:120], _langue(langue), *params), role=_role(lecteur),
    )
    if not ligne:
        # The same answer whether the article does not exist or the reader may not
        # see it. Distinguishing the two would let anyone map the internal
        # catalogue by trying keys.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="article introuvable")

    version = db.fetch_one(
        "SELECT version, blocs FROM aide_article_version"
        " WHERE article_id = %s AND publie_le IS NOT NULL"
        " ORDER BY version DESC LIMIT 1",
        (str(ligne["id"]),), role=_role(lecteur),
    )
    blocs = [Bloc(**b) for b in (version["blocs"] if version else []) if isinstance(b, dict)]
    publie = ligne.get("publie_le")
    return Article(
        cle=str(ligne["cle"]), slug=str(ligne["slug"]), titre=str(ligne["titre"]),
        extrait=str(ligne.get("extrait") or ""), rubrique=str(ligne["rubrique"]),
        application_code=str(ligne["application_code"]), ordre=int(ligne["ordre"]),
        blocs=blocs, version=int(version["version"]) if version else 0,
        publie_le=publie.isoformat() if publie else None,
    )


@router.post("/usage", status_code=status.HTTP_204_NO_CONTENT)
def enregistrer_usage(evenement: EvenementUsage, request: Request) -> None:
    """Record what was looked for, chiefly what was looked for and not found.

    Written by the server and never shown back in the reading interface. A counter
    displayed but never incremented, which is what the reference project ships, is
    worse than no counter: someone will plan the editorial backlog with it.
    """
    if evenement.type not in ("ouverture", "recherche", "lecture", "avis", "escalade"):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="type inconnu")
    lecteur = lecteur_eventuel(request)
    db.execute(
        "INSERT INTO aide_usage (type, application, cle_ecran, article_id, requete,"
        " resultats, utile, commentaire, utilisateur_id, langue)"
        " VALUES (%s, %s, %s, (SELECT id FROM aide_article WHERE cle = %s LIMIT 1),"
        " %s, %s, %s, %s, %s, %s)",
        (
            evenement.type, evenement.application[:40], evenement.cle_ecran[:120],
            evenement.article[:120] or None, evenement.requete[:200],
            evenement.resultats, evenement.utile, evenement.commentaire[:2000],
            lecteur.id if lecteur else None, "fr",
        ),
        role=_role(lecteur),
    )


def _langue(valeur: str) -> str:
    """An unknown language falls back to French rather than returning nothing."""
    return valeur if valeur in LANGUES else "fr"


def _role(lecteur: UserMe | None) -> str | None:
    return lecteur.role if lecteur else None
