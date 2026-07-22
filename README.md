# adsum-api

![Python](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.138-009688?logo=fastapi&logoColor=white)
![Starlette](https://img.shields.io/badge/Starlette-1.3-2C2C2C)
![Uvicorn](https://img.shields.io/badge/Uvicorn-0.49-499848?logo=gunicorn&logoColor=white)
![Pydantic](https://img.shields.io/badge/Pydantic-2.13-E92063?logo=pydantic&logoColor=white)
![psycopg](https://img.shields.io/badge/psycopg-3.3-336791?logo=postgresql&logoColor=white)
![PyJWT](https://img.shields.io/badge/PyJWT-2.13-000000?logo=jsonwebtokens&logoColor=white)
![cryptography](https://img.shields.io/badge/cryptography-49.0-3776AB)
![Vercel](https://img.shields.io/badge/Vercel-serverless-000000?logo=vercel&logoColor=white)

Part of the ADSUM platform (membership, QR check-in and attendance).
Subgroup: `services`.

## Role

Business API (REST and WebSocket): members, events, presences, sync, statistics, administration.

## Stack

Python (>=3.11), FastAPI on Uvicorn, Pydantic v2, psycopg 3 (direct PostgreSQL,
no ORM in the API), PyJWT and cryptography for auth. Deployed as Vercel serverless
functions (region cdg1). Exact versions in the table at the bottom.

## Conventions

- Branches: work on `feature/*` or `fix/*` from `develop`, then a merge request.
  Merge order `feature/* -> develop -> main`. Never push to `main`.
- Constitution (zero tolerance): no mock data, no file over 500 lines,
  no em-dash (U+2014 / U+2013), no secret in clear. CI enforces these.
- Commit messages in English, Conventional Commits.

## Endpoints (v0.1)

- `GET /health` - liveness.
- `POST /api/v1/auth/login` - email and password, returns a JWT (Argon2 verify against `utilisateur.hash_mdp`).
- `GET /api/v1/auth/me` - current user from the Bearer token; sets the `adsum.role`
  session variable so the per-role RLS policies apply (ADR-0002).

## Run

```
py -3 -m venv .venv
.venv/Scripts/python -m pip install -e .[dev]
export ADSUM_DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/postgres
export ADSUM_JWT_SECRET=<a strong random secret, 32+ bytes>
.venv/Scripts/python -m uvicorn app.main:app --reload
# OpenAPI docs at http://localhost:8000/docs
```

All configuration is read from `ADSUM_*` environment variables only; no secret is
stored in the repository (Constitution I10).

## Tests

- `pytest` runs unit tests (Argon2, JWT) with no database, so they pass in CI.
- The real login integration test runs only when `ADSUM_DATABASE_URL` and the
  provisioned accounts are available; it proves the real authentication loop.

## CI

Pipelines are defined in `.gitlab-ci.yml`, which includes the shared templates
from `sr-media-ai/adsum/deployment/ci-templates`.

## Stack technique, versions exactes

Versions testées et déployées (relevées via `pip freeze` dans `.venv`). Les contraintes de `requirements.txt` sont des minima ; le tableau donne la version exacte utilisée.

| Composant | Rôle | Version exacte |
| --- | --- | --- |
| Python | Runtime | 3.13.7 (dev), `requires-python >=3.11`, cible ruff py311 |
| FastAPI | Framework API REST | 0.138.1 |
| Starlette | Socle ASGI | 1.3.1 |
| Uvicorn | Serveur ASGI (`[standard]`) | 0.49.0 |
| Pydantic | Validation et schémas | 2.13.4 |
| pydantic-settings | Configuration | 2.14.2 |
| psycopg (`[binary,pool]`) | Driver PostgreSQL | 3.3.4 |
| argon2-cffi | Hachage des mots de passe | 25.1.0 |
| PyJWT | Jetons JWT | 2.13.0 |
| cryptography | Chiffrement (Fernet, etc.) | 49.0.0 |
| httpx | Client HTTP sortant | 0.28.1 |
| openpyxl | Export/import Excel | 3.1.5 |
| python-multipart | Upload de fichiers | 0.0.32 |
| anyio | Primitives async | 4.14.1 |
| Vercel | Hébergement serverless | région `cdg1` (build Python) |
| PostgreSQL (Supabase) | Base de données cible | 17.6 |
