"""RGPD minimisation guard on the published organisation chart. When the direction
sets afficher_photo=false on a node, the person's photo must NOT be shown; the
published read (served to every authenticated member and collaborator) must therefore
drop the photo URL entirely so it cannot leak. The back-office editor keeps it to
preview the toggle (default behaviour, flag off)."""
from __future__ import annotations

import uuid

from app.organigramme_core import node_dict


def _row(afficher_photo: bool) -> dict[str, object]:
    return {"id": uuid.uuid4(), "afficher_photo": afficher_photo, "photo_url": "abc/photo.jpg"}


def test_published_read_drops_photo_when_not_displayed() -> None:
    node = node_dict(_row(afficher_photo=False), masquer_photo_non_affichee=True)
    assert node["photo_url"] is None
    assert node["afficher_photo"] is False


def test_published_read_keeps_photo_when_displayed() -> None:
    node = node_dict(_row(afficher_photo=True), masquer_photo_non_affichee=True)
    assert node["photo_url"] == "abc/photo.jpg"
    assert node["afficher_photo"] is True


def test_editor_read_keeps_photo_even_when_not_displayed() -> None:
    # Default (back-office editor): the URL stays so the editor can preview the toggle.
    node = node_dict(_row(afficher_photo=False))
    assert node["photo_url"] == "abc/photo.jpg"
    assert node["afficher_photo"] is False
