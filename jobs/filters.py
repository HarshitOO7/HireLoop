"""
Job filtering — runs BEFORE AI parsing to keep costs low.

Filters applied (in order):
  1. Skip jobs with no description
  2. Dedup by URL hash — skip already-seen jobs
  3. Semantic dedup (title+company) — same job on multiple boards
  4. Blacklist — skip if company/title matches any term
  5. Min salary gate — skip if salary is known and below threshold
  6. Years of experience — skip if job clearly requires more than user has
"""

import hashlib
import logging
import re

logger = logging.getLogger(__name__)


def url_hash(url: str) -> str:
    """SHA-256 hash of a URL, shortened to 16 hex chars — used as dedup key."""
    return hashlib.sha256((url or "").encode()).hexdigest()[:16]


def semantic_key(job: dict) -> tuple[str, str]:
    """Normalized (title, company) tuple — used for cross-board dedup before AI calls."""
    return (
        (job.get("title") or "").lower().strip(),
        (job.get("company") or "").lower().strip(),
    )


# ── Years-of-experience filter ────────────────────────────────────────────────

# Title keywords that strongly signal high seniority (typically 5+ years required).
# Checked as whole words / bounded substrings to avoid false matches.
_SENIOR_TITLE_WORDS = {
    "senior", "sr", "lead", "staff", "principal", "architect",
    "director", "manager", "head", "vp", "partner", "distinguished",
}

# Title keywords that signal junior/entry level (0–2 years required).
_JUNIOR_TITLE_WORDS = {
    "junior", "jr", "entry", "intern", "graduate", "trainee",
    "associate", "apprentice", "co-op", "coop",
}

# Common words to ignore when building a relevance token set from role titles.
_ROLE_STOP_WORDS = {
    "and", "or", "the", "of", "in", "at", "for", "to", "a", "an",
    "by", "on", "as", "with", "from", "is", "it", "its", "be",
}

# Regex that finds year-requirement phrases in job descriptions.
# Four capture groups, one per pattern variant — used by _min_years_from_jd to
# distinguish "hard minimum" phrases from range phrases.
#
# Group 1 — hard min: "5+ years", "5 or more years", "5 plus years"
# Group 2 — range:    "3-5 years", "3 to 5 years"  (often tool-specific, secondary)
# Group 3 — hard min: "minimum 5 years", "minimum of 5 years", "at least 5 years"
# Group 4 — hard min: "5 years of experience", "5 years of software engineering experience"
_JD_YEARS_RE = re.compile(
    r"""
    (?:
        # Group 1: "5+ years", "5 or more years", "5 plus years"
        (\d+)\s*(?:\+|or\s+more|plus)\s*years?
        |
        # Group 2: "5 to 8 years", "5-8 years", "5–8 years"
        (\d+)\s*(?:to|–|-)\s*\d+\s*years?
        |
        # Group 3: "minimum 5 years", "minimum of 5 years", "at least 5 years"
        (?:minimum|min\.?|at\s+least)\s+(?:of\s+)?(\d+)\s*\+?\s*years?
        |
        # Group 4: "5 years of experience", "5 years of software engineering experience"
        (\d+)\s*years?'?\s*(?:of\s+(?:\w+\s+){0,3})?experience
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _title_words(title: str) -> set[str]:
    """Return lowercase tokens from a job title, stripped of punctuation."""
    return set(re.split(r"[\s/\-–,.()|]+", title.lower()))


def _build_relevance_tokens(search_terms: list[str]) -> set[str]:
    """
    Extract meaningful tokens from the role search terms used to scrape jobs.

    These tokens are compared against incoming job titles to catch off-topic results
    returned by job boards due to loose keyword matching (e.g. Indeed returning
    "CNC Machinist Programmer" when searching "Software Developer", or returning
    "Software Engineer" when a nurse searches for "Registered Nurse").

    Works for any role — tech or non-tech — because it derives from the user's own
    search terms rather than a hardcoded category list.

    Returns empty set if no terms are available → relevance check is skipped
    (fail-open: a false pass is cheaper than a false drop).
    """
    tokens: set[str] = set()
    for term in search_terms:
        for word in re.split(r"[\s,/\-–.()|]+", term.lower()):
            if len(word) >= 2 and word not in _ROLE_STOP_WORDS:
                tokens.add(word)
    return tokens


def _min_years_from_title(title: str) -> int | None:
    """
    Return implied minimum years required based on seniority keywords in the title.
    Returns None if the title gives no clear signal.
    Conservative: only triggers on unambiguous senior-level words.
    """
    words = _title_words(title)
    if words & _SENIOR_TITLE_WORDS:
        return 5
    if words & _JUNIOR_TITLE_WORDS:
        return 0
    return None


def _min_years_from_jd(desc: str) -> int | None:
    """
    Scan the first 4,000 chars of a job description for year-requirement phrases.

    Distinguishes hard minimums (groups 1, 3, 4) from ranges (group 2).
    Ranges often describe tool-specific requirements ("3-5 years Python") which
    are secondary to the overall experience requirement ("minimum 5 years total").

    Strategy (conservative — avoids false drops):
      - If any hard minimum phrases are found → return the MINIMUM of those values.
        Example: "5+ years total, 8+ years preferred" → 5
      - If only ranges are found → return the MINIMUM of those.
        Example: "3-5 years Python, 2-4 years SQL" → 2
      - If nothing found → None (filter skipped, job passes through to AI).
    """
    snippet = desc[:4000]
    hard_mins: list[int] = []  # groups 1, 3, 4 — explicit minimums
    ranges:    list[int] = []  # group 2 — range lower-bounds (often tool-specific)

    for m in _JD_YEARS_RE.finditer(snippet):
        g1, g2, g3, g4 = m.group(1), m.group(2), m.group(3), m.group(4)
        if g1:
            hard_mins.append(int(g1))
        elif g2:
            ranges.append(int(g2))
        elif g3:
            hard_mins.append(int(g3))
        elif g4:
            hard_mins.append(int(g4))

    if hard_mins:
        return min(hard_mins)
    if ranges:
        return min(ranges)
    return None


def _job_min_years(title: str, desc: str) -> int | None:
    """
    Return the minimum years of experience a job requires, using title first,
    then JD scan if the title gives no signal.  Returns None if unknown.
    """
    years = _min_years_from_title(title)
    if years is not None:
        return years
    return _min_years_from_jd(desc)


def _parse_user_max_years(years_str: str | None) -> int | None:
    """
    Parse the user's years_of_exp preference into a maximum-years-I-have value.
    Returns None meaning 'no filter'.

    Accepted formats (case-insensitive, spaces optional):
      any          → no filter
      < 4  / <4   → user has up to 3 years (max = 3)
      <= 4 / <=4  → user has up to 4 years (max = 4)
      1-3  / 1 to 3 → user has 1–3 years (max = 3)
      2-5          → user has 2–5 years (max = 5)
      3+           → user has 3+ years (no upper cap → no filter on too-high requirements)
    """
    if not years_str:
        return None
    s = years_str.strip().lower()
    if s in ("any", "", "none"):
        return None

    # "< N" — strictly less than
    m = re.match(r"^<\s*(\d+)$", s)
    if m:
        return int(m.group(1)) - 1

    # "<= N" — less than or equal
    m = re.match(r"^<=\s*(\d+)$", s)
    if m:
        return int(m.group(1))

    # "N-M" or "N to M"
    m = re.match(r"^(\d+)\s*(?:-|to)\s*(\d+)$", s)
    if m:
        return int(m.group(2))

    # "N+" — user has at least N years, no upper bound
    m = re.match(r"^(\d+)\s*\+$", s)
    if m:
        return None  # no upper cap → don't filter out high-requirement jobs

    logger.debug("[filters] unrecognised years_of_exp format %r — skipping filter", years_str)
    return None


# ── Main filter entry point ───────────────────────────────────────────────────

def apply_filters(
    raw_jobs: list[dict],
    user_filters: dict,
    seen_hashes: set[str],
    seen_keys: set[tuple] | None = None,
    search_terms: list[str] | None = None,
) -> list[dict]:
    """
    Filter raw JobSpy results. Returns filtered list with 'url_hash' added.

    Dedup order (cheapest first — AI is only called after all these pass):
      1. No description → skip
      2. URL hash match → skip (exact same URL seen before)
      3. Semantic match (title+company) → skip (same job on another board)
      4. Blacklist → skip
      4b. Role relevance → skip if title shares no tokens with search terms
      5. Salary gate → skip
      6. Years of experience → skip if job clearly requires more than user has

    Args:
        raw_jobs:     List of dicts from jobspy
        user_filters: User.filters JSON — role, location, remote, min_salary,
                      blacklist, years_of_exp
        seen_hashes:  Set of url_hash values already in DB for this user
        seen_keys:    Set of (title, company) tuples already in DB
        search_terms: All role titles sent to the scraper (base role + AI variants).
                      Used to build a relevance token set — works for any role/industry.
                      If None, falls back to user_filters["role"]; if still empty, skip check.
    """
    blacklist  = [b.lower().strip() for b in (user_filters.get("blacklist") or []) if b]
    min_salary = int(user_filters.get("min_salary") or 0)
    user_max_years = _parse_user_max_years(user_filters.get("years_of_exp"))
    _seen_keys = seen_keys or set()

    # Build relevance tokens from all search terms (role variants + base role).
    # Works for any industry — derived from the user's own search, not a hardcoded category.
    _base_role = (user_filters.get("role") or "").strip()
    _all_terms = list(search_terms or []) or ([_base_role] if _base_role else [])
    _relevance_tokens = _build_relevance_tokens(_all_terms)

    out = []
    skipped_nodesc = skipped_seen = skipped_semantic = 0
    skipped_blacklist = skipped_salary = skipped_years = skipped_irrelevant = 0

    for job in raw_jobs:
        # 1. Must have a description
        desc = (job.get("description") or "").strip()
        if not desc:
            skipped_nodesc += 1
            continue

        # 2. URL dedup
        raw_url = job.get("job_url") or job.get("url") or ""
        h = url_hash(raw_url)
        if h in seen_hashes:
            skipped_seen += 1
            continue
        seen_hashes.add(h)

        # 3. Semantic dedup (title+company)
        key = semantic_key(job)
        if key in _seen_keys:
            skipped_semantic += 1
            continue
        _seen_keys.add(key)

        # 4. Blacklist
        company = key[1]
        title   = key[0]
        if blacklist and any(term in company or term in title for term in blacklist):
            skipped_blacklist += 1
            continue

        # 4b. Role relevance — drop if job title shares no tokens with what was searched.
        # Catches off-topic board results (e.g. Indeed returning "CNC Machinist" for a
        # "Software Developer" search, or "Software Engineer" for a "Registered Nurse" search).
        # Skipped entirely if no tokens could be built — fail-open so no valid job is lost.
        title_word_set = _title_words(title)
        if _relevance_tokens and not (title_word_set & _relevance_tokens):
            skipped_irrelevant += 1
            logger.debug("[filters] off-topic: dropped %r", title)
            continue

        # 5. Salary gate
        max_sal = job.get("max_amount") or job.get("max_salary")
        if min_salary and max_sal:
            try:
                if float(max_sal) < min_salary:
                    skipped_salary += 1
                    continue
            except (TypeError, ValueError):
                pass

        # 6. Years of experience gate
        # Only drop if we are CONFIDENT the job requires more than user has.
        # If signal is ambiguous or missing → always let through.
        if user_max_years is not None:
            job_min = _job_min_years(title, desc)
            if job_min is not None and job_min > user_max_years:
                skipped_years += 1
                logger.debug(
                    "[filters] years gate: dropped %r — requires %d yrs, user max=%d",
                    title, job_min, user_max_years,
                )
                continue

        job["url_hash"] = h
        out.append(job)

    logger.info(
        "[filters] in=%d  out=%d | skipped: no_desc=%d  url=%d  semantic=%d  "
        "blacklist=%d  non_tech=%d  salary=%d  years=%d",
        len(raw_jobs), len(out),
        skipped_nodesc, skipped_seen, skipped_semantic,
        skipped_blacklist, skipped_irrelevant, skipped_salary, skipped_years,
    )
    return out
