"""
Resume section order — pure Python decision tree, zero AI tokens.

Three profiles:
  experienced     → SUMMARY / WORK EXPERIENCE / SKILLS / EDUCATION / PROJECTS
  career_changer  → SUMMARY / SKILLS / WORK EXPERIENCE / EDUCATION / PROJECTS
  fresher         → SUMMARY / SKILLS / EDUCATION / PROJECTS / WORK EXPERIENCE

Decision rules:
  fresher         = total experience < 2 years
  career_changer  = domain_mismatch(past_roles, target) AND years >= 2 AND longest_role >= 6m
  experienced     = everything else (same domain, 2+ years)

Result stored in user.filters["resume_section_order"] after onboarding / resume upload.
Groq fallback deferred to post-MVP (edge cases are rare in practice).
"""

from __future__ import annotations
import logging

logger = logging.getLogger(__name__)

SECTION_ORDERS: dict[str, list[str]] = {
    "experienced":    ["SUMMARY", "WORK EXPERIENCE", "SKILLS", "EDUCATION", "PROJECTS"],
    "career_changer": ["SUMMARY", "SKILLS", "WORK EXPERIENCE", "EDUCATION", "PROJECTS"],
    "fresher":        ["SUMMARY", "SKILLS", "EDUCATION", "PROJECTS", "WORK EXPERIENCE"],
}

# ── Domain keyword map ────────────────────────────────────────────────────────

_DOMAIN_MAP: dict[str, set[str]] = {
    "tech": {
        "software", "developer", "engineer", "programmer", "backend", "frontend",
        "fullstack", "full-stack", "devops", "data", "machine learning", "ml",
        "ai", "swe", "sde", "ios", "android", "web", "cloud", "platform",
        "security", "qa", "testing", "architect", "sre", "database", "dba",
        "infrastructure", "systems",
    },
    "healthcare": {
        "nurse", "doctor", "physician", "therapist", "medical", "clinical",
        "pharmacist", "dentist", "surgeon", "emt", "paramedic", "psychologist",
        "counselor", "radiologist", "technician",
    },
    "finance": {
        "accountant", "analyst", "banker", "financial", "auditor", "actuary",
        "trader", "broker", "controller", "cfa", "cpa",
    },
    "marketing": {
        "marketer", "marketing", "seo", "content", "brand", "growth",
        "social media", "communications", "pr", "copywriter", "media",
    },
    "design": {
        "designer", "ux", "ui", "graphic", "creative", "art director",
        "illustrator", "visual",
    },
    "management": {
        "manager", "director", "vp", "ceo", "cto", "coo", "lead",
        "program manager", "project manager", "scrum master", "president",
    },
}


def _domain_of(title: str) -> str | None:
    """Return the domain label for a role title, or None if unknown."""
    t = title.lower()
    for domain, keywords in _DOMAIN_MAP.items():
        if any(kw in t for kw in keywords):
            return domain
    return None


# ── Core decision tree ────────────────────────────────────────────────────────

def infer_profile(target_role: str, work_history: list[dict]) -> str:
    """
    Return one of: "experienced" | "career_changer" | "fresher"

    work_history items expected keys:
        role_title: str
        duration_months: int
    """
    if not work_history:
        logger.debug("[section_order] no work history → fresher")
        return "fresher"

    total_months = sum((e.get("duration_months") or 0) for e in work_history)
    longest      = max((e.get("duration_months") or 0) for e in work_history)

    # Freshers: under 2 years total (including part-time / short stints)
    if total_months / 12 < 2:
        logger.debug("[section_order] total_months=%d < 24 → fresher", total_months)
        return "fresher"

    target_domain   = _domain_of(target_role)
    history_domains = {d for e in work_history if (d := _domain_of(e.get("role_title", "")))}

    # Unknown domains — don't penalise niche roles, treat as matching
    if target_domain and history_domains:
        domain_match = target_domain in history_domains
    else:
        domain_match = True

    logger.debug(
        "[section_order] total_months=%d  longest=%dm  target_domain=%r  "
        "history_domains=%s  domain_match=%s",
        total_months, longest, target_domain, history_domains, domain_match,
    )

    # Career changer: significant experience (2+ years, at least 6m in one role)
    # but in a different domain from the target
    if not domain_match and longest >= 6:
        return "career_changer"

    return "experienced"


def get_section_order(target_role: str, work_history: list[dict]) -> list[str]:
    """
    Return ordered list of resume section names for the given profile.

    Args:
        target_role:  The role the user is targeting (e.g. "Software Engineer")
        work_history: List of dicts with keys role_title, duration_months
    """
    profile = infer_profile(target_role, work_history)
    order   = SECTION_ORDERS[profile]
    logger.info("[section_order] profile=%r  order=%s", profile, order)
    return order
