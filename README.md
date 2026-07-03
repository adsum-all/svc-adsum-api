# adsum-api

Part of the ADSUM platform (membership, QR check-in and attendance).
Subgroup: `services`.

## Role

Business API (REST and WebSocket): members, events, presences, sync, statistics, administration.

## Stack

Python 3.11, FastAPI, Pydantic, SQLAlchemy.

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
