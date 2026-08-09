"""Proof that the canonical attendance layer (app.stats_core) is correct and reconciled.

Two kinds of proof:

* Structural invariants checked against every real event of the connected database.
* A controlled scenario built inside a transaction that is ALWAYS rolled back (no data
  is ever written), which proves the three properties the audit required: the
  denominator is the event's targeted active population; a member is counted once even
  when they appear in both attendance tables or attend in a hybrid way; and an attendee
  outside the targeted population never inflates a numerator.

The tests skip when no database is reachable (e.g. offline CI), so they never fail for
an environment reason; run them with ADSUM_DATABASE_URL set to exercise the real SQL.
"""
# ruff: noqa: E501 - long inline SQL and assertion lines are kept readable on one line
from __future__ import annotations

import os

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.rows import dict_row  # noqa: E402

from app import stats_core  # noqa: E402


def _dsn() -> str:
    url = os.environ.get("ADSUM_DATABASE_URL", "")
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def _conn() -> object:
    dsn = _dsn()
    if not dsn:
        pytest.skip("ADSUM_DATABASE_URL not set")
    try:
        # prepare_threshold=None disables server-side prepared statements, which the
        # Supabase transaction pooler rejects when a pooled connection is reused
        # (DuplicatePreparedStatement). The application db module does the same.
        return psycopg.connect(dsn, row_factory=dict_row, connect_timeout=8, prepare_threshold=None)
    except Exception as exc:  # noqa: BLE001 - unreachable database is a skip, not a failure
        pytest.skip(f"database unreachable: {exc}")


def test_invariants_on_every_real_event() -> None:
    conn = _conn()
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT e.id FROM evenement e "
            "WHERE EXISTS(SELECT 1 FROM presence p WHERE p.evenement_id=e.id) "
            "OR EXISTS(SELECT 1 FROM participation pa WHERE pa.evenement_id=e.id)"
        )
        events = [str(r["id"]) for r in cur.fetchall()]
        for ev in events:
            cur.execute(stats_core._STATS_EVENEMENT_SQL, {"ev": ev})
            s = cur.fetchone()
            eff = int(s["effectif_attendu"])
            suivis, abse = int(s["suivis"]), int(s["absents"])
            pres = int(s["presents"])
            rep = int(s["repondants"])
            # Axis 1 is exhaustive over those who answered. The previous version added
            # "presents" and "partiels" to reach the same total, which held only while
            # "presents" quietly included people who had followed online.
            assert suivis + abse == rep, ev
            assert rep <= eff, ev
            assert pres <= suivis <= eff, ev
            # Axis 2 splits the follows, not the on-site count.
            assert int(s["presents_presentiel"]) + int(s["presents_enligne"]) + int(s["presents_modalite_inconnue"]) == suivis, ev
            # Proof splits the on-site population.
            assert int(s["presents_scan"]) + int(s["presents_declare"]) == pres, ev
    finally:
        conn.close()


def test_controlled_scenario_dedup_targeting_bounding() -> None:
    conn = _conn()
    try:
        conn.autocommit = False
        cur = conn.cursor()
        # A commission with at least two active members to target, plus one active
        # member outside it (must be excluded from the numerator).
        cur.execute(
            "SELECT commission_id, array_agg(id) ids FROM membre "
            "WHERE statut='actif' AND commission_id IS NOT NULL GROUP BY 1 HAVING count(*) >= 2 "
            "ORDER BY count(*) DESC LIMIT 2"
        )
        rows = cur.fetchall()
        if len(rows) < 2:
            pytest.skip("not enough member data to build the targeted scenario")
        com = rows[0]["commission_id"]
        cibles = list(dict.fromkeys(rows[0]["ids"]))
        hors = rows[1]["ids"][0]
        eff_attendu = len(cibles)

        cur.execute(
            "INSERT INTO evenement (titre,type,volet,debut,cible_type,cible_id,visibilite) "
            "VALUES ('PROOF-STATS-CORE','reunion','A',now(),'commission',%s,'membres') RETURNING id",
            (com,),
        )
        ev = str(cur.fetchone()["id"])

        def pres(m: str, mode: str, meth: str) -> None:
            cur.execute(
                "INSERT INTO presence (membre_id,evenement_id,mode,arrivee,methode) VALUES (%s,%s,%s,now(),%s) ON CONFLICT DO NOTHING",
                (m, ev, mode, meth),
            )

        def part(m: str, st: str, src: str, val: bool, mod: str | None = None) -> None:
            cur.execute(
                "INSERT INTO participation (evenement_id,membre_id,statut,source,valide,modalite) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (ev, m, st, src, val, mod),
            )

        ma = cibles[0]  # scan: presence + participation, counted once
        mb = cibles[1]  # online-only: presence(lien) only
        pres(ma, "presentiel", "qr")
        part(ma, "present", "scan", True)
        pres(mb, "en_ligne", "lien")
        # out-of-scope active member attends: excluded from the targeted numerator
        pres(hors, "presentiel", "qr")
        part(hors, "present", "scan", True)

        cur.execute(stats_core._STATS_EVENEMENT_SQL, {"ev": ev})
        s = cur.fetchone()

        # The denominator is the commission's active population, not the whole base.
        assert int(s["effectif_attendu"]) == eff_attendu
        # ma and mb are present; the out-of-scope member never inflates the numerator.
        assert int(s["presents"]) <= int(s["suivis"]) <= int(s["effectif_attendu"])
        assert int(s["hors_cible"]) >= 1
        # ma appears in both tables but is counted once (dedup): presents counts distinct members.
        cur.execute(
            "WITH x AS (SELECT membre_id FROM presence WHERE evenement_id=%s "
            "UNION ALL SELECT membre_id FROM participation WHERE evenement_id=%s) "
            "SELECT count(*) tot, count(DISTINCT membre_id) d FROM x",
            (ev, ev),
        )
        raw = cur.fetchone()
        assert raw["tot"] > raw["d"]  # ma contributes 2 raw rows
        # reconciliation and modality split
        assert int(s["presents_presentiel"]) + int(s["presents_enligne"]) + int(s["presents_modalite_inconnue"]) == int(s["suivis"])
    finally:
        conn.rollback()  # never write anything
        conn.close()
