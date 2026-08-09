"""The direction's figures must reconcile, refuse what they cannot compute, and
never count a member twice.

Run against the real database rather than a substitute. The properties asserted here
are exactly the ones a stand-in cannot vouch for: that the consolidation deduplicates
what the two attendance tables hold in common, that a total equals the sum of its
parts on the data the organisation actually has, and that a filter narrows. A fake
that returns rows chosen by the test proves the test, not the platform.

The suite is read-only. Nothing here writes, so it is safe to run against the live
base, and it is skipped rather than failed when the database is unreachable: a
missing connection is an environment fact, not a defect in this code.
"""
from __future__ import annotations

import pytest

from app import direction_analyse as analyse
from app import direction_membres
from app import direction_pilotage as pilotage
from app.db import fetch_one


def _base_joignable() -> bool:
    try:
        fetch_one("SELECT 1 AS ok", ())
        return True
    except Exception:  # noqa: BLE001 - any connection failure means "not here"
        return False


pytestmark = pytest.mark.skipif(
    not _base_joignable(), reason="base de données non joignable dans cet environnement"
)


# --- Reconciliation ---------------------------------------------------------

def test_les_axes_donnent_le_meme_total():
    """One set of attendance facts, grouped differently, sums to the same total.

    This is the property the whole dashboard rests on. If cutting by commission and
    cutting by tribe disagree, one of the two screens is lying and nothing on the
    page can be trusted.
    """
    totaux = {
        axe: sum(x["total"] for x in analyse.repartition(axe, {}, None))
        for axe in ("commission", "tribu", "coordination", "pays", "volet")
    }
    assert len(set(totaux.values())) == 1, f"les axes divergent : {totaux}"


def test_la_synthese_egale_la_repartition():
    """The headline card and the chart under it come from the same rows.

    Compared on the three axes rather than on a single "presents" figure, because that
    figure used to mean two things at once: the status said the person had followed,
    and the word said they were in the room.
    """
    s = pilotage.synthese({}, None)
    lignes = analyse.repartition("commission", {}, None)
    assert s["observations"] == sum(x["total"] for x in lignes)
    assert s["suivis"] == sum(x["suivis"] for x in lignes)
    assert s["presentiel"] == sum(x["presentiel"] for x in lignes)
    assert s["en_ligne"] == sum(x["en_ligne"] for x in lignes)
    assert s["absents"] == sum(x["absents"] for x in lignes)


def test_le_croisement_boucle_sur_ses_marges():
    """Cells sum to the general total, and each margin sums to its own line."""
    t = analyse.tableau_croise("commission", "modalite", {}, None)
    assert sum(c["valeur"] for c in t["cellules"]) == t["total_general"]
    assert sum(m["valeur"] for m in t["totaux_lignes"]) == t["total_general"]
    assert sum(m["valeur"] for m in t["totaux_colonnes"]) == t["total_general"]

    # Every row margin is exactly its own cells, not merely the grand total by luck.
    for marge in t["totaux_lignes"]:
        cellules = [c["valeur"] for c in t["cellules"] if c["ligne"] == marge["label"]]
        assert sum(cellules) == marge["valeur"], f"ligne incohérente : {marge['label']}"


def test_l_arborescence_egale_la_somme_de_ses_enfants():
    """A node in the drill-down is exactly what its children add up to.

    A parent that disagrees with its children is the defect that makes a drill-down
    useless: the reader opens a coordination to explain a figure and finds numbers
    that cannot produce it.
    """
    arbre = pilotage.arborescence({}, None)
    assert arbre, "aucune donnée : le test ne prouverait rien"

    def verifier(noeud):
        enfants = noeud.get("enfants") or []
        if enfants:
            assert noeud["total"] == sum(e["total"] for e in enfants), noeud["label"]
            assert noeud["presents"] == sum(e["presents"] for e in enfants), noeud["label"]
            for e in enfants:
                verifier(e)

    for racine in arbre:
        verifier(racine)

    s = pilotage.synthese({}, None)
    assert sum(n["total"] for n in arbre) == s["observations"]


# --- No double counting -----------------------------------------------------

def test_aucun_membre_compte_deux_fois_sur_une_activite():
    """The consolidation yields at most one row per (member, activity).

    Attendance lives in two tables and a QR scan writes to both. Summing them counts
    the scanned member twice, which is precisely the error the controller application
    makes possible at scale. Asserted here on the live data rather than argued from
    the schema.
    """
    doublons = fetch_one(
        f"WITH {analyse._CONSO} "
        "SELECT count(*) AS n FROM ("
        "  SELECT membre_id, evenement_id FROM conso GROUP BY 1, 2 HAVING count(*) > 1"
        ") d",
        {},
    )
    assert (doublons or {}).get("n", 0) == 0


def test_le_scan_prime_sur_la_declaration():
    """A scanned attendance is reported as on-site whatever was declared afterwards.

    The scan is the only channel that proves physical presence, so a later
    declaration of "en ligne" must not overwrite it. Otherwise a member could move
    themselves out of the on-site count after being seen at the door.
    """
    lignes = analyse.repartition("modalite", {}, None)
    par_label = {x["label"]: x for x in lignes}
    prouve = par_label.get("Présentiel prouvé")
    if prouve is None or prouve["total"] == 0:
        pytest.skip("aucune présence scannée sur la base : rien à prouver ici")
    # Every proven-on-site observation carries the scan flag, by construction.
    assert prouve["scannes"] == prouve["total"]


# --- Filters ----------------------------------------------------------------

def test_un_filtre_restreint_toujours():
    """A filter can only ever remove observations, never add any."""
    total = sum(x["total"] for x in analyse.repartition("commission", {}, None))
    for filtre in ({"volet": "A"}, {"depuis": "2026-07-01"}, {"genre": "F"}):
        restreint = sum(x["total"] for x in analyse.repartition("commission", filtre, None))
        assert restreint <= total, f"le filtre {filtre} a élargi le périmètre"


def test_une_periode_vide_ne_renvoie_rien():
    """A window in the far future holds no activity, and says so rather than falling
    back to the whole history."""
    s = pilotage.synthese({"depuis": "2400-01-01"}, None)
    assert s["observations"] == 0
    assert s["taux_presence"] == 0.0


def test_une_dimension_inconnue_est_refusee():
    """An axis the platform does not compute is refused, never approximated.

    A dashboard whose axis can be dictated from outside is a dashboard whose figures
    nobody can vouch for.
    """
    with pytest.raises(analyse.DimensionInconnue):
        analyse.repartition("salaire", {}, None)
    with pytest.raises(analyse.DimensionInconnue):
        analyse.tableau_croise("commission", "commission", {}, None)
    with pytest.raises(analyse.DimensionInconnue):
        analyse.tableau_croise("commission", "modalite", {}, None, mesure="mediane")


def test_un_filtre_inconnu_est_refuse():
    """An unrecognised filter raises instead of being silently dropped.

    Dropping it would show a wider population than was asked for, under a heading
    saying otherwise, which is the one failure a filtered dashboard must not have.
    """
    with pytest.raises(analyse.DimensionInconnue):
        analyse.repartition("commission", {"salaire": "eleve"}, None)


# --- Honesty of the rates ---------------------------------------------------

def test_les_taux_restent_dans_leurs_bornes():
    """Bounds, ordering, and the partition each rate divides by.

    The partition asserted here is the corrected one. The previous version added
    "presents" to "partiels" and expected the total, which held only while "presents"
    silently included people who had followed online: on site plus partial online is
    not a population anybody names, and it left the complete online followers out of
    the sum entirely.
    """
    for ligne in analyse.repartition("commission", {}, None):
        assert 0.0 <= ligne["taux_presence"] <= 100.0
        # On site can never exceed following: it is one of its channels.
        assert ligne["taux_presence"] <= ligne["taux_suivi"] <= 100.0
        assert ligne["taux_participation"] == ligne["taux_suivi"]
        # Axis 1 is exhaustive over the observations.
        assert ligne["suivis"] + ligne["absents"] == ligne["total"], f"axe suivi incohérent : {ligne['label']}"
        # Axis 2 is exhaustive over the follows.
        assert (
            ligne["presentiel"] + ligne["en_ligne"] + ligne["suivi_modalite_inconnue"] == ligne["suivis"]
        ), f"axe canal incohérent : {ligne['label']}"
        # Proof splits the on-site population and nothing else.
        assert ligne["presentiel_prouve"] + ligne["presentiel_declare"] == ligne["presentiel"]
        # Completeness splits the online population and nothing else.
        assert (
            ligne["en_ligne_complet"] + ligne["en_ligne_partiel"] + ligne["en_ligne_sans_degre"] == ligne["en_ligne"]
        ), f"axe complétude incohérent : {ligne['label']}"


def test_un_membre_trop_peu_observe_n_est_pas_classe():
    """Somebody with a single record is set apart, not labelled as having dropped out.

    The direction acts on these labels; a label the data cannot support is worse than
    no label at all.
    """
    c = pilotage.cohortes_assiduite({}, None)
    classes = sum(x["membres"] for x in c["cohortes"])
    assert classes == c["membres_classes"]
    assert c["membres_donnees_insuffisantes"] >= 0
    assert 0.0 <= c["taux_median"] <= 100.0


# --- Data minimisation ------------------------------------------------------

def test_le_suivi_nominatif_ne_transmet_que_les_champs_declares():
    """The nominative page returns its whitelist and nothing beyond it.

    This is the guarantee that the direction's member view stays a steering tool
    rather than a second copy of the back-office directory.
    """
    r = direction_membres.suivi_assiduite({}, None, limite=5)
    assert set(r["champs_exposes"]) == set(direction_membres._CHAMPS)
    interdits = {"email", "telephone", "adresse", "photo", "date_naissance", "matricule", "id"}
    for m in r["membres"]:
        assert set(m) == set(direction_membres._CHAMPS)
        assert not (set(m) & interdits)


def test_le_suivi_nominatif_borne_sa_pagination():
    """An unbounded page size would let one request pull the whole base."""
    r = direction_membres.suivi_assiduite({}, None, limite=10_000)
    assert r["limite"] <= 500
    assert len(r["membres"]) <= 500


def test_la_couverture_compare_au_perimetre_filtre():
    """The coverage denominator follows the organisational scope, not the whole base.

    Left global, narrowing to a coordination collapsed the rate: seven observed
    members against sixty-four active in the whole organisation reads as eleven per
    cent, and a leader would conclude their coordination reports almost nothing.
    """
    coord = fetch_one("SELECT id FROM coordination ORDER BY nom LIMIT 1", ())
    if not coord:
        pytest.skip("aucune coordination en base")
    globale = pilotage.synthese({}, None)
    ciblee = pilotage.synthese({"coordination": str(coord["id"])}, None)
    assert ciblee["membres_actifs"] <= globale["membres_actifs"]
    assert ciblee["membres_vus"] <= ciblee["membres_actifs"], (
        "plus de membres observés que le périmètre n'en contient : "
        "le dénominateur ne suit pas le filtre"
    )


def test_restreindre_la_periode_ne_reduit_pas_l_effectif():
    """Looking at one quarter does not make the organisation smaller.

    Letting the period narrow the denominator would make coverage climb simply
    because the window shrank, which is the opposite of what the figure is for.
    """
    globale = pilotage.synthese({}, None)
    fenetre = pilotage.synthese({"depuis": "2026-08-01"}, None)
    assert fenetre["membres_actifs"] == globale["membres_actifs"]
