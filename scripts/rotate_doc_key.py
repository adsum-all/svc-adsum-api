"""Re-encrypt every encrypted document with the current primary key.

Zero-downtime rotation procedure:
1. Generate a new Fernet key:  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
2. In the API environment (Vercel), set ADSUM_DOC_ENCRYPTION_KEY to the NEW key
   and move the PREVIOUS key into ADSUM_DOC_ENCRYPTION_KEYS_OLD (comma-separated
   if several). Redeploy: reads keep working (MultiFernet accepts the old key).
3. Run this script once. It downloads each ciphertext, re-encrypts it with the
   new primary key via MultiFernet.rotate (no plaintext exposure), and re-uploads.
4. When it reports 100% rotated, remove the old key from ADSUM_DOC_ENCRYPTION_KEYS_OLD.

The script never prints key material. Requires the same environment as the API
(ADSUM_DATABASE_URL, ADSUM_SUPABASE_URL, ADSUM_SUPABASE_SERVICE_KEY and the keys).
"""
from __future__ import annotations

import sys

from app import crypto, db, storage
from app.config import settings


def main() -> int:
    rows = db.fetch_all(
        "SELECT id, bucket, chemin_stockage FROM document WHERE chiffre = true AND chemin_stockage IS NOT NULL",
        (),
        role=None,
    )
    total = len(rows or [])
    done = failed = 0
    for r in rows or []:
        bucket = str(r.get("bucket") or settings.storage_bucket_documents)
        path = str(r["chemin_stockage"])
        try:
            current = storage.download_bytes(bucket, path)
            rotated = crypto.rotate_token(current)
            if rotated != current:
                storage.upload_bytes(bucket, path, rotated, content_type="image/jpeg")
            done += 1
        except (storage.StorageError, ValueError) as exc:
            failed += 1
            print(f"echec document {r['id']}: {exc}", file=sys.stderr)
    print(f"documents chiffres: {total} | re-chiffres: {done} | echecs: {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
