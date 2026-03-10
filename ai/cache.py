"""
Simple in-memory AI response cache.

Keyed by (function_name, content_hash). Clears on bot restart — acceptable for
Phase 1. In Phase 2, swap _store for a Redis/SQLite-backed dict.

Cached:   parse_resume, parse_job, analyze_fit
NOT cached: tailor_resume, write_cover_letter, answer_screening_questions
"""

import hashlib
import json
from typing import Any

_store: dict[str, Any] = {}


def _hash(*args) -> str:
    payload = json.dumps(args, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def get(fn: str, *args) -> Any | None:
    return _store.get(f"{fn}:{_hash(*args)}")


def put(fn: str, value: Any, *args) -> None:
    _store[f"{fn}:{_hash(*args)}"] = value


def invalidate_fit(job: dict, skills: list) -> None:
    """Remove a cached fit result (call when skill graph changes)."""
    key = f"analyze_fit:{_hash(job, skills)}"
    _store.pop(key, None)


def stats() -> dict:
    return {"entries": len(_store), "keys": list(_store.keys())}
