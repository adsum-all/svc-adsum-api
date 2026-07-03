"""Test configuration: provide a JWT secret for the security unit tests."""
from __future__ import annotations

import os

os.environ.setdefault("ADSUM_JWT_SECRET", "test-secret-not-for-production")
os.environ.setdefault("ADSUM_JWT_ALGORITHM", "HS256")
