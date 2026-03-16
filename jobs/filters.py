"""
Job filtering — runs BEFORE AI parsing to keep costs low.

Filters applied (in order):
  1. Skip jobs with no description
  2. Dedup by URL hash — skip already-seen jobs
  3. Blacklist — skip if company/title matches any term
  4. Min salary gate — skip if salary is known and below threshold
"""

import hashlib
import logging

logger = logging.getLogger(__name__)


def url_hash(url: str) -> str:
    """SHA-256 hash of a URL, shortened to 16 hex chars — used as dedup key."""
    return hashlib.sha256((url or "").encode()).hexdigest()[:16]


def apply_filters(
    raw_jobs: list[dict],
    user_filters: dict,
    seen_hashes: set[str],
) -> list[dict]:
    """
    Filter raw JobSpy results. Returns a filtered list with 'url_hash' added to each entry.

    Args:
        raw_jobs:     List of dicts from jobspy DataFrame.to_dict("records")
        user_filters: User.filters JSON — role, location, remote, min_salary, blacklist
        seen_hashes:  Set of url_hash values already in DB for this user
    """
    blacklist = [b.lower().strip() for b in (user_filters.get("blacklist") or []) if b]
    min_salary = int(user_filters.get("min_salary") or 0)

    out = []
    skipped_nodesc = skipped_seen = skipped_blacklist = skipped_salary = 0

    for job in raw_jobs:
        desc = (job.get("description") or "").strip()
        if not desc:
            skipped_nodesc += 1
            continue

        raw_url = job.get("job_url") or job.get("url") or ""
        h = url_hash(raw_url)
        if h in seen_hashes:
            skipped_seen += 1
            continue

        company = (job.get("company") or "").lower()
        title   = (job.get("title") or "").lower()
        if blacklist and any(term in company or term in title for term in blacklist):
            skipped_blacklist += 1
            continue

        # Only gate on salary when job explicitly advertises a max that's below threshold
        max_sal = job.get("max_amount") or job.get("max_salary")
        if min_salary and max_sal:
            try:
                if float(max_sal) < min_salary:
                    skipped_salary += 1
                    continue
            except (TypeError, ValueError):
                pass  # unparseable salary — let it through

        job["url_hash"] = h
        out.append(job)

    logger.info(
        "[filters] in=%d  out=%d | skipped: no_desc=%d  seen=%d  blacklist=%d  salary=%d",
        len(raw_jobs), len(out),
        skipped_nodesc, skipped_seen, skipped_blacklist, skipped_salary,
    )
    return out
