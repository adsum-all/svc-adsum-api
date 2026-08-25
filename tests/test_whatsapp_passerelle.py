"""WhatsApp doit emprunter la passerelle interne, pas l'API Meta en direct.

Un canal appelé en direct n'a ni repli sur un second fournisseur, ni registre
d'envoi, ni vérification de signature sur les accusés. Ces tests vérifient à quelle
adresse part réellement la requête, ce qui est le seul fait qui distingue les deux
chemins, et ce que l'API métier met dans la charge pour que la passerelle sache
quel gabarit approuvé utiliser.
"""
from __future__ import annotations

from app import channels


class AppelCapture:
    """Le point exact où la requête HTTP part. Coupé ici, rien d'autre n'est simulé."""

    def __init__(self, reussite: bool = True) -> None:
        self.reussite = reussite
        self.appels: list[tuple[str, dict, dict]] = []

    def __call__(self, url, payload, headers):  # noqa: ANN001
        self.appels.append((url, payload, headers))
        return self.reussite, {}


def _passerelle_configuree(monkeypatch, url="https://passerelle.example", secret="s" * 40):
    valeurs = {"passerelle_url": url, "passerelle_secret": secret}
    monkeypatch.setattr(
        channels, "integration_value", lambda cle: valeurs.get(cle, ""))


def test_whatsapp_part_par_la_passerelle_quand_elle_est_configuree(monkeypatch):
    _passerelle_configuree(monkeypatch)
    capture = AppelCapture()
    monkeypatch.setattr(channels, "_post_json", capture)

    assert channels.send_whatsapp(
        "+2250700000001", ["Membre", "31 octobre"], "rappel_cotisation") is True

    url, charge, entetes = capture.appels[0]
    assert url == "https://passerelle.example/api/v1/envois"
    assert "graph.facebook.com" not in url
    assert charge["canal"] == "whatsapp"
    assert charge["adresse"] == "+2250700000001"
    assert entetes["Authorization"].startswith("Bearer ")


def test_le_gabarit_et_ses_valeurs_sont_transmis_dans_l_ordre(monkeypatch):
    _passerelle_configuree(monkeypatch)
    capture = AppelCapture()
    monkeypatch.setattr(channels, "_post_json", capture)

    channels.send_whatsapp("+2250700000001", ["Membre", "31 octobre"], "rappel")

    metadonnees = capture.appels[0][1]["metadonnees"]
    assert metadonnees["gabarit"] == "rappel"
    assert metadonnees["gabarit_parametre_1"] == "Membre"
    assert metadonnees["gabarit_parametre_2"] == "31 octobre"


def test_la_cle_d_idempotence_est_stable_sur_une_reprise_immediate(monkeypatch):
    """Une erreur réseau reprise dans la foulée ne doit pas envoyer deux fois."""
    _passerelle_configuree(monkeypatch)
    capture = AppelCapture()
    monkeypatch.setattr(channels, "_post_json", capture)

    channels.send_whatsapp("+2250700000001", ["Membre"], "rappel")
    channels.send_whatsapp("+2250700000001", ["Membre"], "rappel")

    assert capture.appels[0][1]["cle_idempotence"] == capture.appels[1][1]["cle_idempotence"]


def test_deux_destinataires_ne_partagent_pas_la_meme_cle(monkeypatch):
    _passerelle_configuree(monkeypatch)
    capture = AppelCapture()
    monkeypatch.setattr(channels, "_post_json", capture)

    channels.send_whatsapp("+2250700000001", ["Membre"], "rappel")
    channels.send_whatsapp("+2250700000002", ["Membre"], "rappel")

    assert capture.appels[0][1]["cle_idempotence"] != capture.appels[1][1]["cle_idempotence"]


def test_sans_passerelle_l_appel_direct_reste_le_chemin(monkeypatch):
    """La bascule ne coupe pas les installations où la passerelle n'est pas déployée."""
    monkeypatch.setattr(channels, "integration_value", lambda cle: "")
    monkeypatch.setattr(channels, "whatsapp_configured", lambda: True)
    monkeypatch.setattr(channels.settings, "whatsapp_phone_number_id", "123")
    monkeypatch.setattr(channels.settings, "whatsapp_token", "jeton")
    capture = AppelCapture()
    monkeypatch.setattr(channels, "_post_json", capture)

    channels.send_whatsapp("+2250700000001", ["Membre"], "rappel")

    assert "graph.facebook.com" in capture.appels[0][0]


def test_sans_gabarit_resolu_rien_ne_part(monkeypatch):
    """Meta n'accepte aucun message proactif sans gabarit approuvé."""
    _passerelle_configuree(monkeypatch)
    capture = AppelCapture()
    monkeypatch.setattr(channels, "_post_json", capture)
    monkeypatch.setattr(channels.settings, "whatsapp_template_anniversaire", "")

    assert channels.send_whatsapp("+2250700000001", ["Membre"]) is False
    assert capture.appels == []


def test_un_refus_de_la_passerelle_est_remonte_comme_un_echec(monkeypatch):
    _passerelle_configuree(monkeypatch)
    monkeypatch.setattr(channels, "_post_json", AppelCapture(reussite=False))

    assert channels.send_whatsapp("+2250700000001", ["Membre"], "rappel") is False
