"""La frontière entre le monde client et le monde éditeur, prouvée par le refus.

Ces tests existent parce qu'une brèche réelle a été trouvée le 19 août 2026 : les
vingt routes de la console de l'éditeur étaient gardées par la permission
``support.traiter``, accordée d'office aux rôles ``admin`` et ``super_admin`` d'une
organisation cliente. Un administrateur de paroisse pouvait lister toutes les
organisations clientes d'ADSUM, en créer une, en suspendre une autre.

Chaque test ci-dessous rejoue une façon de franchir la frontière et vérifie qu'elle
échoue. Aucun ne consulte la base : les trois barrières se prononcent avant, ce qui
est exactement la propriété qu'on veut. Une barrière qui aurait besoin d'une requête
pour refuser tomberait avec la base.
"""
from __future__ import annotations

import os

import jwt
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("ADSUM_JWT_EDITEUR_SECRET", "secret-editeur-de-test-" + "e" * 24)

from app import frontiere  # noqa: E402
from app.main import app  # noqa: E402
from app.security import create_access_token  # noqa: E402

client = TestClient(app)

#: Un compte quelconque. Aucun de ces tests n'a besoin qu'il existe en base : tous
#: vérifient un refus qui se prononce sur le jeton, avant la moindre requête.
COMPTE = "0a59ba9a-7b16-4630-8e36-761bac40be97"

#: Les routes de console, avec la capacité que la politique leur attribue. La liste
#: est écrite ici et comparée à la table de routage réelle par le test de conformité :
#: si une route apparaît, disparaît ou change de garde, l'un des deux le dit.
ROUTES_CONSOLE = [
    ("GET", "/api/v1/support/console/organisations"),
    ("POST", "/api/v1/support/console/organisations"),
    ("GET", "/api/v1/support/console/fils"),
    ("GET", "/api/v1/support/console/synthese"),
    ("GET", "/api/v1/support/console/agents"),
    ("GET", "/api/v1/support/console/envois"),
]


@pytest.fixture(autouse=True)
def registre_vide(monkeypatch):
    """Chaque test part d'un registre d'opérateurs vide et le remplit s'il en a besoin.

    Sans ce montage, un test qui déclare un opérateur le laisserait déclaré pour les
    suivants, et un refus attendu passerait pour un succès.
    """
    monkeypatch.delenv("ADSUM_OPERATEURS_EDITEUR", raising=False)
    yield


def _declarer_operateur(monkeypatch, role: str = "editor-support-agent") -> str:
    monkeypatch.setenv("ADSUM_OPERATEURS_EDITEUR", f"{COMPTE}:{role}")
    return frontiere.creer_jeton_editeur(COMPTE, "operateur@editeur.test", "admin")


def _entete(jeton: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {jeton}"}


class TestUnJetonClientNOuvrePasLaConsole:
    """La première barrière : l'audience et la clé."""

    @pytest.mark.parametrize(("methode", "chemin"), ROUTES_CONSOLE)
    def test_le_jeton_d_un_super_admin_de_tenant_est_refuse(self, methode, chemin):
        """Le cas exact de la brèche : le compte le plus élevé d'une organisation.

        Son jeton dit « super_admin », ce qui était suffisant hier. Il est signé avec
        le secret des tenants et ne porte pas l'audience éditeur : les deux raisons
        suffisent, et aucune ne dépend d'un réglage qu'on pourrait oublier.
        """
        jeton = create_access_token(COMPTE, "super_admin")
        reponse = client.request(methode, chemin, headers=_entete(jeton), json={})
        assert reponse.status_code == 401, (
            f"{methode} {chemin} a répondu {reponse.status_code} à un jeton de tenant")

    def test_le_jeton_d_un_admin_de_tenant_est_refuse(self):
        jeton = create_access_token(COMPTE, "admin")
        reponse = client.get("/api/v1/support/console/organisations",
                             headers=_entete(jeton))
        assert reponse.status_code == 401

    def test_un_jeton_forge_avec_le_secret_des_tenants_est_refuse(self):
        """La barrière qui tient quand les autres ont cédé.

        Ce jeton porte la bonne audience, la bonne émission et un rôle éditeur
        plausible. Il ne lui manque que la clé. Si le secret des tenants fuitait, ce
        jeton serait exactement ce qu'un attaquant fabriquerait.
        """
        from app.config import settings

        contrefacon = jwt.encode(
            {"sub": COMPTE, "email": "faux@editeur.test",
             "role_editeur": "editor-super-admin", "role_bdd": "super_admin",
             "aud": frontiere.AUDIENCE_EDITEUR, "iss": "adsum-api",
             "exp": 9_999_999_999},
            settings.jwt_secret, algorithm=settings.jwt_algorithm)
        reponse = client.get("/api/v1/support/console/organisations",
                             headers=_entete(contrefacon))
        assert reponse.status_code == 401


class TestLeRegistreDecideEtRevoqueImmediatement:
    """La deuxième barrière : figurer au registre, et y figurer encore."""

    def test_un_jeton_valide_ne_vaut_rien_si_l_operateur_a_ete_retire(self, monkeypatch):
        """La révocation prime sur le jeton.

        Le jeton reste cryptographiquement valide pendant quatre heures. Si la
        révocation attendait son expiration, renvoyer quelqu'un lui laisserait quatre
        heures d'accès au parc entier.
        """
        jeton = _declarer_operateur(monkeypatch)
        monkeypatch.delenv("ADSUM_OPERATEURS_EDITEUR")
        reponse = client.get("/api/v1/support/console/fils", headers=_entete(jeton))
        assert reponse.status_code == 403
        assert "révoqué" in reponse.json()["detail"]

    def test_un_registre_vide_n_ouvre_la_console_a_personne(self):
        """Le comportement d'un déploiement qu'on n'a pas fini de configurer."""
        assert frontiere.registre_operateurs() == {}
        with pytest.raises(frontiere.FrontiereFermee):
            frontiere.creer_jeton_editeur(COMPTE, "x@editeur.test", "admin")

    def test_un_role_absent_de_la_politique_n_est_pas_retenu(self, monkeypatch):
        """Une faute de frappe dans la configuration ne crée pas un opérateur.

        Retenir l'entrée produirait un opérateur légitime mais sans capacité, dont
        les refus seraient incompréhensibles. Ne pas la retenir produit un refus
        clair : ce compte n'est pas opérateur.
        """
        monkeypatch.setenv("ADSUM_OPERATEURS_EDITEUR", f"{COMPTE}:editor-inexistant")
        assert frontiere.registre_operateurs() == {}


class TestLaCapaciteBorneCeQueLOperateurPeut:
    """La troisième barrière : la politique, lue à l'exécution."""

    def test_un_agent_de_support_ne_cree_pas_d_organisation(self, monkeypatch):
        """Le support répond aux demandes ; il ne fabrique pas de clients.

        C'est la différence entre « être de l'éditeur » et « pouvoir tout faire chez
        l'éditeur ». Sans elle, un compte d'agent compromis vaudrait un compte
        d'administrateur général.
        """
        jeton = _declarer_operateur(monkeypatch, "editor-support-agent")
        reponse = client.post("/api/v1/support/console/organisations",
                              headers=_entete(jeton),
                              json={"code": "test-org", "nom": "Organisation d'essai"})
        assert reponse.status_code == 403
        assert "editor.tenants.creer" in reponse.json()["detail"]

    def test_un_developpeur_ne_suspend_pas_une_organisation(self, monkeypatch):
        jeton = _declarer_operateur(monkeypatch, "editor-developer")
        reponse = client.patch(
            f"/api/v1/support/console/organisations/{COMPTE}/etat",
            headers=_entete(jeton), json={"etat": "suspendue"})
        assert reponse.status_code == 403
        assert "editor.tenants.suspendre" in reponse.json()["detail"]

    def test_la_capacite_detenue_franchit_la_garde(self, monkeypatch):
        """Le pendant positif : sans lui, ces tests prouveraient seulement qu'on
        refuse tout, ce qu'un mur ferait aussi bien.

        La garde franchie, la requête part vers la base. On ne juge donc pas le
        contenu de la réponse, seulement qu'aucune des trois barrières ne s'est
        prononcée.
        """
        jeton = _declarer_operateur(monkeypatch, "editor-operations-readonly")
        reponse = client.get("/api/v1/support/console/organisations",
                             headers=_entete(jeton))
        assert reponse.status_code not in (401, 403), reponse.text


class TestLeMondeClientNeConnaitPlusLaPermissionEditeur:
    """Ce que le catalogue client ne doit plus contenir."""

    def test_support_traiter_a_quitte_le_catalogue(self):
        from app import permissions_data

        assert "support.traiter" not in permissions_data.CATALOGUE

    def test_aucun_role_client_ne_detient_support_traiter(self):
        """La cause directe de la brèche : deux rôles de tenant la détenaient d'office."""
        from app import permissions_data

        porteurs = [role for role, permissions
                    in permissions_data.ROLE_PERMISSIONS.items()
                    if "support.traiter" in permissions]
        assert porteurs == []

    def test_aucune_route_de_console_n_est_gardee_par_une_permission_cliente(self):
        from app import permissions_data

        restantes = [route for route in permissions_data.ENDPOINT_PERMISSION
                     if "/support/console" in route]
        assert restantes == []

    def test_aucun_role_client_n_accorde_une_capacite_editeur(self):
        """La règle générale dont la brèche n'était qu'un cas.

        Elle porte sur le préfixe et non sur un nom : elle vaut donc aussi pour la
        prochaine permission qu'on placerait du mauvais côté.
        """
        from app import permissions_data

        fautives = {
            f"{role}.{permission}"
            for role, permissions in permissions_data.ROLE_PERMISSIONS.items()
            for permission in permissions
            if permission.startswith("editor.")
        }
        assert fautives == set()


class TestUnJetonEditeurNOuvrePasLeMondeClient:
    """Le sens inverse, qui manquait.

    Toute l'attention portait sur le client atteignant la console. Le trajet inverse
    compte autant : un opérateur de l'éditeur ne doit pas ouvrir les applications
    métier d'une organisation parce qu'il travaille pour l'éditeur. Ce chemin existe,
    il s'appelle assistance exceptionnelle, il se demande et il expire.
    """

    ROUTES_CLIENTES = [
        ("GET", "/api/v1/auth/me"),
        ("GET", "/api/v1/membres/me"),
    ]

    @pytest.mark.parametrize(("methode", "chemin"), ROUTES_CLIENTES)
    def test_le_jeton_d_operateur_est_refuse_cote_client(self, monkeypatch, methode, chemin):
        jeton = _declarer_operateur(monkeypatch, "editor-super-admin")
        reponse = client.request(methode, chemin, headers=_entete(jeton))
        assert reponse.status_code == 401, (
            f"{methode} {chemin} a accepté un jeton d'opérateur : la frontière ne "
            "tient que dans un sens.")

    def test_les_deux_audiences_different(self):
        """La propriété dont tout le reste découle."""
        assert frontiere.AUDIENCE_CLIENT != frontiere.AUDIENCE_EDITEUR
