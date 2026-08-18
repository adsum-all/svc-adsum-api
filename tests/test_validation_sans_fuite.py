"""Un 422 ne doit jamais renvoyer ce qui a été soumis sur un champ sensible.

Le défaut a été constaté en conditions réelles : une tentative de connexion mal
formée a fait apparaître un mot de passe réel dans la réponse de l'API, donc dans
tout ce qui enregistre les réponses en chemin.
"""
from __future__ import annotations

import pytest

from app.validation_sans_fuite import MASQUE, champ_sensible, nettoyer


class TestReperageDesChamps:
    @pytest.mark.parametrize("chemin", [
        ["body", "password"],
        ["body", "mot_de_passe"],
        ["body", "nouveau_mdp"],
        ["body", "code_otp"],
        ["body", "access_token"],
        ["body", "jwt_secret"],
        ["query", "api_key"],
        ["header", "authorization"],
        ["body", "utilisateur", "password_confirm"],
    ])
    def test_reconnait_un_champ_sensible(self, chemin):
        assert champ_sensible(chemin), chemin

    @pytest.mark.parametrize("chemin", [
        ["body", "email"],
        ["body", "nom"],
        ["body", "membre_id"],
        ["body", "date_naissance"],
    ])
    def test_laisse_passer_un_champ_ordinaire(self, chemin):
        assert not champ_sensible(chemin), chemin

    def test_un_chemin_absent_ne_fait_pas_tomber_le_gestionnaire(self):
        # Une erreur sans « loc » ne doit pas transformer un 422 en 500.
        assert not champ_sensible(None)
        assert not champ_sensible("body")


class TestNettoyage:
    def test_le_mot_de_passe_ne_ressort_pas(self):
        erreurs = [{"type": "string_too_short", "loc": ["body", "password"],
                    "msg": "trop court", "input": "MonVraiMotDePasse2026"}]
        propre = nettoyer(erreurs)
        assert propre[0]["input"] == MASQUE
        assert "MonVraiMotDePasse2026" not in str(propre)

    def test_le_contexte_part_aussi(self):
        # Certains validateurs recopient la valeur dans « ctx » sous une autre clé.
        # La deviner est plus fragile que de retirer le contexte entier.
        erreurs = [{"type": "value_error", "loc": ["body", "code_otp"],
                    "msg": "invalide", "input": "483920",
                    "ctx": {"valeur_recue": "483920"}}]
        propre = nettoyer(erreurs)
        assert "483920" not in str(propre)

    def test_l_emplacement_et_la_raison_sont_conserves(self):
        # C'est ce dont un développeur a besoin ; la valeur, il l'a déjà.
        erreurs = [{"type": "missing", "loc": ["body", "password"], "msg": "champ requis",
                    "input": "x"}]
        propre = nettoyer(erreurs)
        assert propre[0]["loc"] == ["body", "password"]
        assert propre[0]["msg"] == "champ requis"
        assert propre[0]["type"] == "missing"

    def test_un_champ_ordinaire_garde_sa_valeur(self):
        erreurs = [{"type": "value_error", "loc": ["body", "email"],
                    "msg": "adresse invalide", "input": "pas-une-adresse"}]
        assert nettoyer(erreurs)[0]["input"] == "pas-une-adresse"

    def test_une_valeur_ordinaire_demesuree_est_bornee(self):
        # Un corps volumineux mal formé produirait sinon une réponse aussi grosse que
        # la requête, ce qui amplifie une requête abusive.
        erreurs = [{"type": "value_error", "loc": ["body", "commentaire"],
                    "msg": "trop long", "input": "x" * 5000}]
        assert len(nettoyer(erreurs)[0]["input"]) < 250

    def test_plusieurs_erreurs_sont_toutes_traitees(self):
        erreurs = [
            {"loc": ["body", "email"], "msg": "invalide", "input": "abc"},
            {"loc": ["body", "password"], "msg": "trop court", "input": "secret123"},
        ]
        propre = nettoyer(erreurs)
        assert propre[0]["input"] == "abc"
        assert propre[1]["input"] == MASQUE


class TestFuiteParChampManquant:
    """La façon dont la fuite se produit réellement.

    Une erreur « champ requis » ne porte pas le champ fautif en ``input`` : elle
    porte le corps entier de la requête. Une connexion sans adresse produit donc une
    erreur située sur « email », qui n'est pas un champ sensible, et dont la valeur
    contient le mot de passe. Masquer d'après l'emplacement seul ne suffit pas.
    """

    def test_le_corps_entier_est_nettoye_meme_sur_un_champ_ordinaire(self):
        erreurs = [{
            "type": "missing", "loc": ["body", "email"], "msg": "Field required",
            "input": {"password": "MotDePasseReel2026", "identifiant": "ADS-2026-000042"},
        }]
        propre = nettoyer(erreurs)
        assert "MotDePasseReel2026" not in str(propre)
        # L'identifiant reste : il aide à retrouver l'appel, et il n'est pas secret.
        assert "ADS-2026-000042" in str(propre)

    def test_un_champ_sensible_imbrique_est_masque(self):
        erreurs = [{
            "type": "missing", "loc": ["body", "comptes", 0, "email"],
            "input": {"comptes": [{"password": "SecretImbrique"}]},
        }]
        assert "SecretImbrique" not in str(nettoyer(erreurs))

    def test_une_structure_repliee_ne_fait_pas_tourner_le_gestionnaire(self):
        # Un gestionnaire d'erreur qui boucle transforme un 422 en incident.
        profond: dict = {"niveau": 0}
        courant = profond
        for i in range(1, 40):
            suivant: dict = {"niveau": i}
            courant["dedans"] = suivant
            courant = suivant
        propre = nettoyer([{"loc": ["body", "x"], "input": profond}])
        assert propre, "le nettoyage doit aboutir"

    def test_une_liste_de_corps_est_traitee(self):
        erreurs = [{"loc": ["body"], "input": [{"password": "a"}, {"mdp": "b"}]}]
        propre = str(nettoyer(erreurs))
        assert "'a'" not in propre and "'b'" not in propre
