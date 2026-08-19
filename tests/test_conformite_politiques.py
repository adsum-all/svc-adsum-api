"""Le code fait-il ce que les politiques d'accès déclarent.

Deux fichiers JSON disent qui peut quoi de part et d'autre de la frontière. Sans ce
test ils seraient deux documents que personne ne relit et que le code contredit en
silence, ce qui est pire que pas de document du tout : on croirait la frontière tenue.

Le test lit la table de routage réelle de l'application, pas une liste écrite à la
main. Une route ajoutée sans déclaration, une garde changée, une capacité déplacée
d'un côté à l'autre : chacune de ces trois choses le fait échouer.

L'exemplaire faisant foi vit dans ``sr-media-ai/adsum/deployment/ci-templates``. En
intégration continue, la variable ``ADSUM_POLICIES`` pointe sur le clone frais ; en
local, la copie versionnée du dépôt sert, et un dernier test vérifie qu'elle n'a pas
dérivé de l'original quand celui-ci est accessible en voisin.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

from app import permissions_data
from app.main import app

RACINE_DEPOT = Path(__file__).resolve().parents[1]


def _dossier_politiques() -> Path:
    depuis_env = os.environ.get("ADSUM_POLICIES", "").strip()
    return Path(depuis_env) if depuis_env else RACINE_DEPOT / "policies"


def _lire(nom: str) -> dict:
    chemin = _dossier_politiques() / nom
    if not chemin.exists():
        pytest.skip(f"Politique introuvable : {chemin}")
    with open(chemin, encoding="utf-8") as fichier:
        return json.load(fichier)


@pytest.fixture(scope="module")
def politique_client() -> dict:
    return _lire("client-access-policy.json")


@pytest.fixture(scope="module")
def politique_editeur() -> dict:
    return _lire("editor-access-policy.json")


def _toutes_les_routes():
    """Toutes les routes réelles, enveloppes comprises.

    Cette version de FastAPI n'aplatit pas : ``app.routes`` rend cent sept objets
    ``_IncludedRouter``, qui exposent le routeur inclus sous ``original_router`` et
    non sous ``routes``. S'arrêter au premier niveau ne voyait aucune route de
    console, et le test passait en ne vérifiant rien, ce qui est le pire état
    possible pour un test de frontière : il rassure sans rien garantir.
    """
    a_visiter = list(app.routes)
    vus: set[int] = set()
    while a_visiter:
        element = a_visiter.pop()
        if id(element) in vus:
            continue
        vus.add(id(element))
        if isinstance(element, APIRoute):
            yield element
            continue
        for attribut in ("routes", "original_router"):
            suite = getattr(element, attribut, None)
            if suite is None:
                continue
            a_visiter.extend(suite if isinstance(suite, list) else [suite])


def _gardes_editeur_du_code() -> dict[tuple[str, str], str]:
    """Ce que le code exige réellement, relu depuis la table de routage.

    La capacité est accrochée à la dépendance par ``require_capacite``. La lire ici
    plutôt que deviner la garde en inspectant une fermeture est ce qui rend ce test
    robuste au remaniement : si le marqueur disparaît, la route cesse d'être vue et
    le test de couverture le signale.
    """
    trouvees: dict[tuple[str, str], str] = {}
    for route in _toutes_les_routes():
        for dependance in route.dependant.dependencies:
            capacite = getattr(dependance.call, "capacite_exigee", None)
            if capacite:
                for methode in sorted(route.methods or ()):
                    trouvees[(methode, route.path)] = capacite
    return trouvees


def _routes_declarees(politique: dict, cle: str) -> dict[tuple[str, str], str]:
    return {
        (route["method"], route["path"]): route["capability"]
        for application in politique.get(cle, [])
        for route in application.get("routes", [])
    }


class TestLesRoutesEditeurSontCellesQueLaPolitiqueDeclare:

    def test_chaque_route_gardee_est_declaree_avec_la_meme_capacite(self, politique_editeur):
        """Le cœur du test : garde du code contre déclaration de la politique."""
        code = _gardes_editeur_du_code()
        assert code, (
            "Aucune garde éditeur trouvée dans la table de routage. Sans ce garde-fou, "
            "une collecte défaillante ferait passer ce test en ne comparant rien.")
        declare = _routes_declarees(politique_editeur, "internal_applications")
        ecarts = []
        for (methode, chemin), capacite in sorted(code.items()):
            attendue = declare.get((methode, chemin))
            if attendue is None:
                ecarts.append(
                    f"{methode} {chemin} exige « {capacite} » mais n'est déclarée "
                    "dans aucune application interne de la politique éditeur.")
            elif attendue != capacite:
                ecarts.append(
                    f"{methode} {chemin} exige « {capacite} » alors que la politique "
                    f"déclare « {attendue} ».")
        assert ecarts == [], "\n".join(ecarts)

    def test_aucune_route_declaree_n_a_disparu_du_code(self, politique_editeur):
        """Une route déclarée mais absente donne une couverture qui n'existe pas.

        Ne concerne que les routes de cette application : la politique déclare aussi
        celles du service commerce, qui vit dans un autre dépôt et a son propre test.
        """
        code = _gardes_editeur_du_code()
        manquantes = [
            f"{methode} {chemin} déclarée par la politique, absente de la table de routage"
            for (methode, chemin), _ in sorted(
                _routes_declarees(politique_editeur, "internal_applications").items())
            if chemin.startswith("/api/v1/support/console") and (methode, chemin) not in code
        ]
        assert manquantes == [], "\n".join(manquantes)

    def test_toute_route_de_console_porte_une_garde_editeur(self):
        """Aucune route sous le préfixe de la console ne doit rester sans frontière.

        C'est la vérification qui aurait attrapé la brèche : à l'époque, ces vingt
        routes avaient toutes une garde, mais aucune n'était une garde éditeur.
        """
        gardees = _gardes_editeur_du_code()
        nues = [
            f"{methode} {route.path}"
            for route in _toutes_les_routes()
            if "/support/console" in route.path
            for methode in sorted(route.methods or ())
            if (methode, route.path) not in gardees
        ]
        assert nues == [], (
            "Ces routes de console ne passent pas par la frontière éditeur :\n"
            + "\n".join(nues))


class TestLesDeuxMondesRestentDisjoints:

    def test_aucune_capacite_editeur_n_est_accordee_a_un_role_client(self, politique_client):
        fautives = [
            f"{role['id']} accorde {capacite}"
            for role in politique_client["roles"]
            for capacite in role.get("grants", [])
            if not capacite.startswith("client.")
        ]
        assert fautives == []

    def test_aucune_capacite_cliente_n_est_accordee_a_un_role_editeur(self, politique_editeur):
        fautives = [
            f"{role['id']} accorde {capacite}"
            for role in politique_editeur["roles"]
            for capacite in role.get("grants", [])
            if not capacite.startswith("editor.")
        ]
        assert fautives == []

    def test_aucune_route_n_est_revendiquee_par_les_deux_politiques(
            self, politique_client, politique_editeur):
        cotes = set(_routes_declarees(politique_client, "applications"))
        editeur = set(_routes_declarees(politique_editeur, "internal_applications"))
        assert cotes & editeur == set()

    def test_les_audiences_de_jeton_different(self, politique_client, politique_editeur):
        """Sans audiences distinctes, un seul jeton ouvrirait les deux mondes.

        C'est littéralement l'état d'avant : le même jeton servait la console de
        l'éditeur et l'espace personnel d'un membre.
        """
        from app import frontiere

        assert politique_client["scope"]["token_audience"] == frontiere.AUDIENCE_CLIENT
        assert politique_editeur["scope"]["token_audience"] == frontiere.AUDIENCE_EDITEUR
        assert frontiere.AUDIENCE_CLIENT != frontiere.AUDIENCE_EDITEUR


class TestLeCatalogueClientEtLaPolitiqueSAccordent:

    def test_chaque_permission_du_catalogue_est_declaree(self, politique_client):
        """La politique doit connaître toutes les permissions que le code applique.

        Une permission absente de la politique est un droit que personne n'a arbitré :
        elle existe dans le code, elle s'exerce, et aucun document ne dit qui la tient.
        """
        declarees = {action["id"] for action in politique_client["actions"]}
        absentes = sorted(
            f"client.{permission}" for permission in permissions_data.CATALOGUE
            if f"client.{permission}" not in declarees)
        assert absentes == [], (
            "Ces permissions sont appliquées par le code sans figurer dans la "
            "politique cliente :\n" + "\n".join(absentes))

    def test_chaque_capacite_cliente_correspond_a_une_permission_reelle(self, politique_client):
        """Le sens inverse : la politique ne déclare pas de droits qui n'existent pas.

        Les capacités du portail font exception : elles vivent dans le service
        commerce, pas dans le catalogue de cette application.
        """
        connues = {f"client.{permission}" for permission in permissions_data.CATALOGUE}
        inventees = sorted(
            action["id"] for action in politique_client["actions"]
            if action["id"] not in connues
            and not action["id"].startswith("client.portail."))
        assert inventees == [], (
            "Ces capacités sont déclarées sans correspondre à une permission du "
            "catalogue :\n" + "\n".join(inventees))

    def test_les_grants_des_roles_reproduisent_la_matrice_du_code(self, politique_client):
        """Ce que la politique accorde doit être ce que le code accorde.

        La correspondance ne porte que sur les rôles que le code connaît : la
        politique en déclare d'autres, marqués comme à créer, qui n'ont pas encore
        d'équivalent dans la matrice.
        """
        correspondance = {
            "client-super-admin": "super_admin", "client-admin": "admin",
            "client-manager": "gestionnaire", "client-direction": "direction",
            "client-controller": "controleur", "client-member": "membre",
        }
        par_identifiant = {role["id"]: role for role in politique_client["roles"]}
        ecarts = []
        for identifiant, code_role in correspondance.items():
            declares = set(par_identifiant[identifiant]["grants"])
            reels = {f"client.{p}" for p in permissions_data.ROLE_PERMISSIONS[code_role]}
            if declares != reels:
                ecarts.append(
                    f"{identifiant} : en trop {sorted(declares - reels)}, "
                    f"manquant {sorted(reels - declares)}")
        assert ecarts == [], "\n".join(ecarts)


class TestLaCopieVerseeNAPasDerive:

    def test_les_politiques_du_depot_sont_celles_qui_font_foi(self):
        """La copie versée ici doit être identique à l'exemplaire faisant foi.

        Une copie qui dérive est pire que pas de copie : elle répond, et c'est la
        mauvaise réponse. En intégration continue la tâche policy:drift fait ce même
        contrôle contre le dépôt distant ; ici on se contente du voisin sur disque,
        et on passe si le voisin n'existe pas, parce que le dépôt doit rester
        clonable seul.
        """
        origine = (RACINE_DEPOT.parents[1] / "deployment" / "ci-templates" / "policies")
        if not origine.exists():
            pytest.skip("Dépôt ci-templates absent en voisin : contrôle fait en CI.")
        for nom in ("client-access-policy.json", "editor-access-policy.json"):
            attendu = (origine / nom).read_bytes()
            obtenu = (RACINE_DEPOT / "policies" / nom).read_bytes()
            assert obtenu == attendu, (
                f"{nom} a dérivé de l'exemplaire faisant foi de ci-templates.")
