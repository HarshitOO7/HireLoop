"""
Resume generator — orchestrates ai.tailor_resume() and persists the result.

Flow:
  1. Load Job + User + verified SkillNodes + SkillEvidence from DB
  2. Infer section order (pure Python, zero tokens)
  3. Build evidence notes string
  4. Call ai.tailor_resume()
  5. Split output at ---COVER LETTER--- and strip ---CHANGES--- section
  6. Persist resume_markdown + cover_letter_markdown into Application row
  7. Return the Application object

Called by:
  bot/handlers/job_approval.py  after skill verification is complete
"""

import re
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


# ── Resume utilities ───────────────────────────────────────────────────────────

def _compress_resume(text: str) -> str:
    """Collapse whitespace bloat from PDF extraction without losing any content."""
    # Collapse 3+ consecutive blank lines → single blank line
    text = re.sub(r'\n{3,}', '\n\n', text)
    # Strip trailing whitespace from every line
    text = "\n".join(line.rstrip() for line in text.splitlines())
    # Remove decorative separator lines (----, ====, ....)
    text = re.sub(r'^[\-=\.]{4,}\s*$', '', text, flags=re.MULTILINE)
    return text.strip()


def apply_patch(current_md: str, patch_output: str) -> str:
    """Splice AI-returned changed sections back into the current resume markdown.

    Handles three cases:
    - <reorder>SEC1, SEC2, ...</reorder>  — reorder sections without changing content
    - <section name="NAME">...</section>  — replace or add a section
    - <section name="CANNOT_APPLY">...</section> — silently ignored (caller checks separately)
    """
    # ── Reorder ───────────────────────────────────────────────────────────────
    reorder_m = re.search(r'<reorder>(.*?)</reorder>', patch_output, re.IGNORECASE | re.DOTALL)
    if reorder_m:
        new_order = [s.strip().upper() for s in reorder_m.group(1).split(',')]
        # Extract header (everything before first ## section)
        header_m = re.match(r'^(.*?)(?=\n## )', current_md, re.DOTALL)
        header   = header_m.group(1).strip() if header_m else ''
        # Extract all existing sections preserving original heading capitalisation
        sections: dict[str, tuple[str, str]] = {}  # UPPER_NAME → (orig_heading, content)
        for m in re.finditer(r'## ([^\n]+)\n(.*?)(?=\n## |\Z)', current_md, re.DOTALL):
            key = m.group(1).strip().upper()
            sections[key] = (m.group(1).strip(), m.group(2).strip())
        # Rebuild in requested order, then append anything not mentioned
        parts = [header] if header else []
        placed: set[str] = set()
        for sec in new_order:
            if sec in sections:
                orig, content = sections[sec]
                parts.append(f"## {orig}\n{content}")
                placed.add(sec)
        for key, (orig, content) in sections.items():
            if key not in placed:
                parts.append(f"## {orig}\n{content}")
        return '\n\n'.join(parts)

    # ── Section content patches ────────────────────────────────────────────────
    for m in re.finditer(r'<section name="([^"]+)">(.*?)</section>', patch_output, re.DOTALL):
        section_name = m.group(1).strip().upper()
        if section_name == 'CANNOT_APPLY':
            continue  # caller handles feedback
        new_content = m.group(2).strip()
        pattern = rf"(## {re.escape(section_name)}\n)(.*?)(?=\n## |\Z)"
        if re.search(pattern, current_md, re.DOTALL):
            # Section exists — replace content in-place
            current_md = re.sub(pattern, rf"\g<1>{new_content}\n\n", current_md, flags=re.DOTALL)
        else:
            # New section — insert before EDUCATION/PROJECTS if present, else append
            title_case = section_name.title()
            inserted = False
            for anchor in ['## EDUCATION', '## PROJECTS']:
                if anchor in current_md:
                    current_md = current_md.replace(
                        anchor, f"## {title_case}\n{new_content}\n\n{anchor}", 1)
                    inserted = True
                    break
            if not inserted:
                current_md = current_md.rstrip() + f"\n\n## {title_case}\n{new_content}"
    return current_md

# ── Contact extraction ─────────────────────────────────────────────────────────

_RE_EMAIL    = re.compile(r"[\w.+-]+@[\w-]+\.[a-z]{2,}", re.IGNORECASE)
# Requires a full North-American phone structure (10+ digits) — rejects 8-digit IDs from URLs
_RE_PHONE    = re.compile(
    r"(?:\+?1[\s.\-]?)?"           # optional country code +1
    r"(?:\(?\d{3}\)?[\s.\-]?)"     # area code
    r"\d{3}[\s.\-]?\d{4}",         # 7-digit local
)
_RE_LINKEDIN = re.compile(r"linkedin\.com/in/[\w\-]+", re.IGNORECASE)
_RE_GITHUB   = re.compile(r"github\.com/[\w\-]+", re.IGNORECASE)
# Strip these domains before phone search so numeric profile IDs aren't matched as phones
_RE_STRIP_URLS = re.compile(
    r"https?://\S+|(?:linkedin|github|behance|dribbble|kaggle)\.com\S*",
    re.IGNORECASE,
)


def _format_phone(raw: str) -> str | None:
    """Normalize a raw phone string to (XXX) XXX-XXXX or +1 (XXX) XXX-XXXX."""
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("1") and len(digits) == 11:
        digits = digits[1:]          # strip country code, handle below
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    if len(digits) > 10:
        return raw                   # international — keep original
    return None                      # too short to be a real phone number


def _extract_contact(raw_text: str) -> dict:
    """
    Scan raw resume text for name and contact fields.
    Returns dict with keys: name, phone, email, linkedin_url, github_url.
    Any field may be None if not found.
    """
    lines = [l.strip() for l in raw_text.splitlines() if l.strip()]
    # Name: first non-empty line; strip markdown # prefix and any trailing location.
    # Location may be separated by | or a tab (both are common in PDF-extracted resumes).
    name_raw = lines[0].lstrip("# ").strip() if lines else None
    name = re.split(r"[\|\t]", name_raw)[0].strip() if name_raw else None

    email        = m.group(0) if (m := _RE_EMAIL.search(raw_text))    else None
    linkedin_url = m.group(0) if (m := _RE_LINKEDIN.search(raw_text)) else None
    github_url   = m.group(0) if (m := _RE_GITHUB.search(raw_text))   else None

    # Strip all URLs before scanning for phone — prevents matching numeric profile IDs
    # (e.g. linkedin.com/in/aman-mishra-90192924 → "90192924" must not become the phone)
    _no_urls = _RE_STRIP_URLS.sub(" ", raw_text)
    _raw_phone = m.group(0).strip() if (m := _RE_PHONE.search(_no_urls)) else None
    phone = _format_phone(_raw_phone) if _raw_phone else None

    return {
        "name":         name,
        "phone":        phone,
        "email":        email,
        "linkedin_url": linkedin_url,
        "github_url":   github_url,
    }


def _build_header(contact: dict, is_tech: bool) -> str:
    """
    Build the resume header markdown from extracted contact info.
    No city. GitHub only for tech roles.
    """
    name = contact.get("name") or "Candidate"
    tokens = [t for t in [
        contact.get("phone"),
        contact.get("email"),
        contact.get("linkedin_url"),
        contact.get("github_url") if is_tech else None,
    ] if t]
    contact_line = " | ".join(tokens)
    return f"# {name}\n{contact_line}"


async def generate_resume(
    job_id: str,
    user_id: str,
    ai,                   # HireLoopAI instance
) -> "Application | None":
    """
    Generate a tailored resume for job_id and persist it in the Application row.
    Returns the Application, or None on failure.
    """
    from sqlalchemy import select
    from db.models import Application, Job, SkillEvidence, SkillNode, User
    from db.session import AsyncSessionLocal
    from resume.section_order import get_section_order, _domain_of

    async with AsyncSessionLocal() as session:

        # ── Load job ──────────────────────────────────────────────────────────
        job_result = await session.execute(select(Job).where(Job.id == job_id))
        job = job_result.scalar_one_or_none()
        if not job:
            logger.error("[generator] job %s not found", job_id)
            return None

        # ── Load user ─────────────────────────────────────────────────────────
        user_result = await session.execute(select(User).where(User.id == user_id))
        user = user_result.scalar_one_or_none()
        if not user:
            logger.error("[generator] user %s not found", user_id)
            return None

        if not user.base_resume_markdown:
            logger.error("[generator] user %s has no base resume — upload one first", user_id)
            return None

        filters               = user.filters or {}
        work_history          = filters.get("work_history", [])
        target_role           = filters.get("role", "")
        special_instructions  = filters.get("resume_instructions", "")

        # ── Verified skills only (hard rule) ──────────────────────────────────
        node_result = await session.execute(
            select(SkillNode).where(
                SkillNode.user_id == user_id,
                SkillNode.status.like("verified_%"),
            )
        )
        skill_nodes = node_result.scalars().all()

        if not skill_nodes:
            logger.warning("[generator] user %s has no verified skills", user_id)
            return None

        node_by_id = {n.id: n for n in skill_nodes}

        # ── Load evidence for context notes ───────────────────────────────────
        ev_result = await session.execute(
            select(SkillEvidence).where(
                SkillEvidence.skill_node_id.in_(list(node_by_id.keys()))
            )
        )
        evidences = ev_result.scalars().all()

        evidence_lines: list[str] = []
        for ev in evidences:
            if not ev.user_context:
                continue
            node = node_by_id.get(ev.skill_node_id)
            skill_name = node.skill_name if node else "?"
            note = f"• {skill_name}: {ev.user_context}"
            if ev.company:
                note += f" (@ {ev.company}"
                if ev.duration_months:
                    note += f", {ev.duration_months}m"
                note += ")"
            evidence_lines.append(note)

        # ── Synthesize work history from skill evidence ────────────────────────
        # Group all evidence by company so we can reconstruct jobs that may be
        # absent from the uploaded base resume (e.g. added via skill verification)
        _work_ev: dict[str, list] = {}
        for ev in evidences:
            if ev.company:
                _work_ev.setdefault(ev.company, []).append(ev)

        _synth_lines: list[str] = []
        for company, evs in _work_ev.items():
            role      = next((e.role_title     for e in evs if e.role_title),     None)
            duration  = next((e.duration_months for e in evs if e.duration_months), None)
            last_year = next((e.last_used_year  for e in evs if e.last_used_year),  None)
            date_parts = [p for p in [
                f"{duration}m" if duration else None,
                str(last_year) if last_year else None,
            ] if p]
            role_line = f"**{role or 'Role'}**" + (f" | {', '.join(date_parts)}" if date_parts else "")
            _synth_lines.append(role_line)
            _synth_lines.append(f"*{company}*")
            for ev in evs:
                if ev.user_context:
                    _synth_lines.append(f"- {ev.user_context}")
            _synth_lines.append("")

        skill_graph_work = "\n".join(_synth_lines).strip()

        verified_skills = [
            {"skill": n.skill_name, "status": n.status, "confidence": n.confidence}
            for n in skill_nodes
        ]

        # ── Section order ──────────────────────────────────────────────────────
        section_order = get_section_order(target_role, work_history)
        logger.info("[generator] section_order=%s", section_order)

        # ── Programmatic header (no AI) ────────────────────────────────────────
        contact   = _extract_contact(user.base_resume_markdown)
        is_tech   = _domain_of(target_role) == "tech"
        header_md = _build_header(contact, is_tech)
        logger.info("[generator] header built — name=%r  is_tech=%s", contact.get("name"), is_tech)

        # ── Build base_resume context: candidate content only (template is in system prompt) ─
        base_resume = (
            f"<!-- preferred section order: {', '.join(section_order)} -->\n\n"
            f"## CANDIDATE'S UPLOADED RESUME (use this for actual content — skills, experience, bullets):\n"
            + _compress_resume(user.base_resume_markdown)
            + (
                f"\n\n## ADDITIONAL WORK HISTORY FROM SKILL GRAPH"
                f" (add these to WORK EXPERIENCE even if absent from the uploaded resume):\n"
                + skill_graph_work
                if skill_graph_work else ""
            )
        )

        # ── Call AI ───────────────────────────────────────────────────────────
        fit = (job.parsed or {}).get("_fit", {})

        logger.info(
            "[generator] calling tailor_resume — job=%r  verified_skills=%d  evidence=%d lines",
            job.title, len(verified_skills), len(evidence_lines),
        )

        try:
            raw_output = await ai.tailor_resume(
                job=job.parsed or {"title": job.title, "company": job.company},
                fit=fit,
                base_resume=base_resume,
                verified_skills=verified_skills,
                user_evidence="\n".join(evidence_lines) if evidence_lines else "",
                special_instructions=special_instructions,
            )
        except Exception as e:
            logger.error("[generator] tailor_resume failed: %s", e)
            return None

        # ── Split: resume / cover letter / changes ─────────────────────────────
        resume_md       = raw_output
        cover_letter_md = None

        if "---COVER LETTER---" in raw_output:
            parts         = raw_output.split("---COVER LETTER---", 1)
            resume_md     = parts[0].strip()
            remainder     = parts[1]
            if "---CHANGES---" in remainder:
                cover_letter_md = remainder.split("---CHANGES---", 1)[0].strip()
            else:
                cover_letter_md = remainder.strip()
        elif "---CHANGES---" in resume_md:
            resume_md = resume_md.split("---CHANGES---", 1)[0].strip()

        # ── Prepend programmatic header (AI output starts from first ## section) ─
        # Strip any accidental name/contact line the AI may have included
        body_lines = resume_md.splitlines()
        # Drop leading lines until we hit the first ## section
        while body_lines and not body_lines[0].startswith("## "):
            body_lines.pop(0)
        resume_md = header_md + "\n\n" + "\n".join(body_lines)

        logger.info(
            "[generator] resume=%d chars  cover_letter=%s",
            len(resume_md), "yes" if cover_letter_md else "no",
        )

    # ── Persist in Application (new session to avoid nesting) ────────────────
    async with AsyncSessionLocal() as session:
        async with session.begin():
            app_result = await session.execute(
                select(Application).where(Application.job_id == job_id)
            )
            app = app_result.scalar_one_or_none()

            if not app:
                app = Application(
                    job_id     = job_id,
                    applied_at = datetime.utcnow(),
                )
                session.add(app)
                await session.flush()

            app.resume_markdown       = resume_md
            app.cover_letter_markdown = cover_letter_md

            # Mark job as approved
            job_row = await session.get(Job, job_id)
            if job_row:
                job_row.status = "approved"

    return app


async def generate_cover_letter(
    job_id: str,
    user_id: str,
    ai,
) -> "Application | None":
    """
    Generate a cover letter for an existing Application row and persist it.
    Returns the updated Application, or None on failure.
    """
    from sqlalchemy import select
    from db.models import Application, Job, User
    from db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        job_result = await session.execute(select(Job).where(Job.id == job_id))
        job = job_result.scalar_one_or_none()
        if not job:
            logger.error("[generator] job %s not found for CL generation", job_id)
            return None

        user_result = await session.execute(select(User).where(User.id == user_id))
        user = user_result.scalar_one_or_none()
        if not user:
            return None

        app_result = await session.execute(
            select(Application).where(Application.job_id == job_id)
        )
        app = app_result.scalar_one_or_none()
        if not app:
            logger.error("[generator] no Application row for job %s", job_id)
            return None

    fit = (job.parsed or {}).get("_fit", {})
    user_profile = {
        "name":   user.name or "",
        "skills": [s["skill"] for s in (user.filters or {}).get("verified_skills", [])],
        "role":   (user.filters or {}).get("role", ""),
    }

    try:
        cover_letter_md = await ai.write_cover_letter(
            job=job.parsed or {"title": job.title, "company": job.company},
            user_profile=user_profile,
            fit=fit,
        )
    except Exception as e:
        logger.error("[generator] write_cover_letter failed: %s", e)
        return None

    async with AsyncSessionLocal() as session:
        async with session.begin():
            app_row = await session.get(Application, app.id)
            if app_row:
                app_row.cover_letter_markdown = cover_letter_md

    app.cover_letter_markdown = cover_letter_md
    logger.info("[generator] cover letter generated — %d chars", len(cover_letter_md))
    return app
