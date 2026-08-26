"""Le centre d'aide ne doit jamais servir ce que le lecteur n'a pas le droit de lire.

Trois filtres commandent chaque lecture : le côté client, la permission qui
gouverne l'écran décrit, et le module réellement souscrit. Ce qui est vérifié ici,
c'est que ces trois filtres sont dans la requête envoyée à PostgreSQL, sur toutes
les routes de lecture sans exception. Une route qui les oublie ne se voit pas à la
relecture : elle rend simplement un peu plus d'articles que les autres.

La preuve de bout en bout contre une vraie base attend l'application de la
migration 0200. Ces tests-ci attrapent la régression qui la précède : une nouvelle
route de lecture écrite sans la clause partagée.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app import aide
from app.schemas import UserMe


class Requete:
    """Le strict nécessaire de ce qu'une route lit d'une requête HTTP.

    Les fonctions sont appelées directement plutôt que par un client de test :
    l'application résout l'organisation depuis l'hôte à chaque requête, ce qui exige
    une base joignable et ferait dépendre ces tests du réseau. Ce qui est vérifié
    ici est la requête SQL construite, qui ne dépend d'aucun des deux.
    """

    def __init__(self, entetes: dict[str, str] | None = None) -> None:
        self.headers = entetes or {}


def _appeler(chemin: str):
    """Router un chemin vers sa fonction, pour garder les tests lisibles."""
    if chemin.startswith("/api/v1/aide/rubriques"):
        return aide.rubriques(Requete())
    if chemin.startswith("/api/v1/aide/articles"):
        return aide.articles(Requete())
    if chemin.startswith("/api/v1/aide/ecran/"):
        return aide.par_ecran(chemin.rsplit("/", 1)[-1], Requete())
    if chemin.startswith("/api/v1/aide/recherche"):
        return aide.recherche(Requete(), q="cotisation")
    raise AssertionError(f"chemin non routé : {chemin}")


class BaseObservee:
    """Retient chaque requête et ses paramètres, et rend des lignes fixes."""

    def __init__(self, lignes=None, une_ligne=None):
        self.lignes = lignes if lignes is not None else []
        # Une file, pas une valeur unique : lire un article demande deux requetes,
        # l'entete puis sa version, et rendre la meme ligne aux deux masquerait
        # une confusion entre les deux.
        self.reponses: list = [une_ligne] if une_ligne is not None else []
        self.requetes: list[tuple[str, tuple]] = []

    @property
    def une_ligne(self):
        return self.reponses[0] if self.reponses else None

    @une_ligne.setter
    def une_ligne(self, valeur):
        self.reponses = [valeur] if valeur is not None else []

    def fetch_all(self, sql, params=(), role=None):  # noqa: ARG002
        self.requetes.append((sql, params))
        return self.lignes

    def fetch_one(self, sql, params=(), role=None):  # noqa: ARG002
        self.requetes.append((sql, params))
        return self.reponses.pop(0) if self.reponses else None

    def execute(self, sql, params=(), role=None):  # noqa: ARG002
        self.requetes.append((sql, params))
        return None


@pytest.fixture
def base(monkeypatch):
    observee = BaseObservee()
    monkeypatch.setattr(aide, "db", observee)
    monkeypatch.setattr(aide.modules_souscrits, "souscriptions", lambda: {"back-office"})
    return observee


def _lecteur(monkeypatch, role="membre", permissions=frozenset()):
    utilisateur = UserMe(id="11111111-1111-4111-8111-111111111111",
                         email="lecteur@exemple.org", role=role)
    monkeypatch.setattr(aide, "lecteur_eventuel", lambda requete: utilisateur)
    monkeypatch.setattr(aide, "permissions_effectives", lambda u: permissions)
    return utilisateur


LECTURES = (
    "/api/v1/aide/rubriques",
    "/api/v1/aide/articles",
    "/api/v1/aide/ecran/back-office.membres",
    "/api/v1/aide/recherche?q=cotisation",
)


@pytest.mark.parametrize("chemin", LECTURES)
def test_toute_lecture_est_bornee_au_cote_client(base, chemin):
    """Un guide de l'éditeur n'a aucun chemin vers une route cliente."""
    _appeler(chemin)
    sql = base.requetes[0][0]
    assert "a.cote = 'client'" in sql


@pytest.mark.parametrize("chemin", LECTURES)
def test_toute_lecture_filtre_la_permission_et_le_module(base, chemin):
    _appeler(chemin)
    sql = base.requetes[0][0]
    assert "a.permission_requise IS NULL OR a.permission_requise = ANY(%s)" in sql
    assert "a.module_requis IS NULL OR a.module_requis = ANY(%s)" in sql


@pytest.mark.parametrize("chemin", LECTURES)
def test_toute_lecture_ecarte_ce_que_l_organisation_a_masque(base, chemin):
    _appeler(chemin)
    assert "aide_reglage_local r WHERE r.cle_article = a.cle AND r.masque" in base.requetes[0][0]


@pytest.mark.parametrize("chemin", LECTURES)
def test_toute_lecture_ne_sert_que_des_articles_publies(base, chemin):
    _appeler(chemin)
    assert "a.statut = 'publie'" in base.requetes[0][0]


def test_un_visiteur_sans_jeton_ne_voit_que_le_corpus_public(base):
    aide.articles(Requete())
    parametres = base.requetes[0][1]
    assert ["public"] in [p for p in parametres if isinstance(p, list)]


def test_un_membre_voit_le_corpus_membre_mais_pas_la_gouvernance(base, monkeypatch):
    _lecteur(monkeypatch, role="membre")
    aide.articles(Requete())
    visibilites = next(p for p in base.requetes[0][1] if isinstance(p, list) and "public" in p)
    assert visibilites == ["public", "membres"]


def test_un_administrateur_voit_les_articles_de_gouvernance(base, monkeypatch):
    _lecteur(monkeypatch, role="admin")
    aide.articles(Requete())
    visibilites = next(p for p in base.requetes[0][1] if isinstance(p, list) and "public" in p)
    assert "gouvernance" in visibilites


def test_les_permissions_du_lecteur_sont_liees_et_non_interpolees(base, monkeypatch):
    """Une permission recopiée dans le texte de la requête serait une injection."""
    _lecteur(monkeypatch, permissions=frozenset({"membre.consulter"}))
    aide.articles(Requete())
    sql, parametres = base.requetes[0]
    assert "membre.consulter" not in sql
    assert ["membre.consulter"] in [p for p in parametres if isinstance(p, list)]


def test_le_module_non_souscrit_ne_peut_pas_apparaitre(base, monkeypatch):
    monkeypatch.setattr(aide.modules_souscrits, "souscriptions", lambda: {"back-office"})
    aide.articles(Requete())
    modules = next(
        p for p in base.requetes[0][1] if isinstance(p, list) and "back-office" in p)
    assert modules == ["back-office"]


# ------------------------------------------------------------------- recherche


def test_le_pliage_des_accents_est_celui_de_la_migration():
    """Sans pliage identique des deux côtés, une recherche correcte ne trouve rien."""
    assert aide.plier("présence") == "presence"
    assert aide.plier("Cotisation à jour") == "Cotisation a jour"
    assert aide.plier("État civil") == "Etat civil"


def test_une_recherche_trop_courte_ne_touche_pas_la_base(base):
    assert aide.recherche(Requete(), q="a") == []
    assert base.requetes == []


def test_la_recherche_plie_la_requete_avant_de_l_envoyer(base):
    aide.recherche(Requete(), q="présence")
    parametres = base.requetes[0][1]
    assert "presence" in parametres
    assert "présence" not in parametres


def test_la_recherche_n_utilise_aucune_fonction_a_privileges(base):
    """Une fonction SECURITY DEFINER échapperait à la sécurité au niveau ligne."""
    aide.recherche(Requete(), q="cotisation")
    sql = base.requetes[0][0].upper()
    assert "SECURITY DEFINER" not in sql
    assert "SELECT" in sql


def test_la_recherche_est_bornee_en_nombre_de_resultats(base):
    aide.recherche(Requete(), q="cotisation")
    assert "LIMIT %s" in base.requetes[0][0]
    assert aide.LIMITE_RECHERCHE in base.requetes[0][1]


# --------------------------------------------------------------------- article


def test_un_article_invisible_rend_le_meme_refus_qu_un_article_absent(base):
    """Distinguer les deux laisserait cartographier le catalogue en essayant des clés."""
    base.une_ligne = None
    with pytest.raises(HTTPException) as capture:
        aide.article("cle-inconnue", Requete())
    assert capture.value.status_code == 404
    assert capture.value.detail == "article introuvable"


def test_le_corps_vient_de_la_derniere_version_publiee(base):
    base.une_ligne = {
        "id": "22222222-2222-4222-8222-222222222222", "cle": "pointer-un-membre",
        "slug": "pointer-un-membre", "titre": "Pointer un membre", "extrait": "",
        "application_code": "controleur", "ordre": 10, "publie_le": None,
        "rubrique": "presence",
    }
    base.reponses.append({"version": 3, "blocs": [
        {"type": "paragraphe", "texte": "Ouvrir la liste, puis toucher le nom."}]})
    article = aide.article("pointer-un-membre", Requete())
    assert article.titre == "Pointer un membre"
    sql_version = base.requetes[1][0]
    assert "publie_le IS NOT NULL" in sql_version
    assert "ORDER BY version DESC" in sql_version


# ----------------------------------------------------------------------- usage


def test_un_type_d_evenement_inconnu_est_refuse(base):
    with pytest.raises(HTTPException) as capture:
        aide.enregistrer_usage(aide.EvenementUsage(type="inventé"), Requete())
    assert capture.value.status_code == 422
    assert base.requetes == []


def test_une_recherche_sans_resultat_est_enregistree(base):
    aide.enregistrer_usage(
        aide.EvenementUsage(type="recherche", requete="rembourser", resultats=0),
        Requete())
    sql, parametres = base.requetes[0]
    assert "INSERT INTO aide_usage" in sql
    assert "rembourser" in parametres
    assert 0 in parametres
