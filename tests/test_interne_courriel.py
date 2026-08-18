"""La porte d'envoi réservée aux services internes de l'éditeur.

Deux propriétés, toutes deux vérifiables sans réseau : l'authentification passe avant
la validation du corps, et deux appels portant la même clé ne produisent qu'un envoi.
"""
from __future__ import annotations

import pytest

from app import interne_courriel


@pytest.fixture(autouse=True)
def cache_vide():
    interne_courriel._ENVOIS_RECENTS.clear()
    yield
    interne_courriel._ENVOIS_RECENTS.clear()


class TestDeduplication:
    def test_une_cle_inconnue_n_est_pas_un_doublon(self):
        assert interne_courriel._deja_envoye("jamais-vue") is None

    def test_une_cle_notee_est_reconnue(self):
        interne_courriel._noter_envoi("relance-1", "brevo")
        assert interne_courriel._deja_envoye("relance-1") == "brevo"

    def test_une_cle_expiree_laisse_repartir_le_message(self, monkeypatch):
        # Au-delà de la rétention, une reprise n'est plus une reprise : c'est un
        # nouvel envoi voulu, par exemple la relance du mois suivant.
        import time

        interne_courriel._noter_envoi("relance-2", "brevo")
        depart = time.monotonic()
        monkeypatch.setattr(
            interne_courriel.time if hasattr(interne_courriel, "time") else time,
            "monotonic", lambda: depart + interne_courriel.RETENTION_S + 1)
        assert interne_courriel._deja_envoye("relance-2") is None

    def test_le_cache_ne_grossit_pas_sans_limite(self):
        # Un appelant qui varie sa clé ferait sinon grossir la mémoire du processus.
        for i in range(interne_courriel.TAILLE_MAX + 50):
            interne_courriel._noter_envoi(f"cle-{i}", "brevo")
        assert len(interne_courriel._ENVOIS_RECENTS) <= interne_courriel.TAILLE_MAX


class TestMiseEnForme:
    def test_le_texte_est_echappe(self):
        # Le corps vient d'un autre service : un nom d'organisation contenant un
        # chevron produirait un message cassé au mieux, une injection au pire.
        html = interne_courriel._en_html("Paroisse <script>alert(1)</script>")
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_les_paragraphes_sont_conserves(self):
        html = interne_courriel._en_html("Bonjour,\n\nÀ régler.\n\nMerci.")
        assert html.count("<p>") == 3

    def test_un_saut_simple_devient_un_retour_a_la_ligne(self):
        html = interne_courriel._en_html("Ligne un\nLigne deux")
        assert "<br>" in html
