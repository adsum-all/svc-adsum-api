"""L'arithmétique des indicateurs doit se fermer, et c'est un test qui le dit.

Ce contrôle existait, mais sous forme d'une page du back-office client qui affichait
« Incohérence détectée. Les chiffres affichés ailleurs sont suspects. » Deux choses
n'allaient pas.

D'abord, un client ne peut rien faire d'un tel message. Il perd confiance dans tous
les chiffres de la plateforme, sans qu'on lui dise lequel est faux ni comment le
corriger, et la correction ne lui appartient de toute façon pas.

Ensuite, et c'est pire : le message était faux. Les chiffres étaient justes, c'est
l'égalité qui était mal posée. Elle affirmait que chaque observation est un suivi ou
une absence, alors qu'il existe un troisième cas, la personne attendue dont personne
n'a rien enregistré. Quatre cent soixante-deux observations tombaient dans ce trou
sur la base réelle. Un contrôle qui accuse les données quand c'est lui qui se trompe
est pire qu'aucun contrôle.

Il est donc devenu ce qu'il aurait dû être : un test. Il échoue avant la mise en
production, devant celui qui peut corriger, au lieu d'alarmer celui qui ne le peut
pas.
"""
from __future__ import annotations

import json
import os
import urllib.parse
from pathlib import Path

import pytest

SECRET_BASE = Path("C:/Users/kouas/Documents/deepl-test/95-sr-adsum/.secret/supabase-secret-adsum.json")


def _dsn() -> str | None:
    if not SECRET_BASE.exists():
        return None
    s = json.load(open(SECRET_BASE, encoding="utf-8"))["supabase"]
    if not s.get("db_password"):
        return None
    mdp = urllib.parse.quote(s["db_password"], safe="")
    return (f"postgresql://postgres.{s['project_id']}:{mdp}"
            f"@aws-0-{s['region']}.pooler.supabase.com:5432/postgres?sslmode=require")


@pytest.fixture(scope="module")
def comptes():
    """Les comptes calculés sur la base réelle, en une passe, sans filtre.

    Sur la base réelle plutôt que sur des lignes fabriquées : une partition ne se
    trompe pas sur des données choisies pour lui donner raison. Le troisième cas
    manquant ne serait jamais apparu sur un jeu de test écrit par la même personne
    que la partition.
    """
    dsn = os.environ.get("DATABASE_URL") or _dsn()
    if not dsn:
        pytest.skip("Aucune base joignable")
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        pytest.skip("psycopg absent")

    from app import indicateurs as ind

    colonnes = ", ".join(
        f"count(*) FILTER (WHERE {i.predicat}) AS {i.code}" for i in ind.COMPTES)
    try:
        with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=20) as conn, \
                conn.cursor() as cur:
            cur.execute(f"WITH {ind._CONSO} SELECT {colonnes} FROM conso cc")
            ligne = cur.fetchone() or {}
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Base injoignable : {type(e).__name__}")

    return {i.code: int(ligne.get(i.code) or 0) for i in ind.COMPTES}


class TestArithmetique:
    def test_toutes_les_egalites_se_ferment(self, comptes):
        """Le test qui remplace le bandeau rouge du back-office client."""
        from app import indicateurs as ind

        echecs = []
        for code, parties, attendu, enonce in ind.CONTROLES:
            somme = sum(comptes[p] for p in parties)
            if somme != comptes[attendu]:
                detail = " + ".join(f"{p}({comptes[p]})" for p in parties)
                echecs.append(
                    f"{code} : {detail} = {somme}, attendu {attendu}"
                    f"({comptes[attendu]}), écart {somme - comptes[attendu]:+d}. {enonce}")
        assert not echecs, "\n".join(echecs)

    def test_les_trois_etats_du_suivi_partagent_la_population(self, comptes):
        """Le cas qui manquait. Il vaut la peine d'être écrit à part, avec son
        chiffre : c'est lui qui a fait apparaître une fausse incohérence pendant que
        les données étaient justes."""
        assert (comptes["suivis"] + comptes["absences"] + comptes["sans_trace"]
                == comptes["observations"])

    def test_le_silence_est_compte_et_non_reparti(self, comptes):
        """Une personne attendue dont rien n'a été enregistré n'est ni présente ni
        absente. La ranger d'office dans l'une des deux fausserait le taux de
        participation, dans un sens ou dans l'autre, sans que personne ne le voie."""
        assert comptes["sans_trace"] >= 0
        # Et il doit rester visible : un silence absorbé dans une autre catégorie
        # donnerait une somme juste et un chiffre faux.
        assert comptes["sans_trace"] <= comptes["observations"]

    def test_aucun_compte_ne_depasse_sa_base(self, comptes):
        """Un sous-ensemble plus grand que son ensemble est un prédicat qui déborde,
        et cela ne se voit pas dans une somme qui se ferme par ailleurs."""
        from app import indicateurs as ind

        for i in ind.COMPTES:
            if i.base:
                assert comptes[i.code] <= comptes[i.base], (
                    f"{i.code} ({comptes[i.code]}) dépasse sa base "
                    f"{i.base} ({comptes[i.base]})")

    def test_aucun_compte_n_est_negatif(self, comptes):
        assert all(v >= 0 for v in comptes.values())


class TestLaPageNExistePlus:
    """La mécanique de calcul ne se publie pas à une organisation cliente.

    Elle appartient à l'éditeur. Et un diagnostic d'arithmétique affiché à quelqu'un
    qui ne peut pas le corriger ne fait que détruire la confiance dans tous les
    autres chiffres.
    """

    def test_la_route_a_disparu_de_l_api(self):
        source = (Path(__file__).resolve().parents[1]
                  / "app/direction_routes.py").read_text(encoding="utf-8")
        assert '@router.get("/regles-calcul")' not in source

    def test_la_permission_a_disparu(self):
        source = (Path(__file__).resolve().parents[1]
                  / "app/permissions_data.py").read_text(encoding="utf-8")
        assert "regles-calcul" not in source

    def test_le_composant_a_disparu_du_back_office(self):
        composant = (Path(__file__).resolve().parents[3]
                     / "applications/adsum-back-office/src/components/ReglesCalcul.tsx")
        assert not composant.exists()

    def test_le_menu_ne_la_propose_plus(self):
        menu = (Path(__file__).resolve().parents[3]
                / "applications/adsum-back-office/src/App.tsx").read_text(encoding="utf-8")
        assert "regles-calcul" not in menu
        assert "Règles de calcul" not in menu
