"""Length-bounded string types for request bodies.

Pydantic puts no upper bound on a plain ``str``: without an explicit limit a
single field can carry megabytes, which is a memory and denial-of-service
vector and lets oversized values reach the database. These annotated aliases
cap free-text inputs at sensible sizes so oversized payloads are rejected at
the validation boundary, before any handler or query runs.
"""
from __future__ import annotations

from typing import Annotated

from pydantic import Field

# Short identifiers, codes, slugs, phone numbers, enum-like values, UUIDs.
ShortStr = Annotated[str, Field(max_length=120)]
# Names, single-line labels, addresses, professions.
LineStr = Annotated[str, Field(max_length=300)]
# A subject or title line.
TitleStr = Annotated[str, Field(max_length=200)]
# Free-text body: a message, a comment, a description.
TextStr = Annotated[str, Field(max_length=5000)]
# A long-form answer submitted to a course.
LongTextStr = Annotated[str, Field(max_length=10000)]
