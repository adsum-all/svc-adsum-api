"""The provider catalogue must stay consistent with the gateway that executes it.

These run without a database: they check the rules that decide whether a switch is
offered at all. A catalogue that drifts from the gateway would put a button on
screen that cannot work, and the administrator would only find out after switching.
"""
from __future__ import annotations

from app.email_fournisseurs import _CLES_AUTORISEES, _COMMUNS, CATALOGUE, _alertes, _manquants
from app.email_gateway import _PROVIDERS


def test_chaque_fournisseur_du_catalogue_existe_dans_la_passerelle() -> None:
    """Offering a provider the gateway cannot instantiate would be a dead button."""
    for f in CATALOGUE:
        assert f.code in _PROVIDERS, f"{f.code} est proposé mais la passerelle ne sait pas l'exécuter"


def test_les_preselections_ne_visent_que_des_champs_declares() -> None:
    """A preset naming an unknown key fills nothing and fails in silence."""
    for f in CATALOGUE:
        connus = {c.cle for c in f.champs}
        for preset in f.preselections:
            assert "libelle" in preset, f"préselection sans libellé sur {f.code}"
            for cle in preset:
                if cle == "libelle":
                    continue
                assert cle in connus, f"la préselection {preset['libelle']} vise {cle}, inconnu de {f.code}"


def test_la_liste_blanche_couvre_exactement_les_champs_du_catalogue() -> None:
    """Writing is restricted to declared keys; a field absent from it is uneditable."""
    attendus = {c.cle for c in _COMMUNS} | {c.cle for f in CATALOGUE for c in f.champs}
    assert _CLES_AUTORISEES == attendus


def test_un_fournisseur_sans_identifiant_n_est_pas_pret() -> None:
    smtp = next(f for f in CATALOGUE if f.code == "smtp")
    manque = _manquants(smtp, {}, ["brevo"])
    assert "Mot de passe" in manque
    assert "Serveur d'envoi" in manque


def test_un_champ_facultatif_ne_bloque_jamais_l_activation() -> None:
    smtp = next(f for f in CATALOGUE if f.code == "smtp")
    complet = {
        "email_smtp_host": "smtp.bbox.fr",
        "email_smtp_port": "465",
        "email_smtp_user": "compte@bbox.fr",
        "email_smtp_password": "secret",
    }
    assert _manquants(smtp, complet, ["smtp"]) == []


def test_la_cle_historique_ne_vaut_que_pour_le_fournisseur_deja_actif() -> None:
    """Regression: the single legacy key belongs to one service, not to all of them.

    Crediting ``email_api_key`` to every provider declared Resend ready while the
    stored key was Brevo's, so a switch would have been accepted and every send
    rejected afterwards.
    """
    brevo = next(f for f in CATALOGUE if f.code == "brevo")
    resend = next(f for f in CATALOGUE if f.code == "resend")
    valeurs = {"email_api_key": "xkeysib-cle-historique-de-brevo"}

    assert _manquants(brevo, valeurs, ["brevo"]) == []
    assert _manquants(resend, valeurs, ["brevo"]) == ["Clé API Resend"]
    # And once Resend holds its own key, it becomes activable.
    assert _manquants(resend, {**valeurs, "email_api_key_resend": "re_propre"}, ["brevo"]) == []


def test_un_secret_est_toujours_declare_comme_tel() -> None:
    """A credential rendered as plain text would be read over the shoulder."""
    for f in CATALOGUE:
        for c in f.champs:
            if "password" in c.cle or "api_key" in c.cle:
                assert c.secret, f"{c.cle} transporte un identifiant et doit être marqué secret"


def test_les_limites_des_fournisseurs_a_risque_sont_ecrites() -> None:
    """SMTP through an access provider, and console, must state what they cost.

    Both look like ordinary choices in a list. One caps the daily volume without
    contract, the other stops every message. Left unsaid, they get picked.
    """
    for code in ("smtp", "console"):
        f = next(x for x in CATALOGUE if x.code == code)
        assert f.limite.strip(), f"{code} est proposé sans énoncer sa limite"


def test_l_alerte_sur_l_expediteur_est_gradee_selon_les_droits_du_compte() -> None:
    """The severity must follow what the account can do, not merely that it exists.

    An address that only receives mail is one thing. An address that also opens a
    privileged session is another: it is printed on every message, so the target is
    public, and the one-time codes protecting it arrive in that same mailbox.
    """
    base = {"email_from": "envoi@exemple.fr", "email_reply_to": "contact@exemple.fr"}

    # No account behind the sender: nothing to say.
    assert _alertes(base, None) == []

    # A plain member account: worth flagging, not critical.
    membre = {"role": "membre", "actif": True, "technique": False}
    assert [a["niveau"] for a in _alertes(base, membre)] == ["avertissement"]

    # Technical access outranks a human super administrator: critical.
    technique = {"role": "membre", "actif": True, "technique": True}
    assert [a["niveau"] for a in _alertes(base, technique)] == ["critique"]

    # A super administrator, even without technical access, is critical too.
    super_admin = {"role": "super_admin", "actif": True, "technique": False}
    assert [a["niveau"] for a in _alertes(base, super_admin)] == ["critique"]

    # A deactivated account cannot open a session, so it is not the same exposure.
    eteint = {"role": "super_admin", "actif": False, "technique": True}
    assert _alertes(base, eteint) == []


def test_l_absence_d_adresse_de_reponse_est_signalee() -> None:
    """A member answering a notification must not write into a mailbox nobody reads."""
    alertes = _alertes({"email_from": "envoi@exemple.fr", "email_reply_to": ""}, None)
    assert [a["titre"] for a in alertes] == ["Aucune adresse de réponse"]


def test_sans_expediteur_configure_aucune_alerte_n_est_inventee() -> None:
    assert _alertes({"email_from": "", "email_reply_to": ""}, None) == []
