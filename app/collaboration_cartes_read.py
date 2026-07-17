"""Batched read assembly for collaboration cards.

Building a board means turning many card rows into the deeply nested
``CarteProtoOut`` the front expects: labels, members, checklists with per item
assignees, attachments, a comment thread (with reactions, mentions and read
receipts) and the activity feed. Assembling that one card at a time issued dozens
of round trips per board, each on its own short lived connection to the remote
pooler, which dominated board load time. This module loads every relation for a
whole set of cards with a handful of set based queries on a single connection,
then assembles the cards in memory.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from . import attachments_core, db


class ReactionOut(BaseModel):
    membre_id: str
    type: str


class PieceOut(BaseModel):
    id: str
    nom: str
    taille: int
    type: str
    cree_le: str | None = None
    data_url: str | None = None
    couverture: bool = False


class CommentaireProtoOut(BaseModel):
    id: str
    auteur_id: str
    corps: str
    cree_le: str | None = None
    edite_le: str | None = None
    reactions: list[ReactionOut]
    pieces: list[PieceOut]
    lu_par: list[str]
    mentions: list[str]


class ChecklistItemOut(BaseModel):
    id: str
    texte: str
    fait: bool
    assigne_id: str | None = None  # kept for backward compatibility (first assignee)
    assignes: list[str] = []
    echeance: str | None = None


class ChecklistOut(BaseModel):
    id: str
    titre: str
    items: list[ChecklistItemOut]


class ActiviteOut(BaseModel):
    id: str
    auteur_id: str
    cree_le: str | None = None
    texte: str


class CarteProtoOut(BaseModel):
    id: str
    tableau_id: str
    colonne_id: str
    numero: int
    titre: str
    description: str
    etiquettes: list[str]
    assignes: list[str]
    suiveurs: list[str]
    debut: str | None = None
    echeance: str | None = None
    rappel: str
    priorite: str
    complexite: int | None = None
    position: int
    checklists: list[ChecklistOut]
    pieces: list[PieceOut]
    commentaires: list[CommentaireProtoOut]
    activite: list[ActiviteOut]
    archive: bool
    modele: bool
    couverture_id: str | None = None
    publie: bool = False
    evenement_id: str | None = None


_CARTE_COLS = (
    "id, tableau_id, colonne_id, numero, titre, description, debut, echeance, rappel, "
    "priorite, complexite, position, archive, modele, couverture_id, publie, evenement_id"
)


def _iso(v: Any) -> str | None:
    return v.isoformat() if isinstance(v, datetime) else None


def _piece_cols() -> str:
    # storage_path only exists once migration 0105 is applied (with the bucket). In
    # inline mode the column is never referenced, so this works either way.
    return (
        "id, nom, taille, type, url, storage_path, cree_le, carte_id, commentaire_id"
        if attachments_core.bucket()
        else "id, nom, taille, type, url, cree_le, carte_id, commentaire_id"
    )


def _piece_out(p: dict[str, Any], couverture_id: str | None, served: dict[str, str]) -> PieceOut:
    return PieceOut(
        id=str(p["id"]),
        nom=p["nom"],
        taille=int(p["taille"]),
        type=p["type"],
        cree_le=_iso(p["cree_le"]),
        data_url=served.get(str(p["id"]), ""),
        couverture=(couverture_id is not None and str(p["id"]) == couverture_id),
    )


def _group(rows: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        grouped[str(r[key])].append(r)
    return grouped


_ITEMS_SQL = (
    "SELECT ci.checklist_id, ci.id, ci.texte, ci.fait, ci.assigne_id, ci.echeance, "
    "COALESCE(array_agg(ia.utilisateur_id::text) FILTER (WHERE ia.utilisateur_id IS NOT NULL), '{}') AS assignes "
    "FROM collab_checklist_item ci LEFT JOIN collab_item_assigne ia ON ia.item_id = ci.id "
    "WHERE ci.checklist_id = ANY(%s) "
    "GROUP BY ci.id, ci.checklist_id, ci.texte, ci.fait, ci.assigne_id, ci.echeance, ci.position "
    "ORDER BY ci.position"
)


@dataclass
class _Bundles:
    etiquettes: dict[str, list[dict[str, Any]]]
    membres: dict[str, list[dict[str, Any]]]
    activite: dict[str, list[dict[str, Any]]]
    checklists: dict[str, list[dict[str, Any]]]
    items: dict[str, list[dict[str, Any]]]
    pieces: dict[str, list[dict[str, Any]]]
    comments: dict[str, list[dict[str, Any]]]
    reactions: dict[str, list[dict[str, Any]]]
    mentions: dict[str, list[dict[str, Any]]]
    lus: dict[str, list[dict[str, Any]]]
    com_pieces: dict[str, list[dict[str, Any]]]
    served: dict[str, str]  # piece id -> fetch URL, resolved in one bulk sign call


def _load_bundles(ids: list[Any], role: str) -> _Bundles:
    """Load every relation for the given card ids with set based queries, all on a
    single connection, and index them by their parent id for in memory assembly."""
    pc = _piece_cols()
    empty: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with db.connection(role=role) as conn:
        def q(sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()

        etiquettes = _group(
            q("SELECT carte_id, etiquette_id FROM collab_carte_etiquette WHERE carte_id = ANY(%s)", (ids,)), "carte_id"
        )
        membres = _group(
            q("SELECT carte_id, utilisateur_id, role FROM collab_carte_membre WHERE carte_id = ANY(%s)", (ids,)),
            "carte_id",
        )
        activite = _group(
            q(
                "SELECT carte_id, id, auteur_id, texte, cree_le FROM collab_activite "
                "WHERE carte_id = ANY(%s) ORDER BY cree_le",
                (ids,),
            ),
            "carte_id",
        )
        checklists = _group(
            q("SELECT carte_id, id, titre FROM collab_checklist WHERE carte_id = ANY(%s) ORDER BY position", (ids,)),
            "carte_id",
        )
        clids = [cl["id"] for group in checklists.values() for cl in group]
        items = _group(q(_ITEMS_SQL, (clids,)), "checklist_id") if clids else empty
        pieces = _group(
            q(f"SELECT {pc} FROM collab_piece WHERE carte_id = ANY(%s) ORDER BY cree_le", (ids,)), "carte_id"
        )
        comments = _group(
            q(
                "SELECT carte_id, id, auteur_id, corps, cree_le, edite_le FROM collab_commentaire "
                "WHERE carte_id = ANY(%s) ORDER BY cree_le",
                (ids,),
            ),
            "carte_id",
        )
        comids = [c["id"] for group in comments.values() for c in group]
        reactions = mentions = lus = com_pieces = empty
        if comids:
            reactions = _group(
                q("SELECT commentaire_id, utilisateur_id, type FROM collab_reaction WHERE commentaire_id = ANY(%s)",
                  (comids,)),
                "commentaire_id",
            )
            mentions = _group(
                q("SELECT commentaire_id, utilisateur_id FROM collab_commentaire_mention "
                  "WHERE commentaire_id = ANY(%s)", (comids,)),
                "commentaire_id",
            )
            lus = _group(
                q("SELECT commentaire_id, utilisateur_id FROM collab_commentaire_lu WHERE commentaire_id = ANY(%s)",
                  (comids,)),
                "commentaire_id",
            )
            com_pieces = _group(
                q(f"SELECT {pc} FROM collab_piece WHERE commentaire_id = ANY(%s) ORDER BY cree_le", (comids,)),
                "commentaire_id",
            )
    # Resolve every attachment URL in a single bulk signing request (outside the DB
    # connection), instead of one storage round trip per piece.
    all_pieces = [p for group in pieces.values() for p in group]
    all_pieces += [p for group in com_pieces.values() for p in group]
    served = attachments_core.served_urls(all_pieces)
    return _Bundles(
        etiquettes, membres, activite, checklists, items, pieces, comments, reactions, mentions, lus, com_pieces,
        served,
    )


def _build_checklists(cid: str, b: _Bundles) -> list[ChecklistOut]:
    out: list[ChecklistOut] = []
    for cl in b.checklists.get(cid, []):
        items = [
            ChecklistItemOut(
                id=str(it["id"]),
                texte=it["texte"],
                fait=bool(it["fait"]),
                assigne_id=str(it["assigne_id"]) if it["assigne_id"] else None,
                assignes=[str(a) for a in (it["assignes"] or [])],
                echeance=_iso(it["echeance"]),
            )
            for it in b.items.get(str(cl["id"]), [])
        ]
        out.append(ChecklistOut(id=str(cl["id"]), titre=cl["titre"], items=items))
    return out


def _build_comments(cid: str, b: _Bundles) -> list[CommentaireProtoOut]:
    out: list[CommentaireProtoOut] = []
    for c in b.comments.get(cid, []):
        coid = str(c["id"])
        out.append(
            CommentaireProtoOut(
                id=coid,
                auteur_id=str(c["auteur_id"]) if c["auteur_id"] else "",
                corps=c["corps"],
                cree_le=_iso(c["cree_le"]),
                edite_le=_iso(c["edite_le"]),
                reactions=[
                    ReactionOut(membre_id=str(r["utilisateur_id"]), type=r["type"]) for r in b.reactions.get(coid, [])
                ],
                pieces=[_piece_out(p, None, b.served) for p in b.com_pieces.get(coid, [])],
                lu_par=[str(x["utilisateur_id"]) for x in b.lus.get(coid, [])],
                mentions=[str(x["utilisateur_id"]) for x in b.mentions.get(coid, [])],
            )
        )
    return out


def _build_carte(row: dict[str, Any], b: _Bundles) -> CarteProtoOut:
    cid = str(row["id"])
    couverture_id = str(row["couverture_id"]) if row["couverture_id"] else None
    membres = b.membres.get(cid, [])
    return CarteProtoOut(
        id=cid,
        tableau_id=str(row["tableau_id"]),
        colonne_id=str(row["colonne_id"]),
        numero=int(row["numero"]) if row["numero"] is not None else 0,
        titre=row["titre"],
        description=row["description"] or "",
        etiquettes=[str(e["etiquette_id"]) for e in b.etiquettes.get(cid, [])],
        assignes=[str(m["utilisateur_id"]) for m in membres if m["role"] == "assigne"],
        suiveurs=[str(m["utilisateur_id"]) for m in membres if m["role"] == "suiveur"],
        debut=_iso(row["debut"]),
        echeance=_iso(row["echeance"]),
        rappel=row["rappel"] or "aucun",
        priorite=row["priorite"] or "normale",
        complexite=int(row["complexite"]) if row["complexite"] is not None else None,
        position=int(row["position"]),
        checklists=_build_checklists(cid, b),
        pieces=[_piece_out(p, couverture_id, b.served) for p in b.pieces.get(cid, [])],
        commentaires=_build_comments(cid, b),
        activite=[
            ActiviteOut(
                id=str(a["id"]),
                auteur_id=str(a["auteur_id"]) if a["auteur_id"] else "",
                cree_le=_iso(a["cree_le"]),
                texte=a["texte"],
            )
            for a in b.activite.get(cid, [])
        ],
        archive=bool(row["archive"]),
        modele=bool(row["modele"]),
        couverture_id=couverture_id,
        publie=bool(row["publie"]),
        evenement_id=str(row["evenement_id"]) if row["evenement_id"] else None,
    )


def assemble_cartes(rows: list[dict[str, Any]], role: str) -> list[CarteProtoOut]:
    """Assemble the full nested cards for a set of card rows using batched,
    set based queries on a single connection (replacing the per card N+1)."""
    if not rows:
        return []
    ids = [r["id"] for r in rows]
    bundles = _load_bundles(ids, role)
    return [_build_carte(r, bundles) for r in rows]


def carte_out(row: dict[str, Any], role: str) -> CarteProtoOut:
    """Assemble a single card. Kept for callers that build one card at a time."""
    return assemble_cartes([row], role)[0]
