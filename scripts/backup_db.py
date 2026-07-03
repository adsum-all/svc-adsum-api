"""Local backup utility: dump every public table (except audit_*) into a zip of
CSV files plus a manifest. Reads the Supabase secret directly; run locally only."""
from __future__ import annotations

import datetime
import io
import json
import pathlib
import sys
import zipfile

import psycopg

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
SEC = r"C:/Users/kouas/Documents/deepl-test/95-sr-adsum/.secret"
sup = json.load(open(f"{SEC}/supabase-secret-adsum.json"))["supabase"]
DSN = (
    f"postgresql://postgres.{sup['project_id']}:{sup['db_password']}"
    f"@aws-0-{sup.get('region', 'eu-west-3')}.pooler.supabase.com:5432/postgres?sslmode=require"
)
stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
outdir = pathlib.Path(SEC) / "backups"
outdir.mkdir(exist_ok=True)
zpath = outdir / f"adsum-db-{stamp}.zip"
with psycopg.connect(DSN) as c, c.cursor() as cur:
    cur.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname='public' "
        "AND tablename NOT LIKE 'audit_%' ORDER BY tablename"
    )
    tables = [r[0] for r in cur.fetchall()]
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        total_rows = 0
        for t in tables:
            buf = io.BytesIO()
            with cur.copy(f'COPY (SELECT * FROM "{t}") TO STDOUT WITH (FORMAT csv, HEADER true)') as cp:
                for chunk in cp:
                    buf.write(bytes(chunk))
            data = buf.getvalue()
            z.writestr(f"{t}.csv", data)
            total_rows += max(0, data.count(b"\n") - 1)
        manifest = {
            "date": stamp,
            "tables": len(tables),
            "note": "Donnees completes (CSV par table). Schema: migrations Alembic du repo deployment/database.",
        }
        z.writestr("MANIFEST.json", json.dumps(manifest, indent=2))
print(f"sauvegarde: {zpath.name} | {len(tables)} tables | ~{total_rows} lignes | {zpath.stat().st_size // 1024} Ko")
print(str(zpath))
