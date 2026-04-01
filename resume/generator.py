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

# ── Contact extraction ─────────────────────────────────────────────────────────

_RE_EMAIL    = re.compile(r"[\w.+-]+@[\w-]+\.[a-z]{2,}", re.IGNORECASE)
_RE_PHONE    = re.compile(r"[\+]?[\d][\d\s\-\(\)]{6,14}[\d]")
_RE_LINKEDIN = re.compile(r"linkedin\.com/in/[\w\-]+", re.IGNORECASE)
_RE_GITHUB   = re.compile(r"github\.com/[\w\-]+", re.IGNORECASE)


def _extract_contact(raw_text: str) -> dict:
    """
    Scan raw resume text for name and contact fields.
    Returns dict with keys: name, phone, email, linkedin_url, github_url.
    Any field may be None if not found.
    """
    lines = [l.strip() for l in raw_text.splitlines() if l.strip()]
    # Name: first non-empty line, strip markdown # prefix
    name = lines[0].lstrip("# ").strip() if lines else None

    email        = m.group(0) if (m := _RE_EMAIL.search(raw_text))    else None
    phone        = m.group(0).strip() if (m := _RE_PHONE.search(raw_text)) else None
    linkedin_url = m.group(0) if (m := _RE_LINKEDIN.search(raw_text)) else None
    github_url   = m.group(0) if (m := _RE_GITHUB.search(raw_text))   else None

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

        filters      = user.filters or {}
        work_history = filters.get("work_history", [])
        target_role  = filters.get("role", "")

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

        # ── Build base_resume context: template format + candidate content ─────
        _template_path = Path(__file__).parent / "variants" / "base_template.md"
        template_text  = _template_path.read_text(encoding="utf-8")
        base_resume = (
            f"<!-- preferred section order: {', '.join(section_order)} -->\n\n"
            f"## FORMAT TEMPLATE (structure and section names only — do NOT copy this content):\n"
            f"{template_text}\n\n"
            f"## CANDIDATE'S UPLOADED RESUME (use this for actual content — skills, experience, bullets):\n"
            + user.base_resume_markdown
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
