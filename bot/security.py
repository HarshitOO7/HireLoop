"""
Input validation, injection detection, and rate limiting for AI-exposed endpoints.
"""

import re
import logging
from datetime import date

logger = logging.getLogger(__name__)

_INJECTION_RE = re.compile(
    r"ignore (previous|above|all) (instructions?|prompts?)|"
    r"\byou are now\b|pretend (to be|you are)|"
    r"\bdisregard\b|\boverride instructions?\b|"
    r"system\s*:|<\s*/?system\s*>|forget (everything|previous)|"
    r"new instructions?\s*:|jailbreak|\bDAN\b",
    re.IGNORECASE,
)


def guard_input(text: str, max_chars: int, field: str) -> str:
    """Truncate oversized input and log it. Returns the (possibly truncated) text."""
    if len(text) > max_chars:
        logger.warning("[security] %s input truncated: %d → %d chars", field, len(text), max_chars)
        return text[:max_chars]
    return text


def contains_injection(text: str) -> bool:
    """Return True if text looks like a prompt injection attempt."""
    return bool(_INJECTION_RE.search(text))


def check_rate_limit(bot_data: dict, tg_id: str, action: str, daily_limit: int) -> bool:
    """
    Increment the per-user daily counter for `action`.
    Returns True if the call is allowed, False if the daily limit is exceeded.
    Uses bot_data["_rate_limits"] as in-memory storage (resets each day automatically).
    """
    today = str(date.today())
    key = f"{tg_id}:{action}:{today}"
    limits: dict = bot_data.setdefault("_rate_limits", {})
    count = limits.get(key, 0)
    if count >= daily_limit:
        logger.warning("[security] rate limit hit — tg_id=%s action=%s count=%d limit=%d",
                       tg_id, action, count, daily_limit)
        return False
    limits[key] = count + 1
    return True
