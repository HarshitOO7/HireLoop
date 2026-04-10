"""
Resume generation end-to-end test harness.

Uses Aman's real DB data (copied to a safe test DB — never touches hireloop.db).
Runs the full prod pipeline: generate_resume → interactive approve/edit loop → render_docx.

Usage:
  python scripts/test_resume_generation.py           # interactive job picker
  python scripts/test_resume_generation.py --all     # auto-run top 3 jobs, skip interaction

Run from project root.
"""

# ── Venv auto-detection — MUST be first ─────────────────────────────────────
import os
import sys

def _ensure_venv():
    if sys.prefix != sys.base_prefix:
        return  # already inside a venv
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = [
        os.path.join(root, "venv",  "Scripts", "python.exe"),  # Windows
        os.path.join(root, ".venv", "Scripts", "python.exe"),  # Windows alt
        os.path.join(root, "venv",  "bin", "python"),          # Unix
        os.path.join(root, ".venv", "bin", "python"),          # Unix alt
    ]
    for py in candidates:
        if os.path.exists(py):
            import subprocess
            result = subprocess.run([py] + sys.argv)
            sys.exit(result.returncode)
    print("[ERROR] Not in a virtual environment and no venv/ found.")
    print("  Create one first:  python -m venv venv")
    print("  Then either activate it or just re-run — this script auto-detects it.")
    sys.exit(1)

_ensure_venv()

# ── DB isolation — MUST come before any db.* imports ────────────────────────
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_PROD_DB = "hireloop.db"
_TEST_DB = "hireloop_test.db"

if not os.path.exists(_PROD_DB):
    print(f"[ERROR] {_PROD_DB} not found — run from project root.")
    sys.exit(1)

shutil.copy2(_PROD_DB, _TEST_DB)
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TEST_DB}"

# ── Now safe to import project modules ───────────────────────────────────────
import asyncio
import re
import time
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from ai.factory import AIFactory
from ai.service import HireLoopAI
from resume.generator import generate_resume, apply_patch
from resume.docx_export import render_docx

# ── Constants ─────────────────────────────────────────────────────────────────
AMAN_USER_ID = "c80cdddc-f3a4-47a0-98e1-867f16b2df0d"

# Pre-selected jobs with good fit scores — all belong to Aman
DEFAULT_JOBS = [
    ("26dbf811-44ac-40fa-bf32-ff21bc7cca65", "Intern, Full Stack Developer", "Autodesk",          78.0),
    ("a5ed28b7-afcb-4178-8269-5f073707d61c", "Junior Full Stack Developer",  "Armour Payments",   74.0),
    ("fc4088c2-eb4d-4ee9-8f42-fb472d6d4426", "UI / Front-End Developer",     "FLiiP",             72.0),
]

AUTO_MODE = "--all" in sys.argv


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sep(title: str = "", char: str = "─", width: int = 60):
    if title:
        pad = max(0, width - len(title) - 2)
        print(f"\n{char * 2} {title} {char * pad}")
    else:
        print(char * width)


def _sections_in(md: str) -> list[str]:
    """Return all ## section headings found in markdown."""
    return [m.group(1).strip() for m in re.finditer(r"^## (.+)", md, re.MULTILINE)]


def _sections_patched(patch_output: str) -> list[str]:
    """Extract section names from <section name="..."> tags in patch output."""
    return re.findall(r'<section name="([^"]+)">', patch_output, re.IGNORECASE)


def _print_resume(md: str):
    _sep("Resume Output")
    print(md)
    _sep()


# ── Profile summary ───────────────────────────────────────────────────────────

async def _print_profile(session):
    from sqlalchemy import select, func
    from db.models import User, SkillNode, SkillEvidence

    user = (await session.execute(select(User).where(User.id == AMAN_USER_ID))).scalar_one()
    skill_count = (await session.execute(
        select(func.count()).select_from(SkillNode)
        .where(SkillNode.user_id == AMAN_USER_ID, SkillNode.status.like("verified_%"))
    )).scalar()
    ev_count = (await session.execute(
        select(func.count()).select_from(SkillEvidence)
        .join(SkillNode).where(SkillNode.user_id == AMAN_USER_ID)
    )).scalar()

    filters = user.filters or {}
    _sep("Aman's Profile")
    print(f"  Name              : {user.name}")
    print(f"  Verified skills   : {skill_count}")
    print(f"  Evidence entries  : {ev_count}")
    print(f"  Target role       : {filters.get('role', '(not set)')}")
    print(f"  Work history      : {len(filters.get('work_history', []))} entries")
    instr = filters.get("resume_instructions")
    print(f"  Instructions      : {instr if instr else '(none)'}")
    print(f"  Base resume       : {len(user.base_resume_markdown or '')} chars")


# ── Job menu ──────────────────────────────────────────────────────────────────

async def _pick_jobs(session) -> list[tuple]:
    """Show job menu and return selected (job_id, title, company, fit_score) tuples."""
    from sqlalchemy import select
    from db.models import Job

    # Load live data for the pre-selected jobs
    jobs = []
    for job_id, _, _, _ in DEFAULT_JOBS:
        row = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
        if row:
            jobs.append((row.id, row.title, row.company, row.fit_score or 0.0))

    _sep("Available Jobs")
    for i, (jid, title, company, score) in enumerate(jobs, 1):
        print(f"  [{i}] {title} @ {company}  —  fit: {score:.0f}%")
    print(f"  [A] Run all {len(jobs)}")
    print(f"  [Q] Quit")

    if AUTO_MODE:
        print("\n  (--all flag: running all jobs)")
        return jobs

    while True:
        choice = input("\n  Pick job(s) [1/2/3/A/Q]: ").strip().lower()
        if choice == "q":
            return []
        if choice == "a":
            return jobs
        if choice.isdigit() and 1 <= int(choice) <= len(jobs):
            return [jobs[int(choice) - 1]]
        print("  Invalid — try again.")


# ── Core: generate + interactive loop ─────────────────────────────────────────

async def _run_job(job_id: str, title: str, company: str, fit: float, ai: HireLoopAI):
    _sep(f"Job: {title} @ {company}  ({fit:.0f}%)", char="═")

    # ── Pre-generation log ────────────────────────────────────────────────────
    from sqlalchemy import select, func
    from db.models import Job, SkillNode, SkillEvidence
    from db.session import AsyncSessionLocal
    from resume.section_order import get_section_order

    evidence_notes = ""
    async with AsyncSessionLocal() as s:
        job_row = (await s.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
        if not job_row:
            print(f"  [ERROR] Job {job_id} not found in test DB.")
            return

        skill_count = (await s.execute(
            select(func.count()).select_from(SkillNode)
            .where(SkillNode.user_id == AMAN_USER_ID, SkillNode.status.like("verified_%"))
        )).scalar()

        ev_count = (await s.execute(
            select(func.count()).select_from(SkillEvidence)
            .join(SkillNode).where(SkillNode.user_id == AMAN_USER_ID)
        )).scalar()

        from db.models import User
        user = (await s.execute(select(User).where(User.id == AMAN_USER_ID))).scalar_one()
        filters = user.filters or {}
        work_history = filters.get("work_history", [])
        target_role = filters.get("role", "")
        section_order = get_section_order(target_role, work_history)

        # Gather evidence notes for patch context
        node_result = await s.execute(
            select(SkillNode).where(
                SkillNode.user_id == AMAN_USER_ID,
                SkillNode.status.like("verified_%"),
            )
        )
        nodes = {n.id: n for n in node_result.scalars().all()}
        ev_result = await s.execute(
            select(SkillEvidence).where(
                SkillEvidence.skill_node_id.in_(list(nodes.keys()))
            )
        )
        ev_lines = []
        for ev in ev_result.scalars().all():
            if not ev.user_context:
                continue
            node = nodes.get(ev.skill_node_id)
            skill_name = node.skill_name if node else "?"
            note = f"• {skill_name}: {ev.user_context}"
            if ev.company:
                note += f" (@ {ev.company}"
                if ev.duration_months:
                    note += f", {ev.duration_months}m"
                note += ")"
            ev_lines.append(note)
        evidence_notes = "\n".join(ev_lines)

    print(f"\n  [generate] Job          : \"{title}\" @ {company}  fit={fit:.0f}%")
    print(f"  [generate] User         : Aman | verified skills: {skill_count} | evidence: {ev_count}")
    print(f"  [generate] Section order: {' → '.join(section_order)}")
    print(f"  [generate] Instructions : {filters.get('resume_instructions') or '(none)'}")
    print(f"  [generate] Calling tailor_resume() via {ai._quality.provider_name} (quality)...")

    t0 = time.monotonic()
    app = await generate_resume(job_id, AMAN_USER_ID, ai)
    elapsed = time.monotonic() - t0

    if not app or not app.resume_markdown:
        print(f"  [generate] ✗ FAILED — generate_resume() returned None or empty markdown")
        return

    resume_md = app.resume_markdown
    cl_md = app.cover_letter_markdown

    sections = _sections_in(resume_md)
    print(f"  [generate] ✓ Done in {elapsed:.1f}s — resume: {len(resume_md)} chars | "
          f"cover letter: {'yes (' + str(len(cl_md)) + ' chars)' if cl_md else 'no'}")
    print(f"  [generate] Sections     : {', '.join(sections)}")

    # ── Interactive loop ──────────────────────────────────────────────────────
    while True:
        _print_resume(resume_md)
        if cl_md:
            _sep("Cover Letter")
            print(cl_md)
            _sep()

        if AUTO_MODE:
            # Non-interactive: just save the DOCX and move on
            _save_docx(resume_md, job_id, title)
            return

        print("\n  [L] Looks good    [E] Edit    [S] Skip to next job")
        choice = input("  > ").strip().lower()

        if choice == "l":
            _save_docx(resume_md, job_id, title)
            return

        elif choice == "e":
            request = input("\n  What to change? > ").strip()
            if not request:
                print("  (empty — skipping edit)")
                continue

            print(f"\n  [patch] User request : \"{request}\"")
            print(f"  [patch] Calling patch_resume() via {ai._quality.provider_name} (quality)...")

            t0 = time.monotonic()
            try:
                patch_output = await ai.patch_resume(resume_md, request, evidence_notes=evidence_notes)
            except Exception as e:
                print(f"  [patch] ✗ FAILED — {e}")
                continue
            elapsed = time.monotonic() - t0

            patched_sections = _sections_patched(patch_output)
            before_len = len(resume_md)
            resume_md = apply_patch(resume_md, patch_output)
            after_len = len(resume_md)
            delta = after_len - before_len

            print(f"  [patch] ✓ Done in {elapsed:.1f}s — patch output: {len(patch_output)} chars")
            if not patched_sections:
                print(f"  [patch] Raw output        : {patch_output.strip()}")
            print(f"  [patch] Sections modified : {', '.join(patched_sections) if patched_sections else '(none detected)'}")
            print(f"  [patch] Resume length     : {before_len} → {after_len} chars  "
                  f"({'Δ+' + str(delta) if delta >= 0 else 'Δ' + str(delta)})")

        elif choice == "s":
            print("  Skipping.")
            return

        else:
            print("  Invalid — try L, E, or S.")


def _save_docx(resume_md: str, job_id: str, title: str):
    slug = re.sub(r"[^\w]+", "_", title.lower())[:30]
    out_path = Path("resume/output") / f"aman_test_{slug}_{job_id[:6]}.docx"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    render_docx(resume_md, out_path)
    size_kb = out_path.stat().st_size / 1024
    print(f"\n  [docx] ✓ Saved → {out_path}  ({size_kb:.1f} KB)")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    _sep("HireLoop — Resume Generation E2E Test", char="═", width=60)

    fast    = AIFactory.create_fast()
    quality = AIFactory.create_quality()
    ai      = HireLoopAI(fast_provider=fast, quality_provider=quality)

    print(f"\n  Fast    provider : {fast.provider_name}")
    print(f"  Quality provider : {quality.provider_name}")
    print(f"  Test DB          : {_TEST_DB}  (prod DB untouched)")

    from db.session import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        await _print_profile(session)
        selected_jobs = await _pick_jobs(session)

    if not selected_jobs:
        print("\n  Nothing to run. Exiting.")
    else:
        for job_id, title, company, fit in selected_jobs:
            await _run_job(job_id, title, company, fit, ai)

    print(f"\n  [info] Test DB kept at {_TEST_DB} — delete manually when done testing.")
    _sep("Done", char="═")


if __name__ == "__main__":
    asyncio.run(main())
