"""
Resume generation end-to-end test harness.

Uses Harshit's real DB data (copied to a safe test DB — never touches hireloop.db).
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
from resume.generator import generate_resume, apply_patch, extract_save_hint, save_globally
from resume.docx_export import render_docx

# ── Constants ─────────────────────────────────────────────────────────────────
TOP_N_JOBS = 10   # how many top-scored jobs to show in the menu
AUTO_MODE  = "--all" in sys.argv


# ── User auto-detection ───────────────────────────────────────────────────────

def _pick_user() -> tuple[str, str]:
    """Pick a test user from the DB.

    Returns (user_id, name).  Prefers users that have:
      1. base_resume_markdown set
      2. verified skill nodes
      3. jobs with a fit score

    If multiple candidates qualify, shows a numbered picker.
    Exits with an error if no user is ready.
    """
    import sqlite3, json
    conn = sqlite3.connect(_PROD_DB)
    cur  = conn.cursor()

    cur.execute("SELECT id, name FROM users WHERE base_resume_markdown IS NOT NULL AND base_resume_markdown != ''")
    users_with_resume = cur.fetchall()

    candidates = []
    for uid, name in users_with_resume:
        cur.execute("SELECT COUNT(*) FROM skill_nodes WHERE user_id = ? AND status LIKE 'verified_%'", (uid,))
        skills = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM jobs WHERE user_id = ? AND fit_score IS NOT NULL", (uid,))
        jobs = cur.fetchone()[0]
        if skills > 0 and jobs > 0:
            candidates.append((uid, name, skills, jobs))

    conn.close()

    if not candidates:
        print("[ERROR] No user in the DB is ready to test.")
        print("  A user needs: base resume uploaded + verified skills + at least one scored job.")
        sys.exit(1)

    if len(candidates) == 1:
        uid, name, skills, jobs = candidates[0]
        print(f"  Auto-selected user: {name} ({skills} skills, {jobs} jobs)")
        return uid, name

    # Multiple candidates — show picker
    print("\n  Multiple users available — pick one:")
    for i, (uid, name, skills, jobs) in enumerate(candidates, 1):
        print(f"  [{i}] {name:<20} {skills} skills  {jobs} jobs  ({uid[:8]}...)")
    while True:
        choice = input(f"  Pick [1-{len(candidates)}]: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(candidates):
            uid, name, _, _ = candidates[int(choice) - 1]
            return uid, name
        print("  Invalid — try again.")


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

async def _print_profile(session, user_id: str, user_name: str):
    from sqlalchemy import select, func
    from db.models import User, SkillNode, SkillEvidence

    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
    skill_count = (await session.execute(
        select(func.count()).select_from(SkillNode)
        .where(SkillNode.user_id == user_id, SkillNode.status.like("verified_%"))
    )).scalar()
    ev_count = (await session.execute(
        select(func.count()).select_from(SkillEvidence)
        .join(SkillNode).where(SkillNode.user_id == user_id)
    )).scalar()

    filters = user.filters or {}
    _sep(f"{user_name}'s Profile")
    print(f"  Name              : {user.name}")
    print(f"  Verified skills   : {skill_count}")
    print(f"  Evidence entries  : {ev_count}")
    print(f"  Target role       : {filters.get('role', '(not set)')}")
    print(f"  Work history      : {len(filters.get('work_history', []))} entries")
    instr = filters.get("resume_instructions")
    print(f"  Instructions      : {instr if instr else '(none)'}")
    print(f"  Base resume       : {len(user.base_resume_markdown or '')} chars")


# ── Job menu ──────────────────────────────────────────────────────────────────

async def _pick_jobs(session, user_id: str) -> list[tuple]:
    """Show job menu pulled live from DB, return selected (job_id, title, company, fit_score) tuples."""
    from sqlalchemy import select
    from db.models import Job

    rows = (await session.execute(
        select(Job)
        .where(Job.user_id == user_id, Job.fit_score.isnot(None))
        .order_by(Job.fit_score.desc())
        .limit(TOP_N_JOBS)
    )).scalars().all()

    jobs = [(r.id, r.title or "?", r.company or "?", r.fit_score or 0.0) for r in rows]

    _sep("Available Jobs (top by fit score)")
    for i, (jid, title, company, score) in enumerate(jobs, 1):
        print(f"  [{i:>2}] {title[:45]:<45}  {company[:20]:<20}  fit: {score:.0f}%")
    print(f"  [ A] Run all {len(jobs)}")
    print(f"  [ Q] Quit")

    if AUTO_MODE:
        print("\n  (--all flag: running all jobs)")
        return jobs

    while True:
        choice = input(f"\n  Pick job [1-{len(jobs)}/A/Q]: ").strip().lower()
        if choice == "q":
            return []
        if choice == "a":
            return jobs
        if choice.isdigit() and 1 <= int(choice) <= len(jobs):
            return [jobs[int(choice) - 1]]
        print("  Invalid — try again.")


# ── Core: generate + interactive loop ─────────────────────────────────────────

async def _run_job(job_id: str, title: str, company: str, fit: float, ai: HireLoopAI, user_id: str, user_name: str):
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
            .where(SkillNode.user_id == user_id, SkillNode.status.like("verified_%"))
        )).scalar()

        ev_count = (await s.execute(
            select(func.count()).select_from(SkillEvidence)
            .join(SkillNode).where(SkillNode.user_id == user_id)
        )).scalar()

        from db.models import User
        user = (await s.execute(select(User).where(User.id == user_id))).scalar_one()
        filters = user.filters or {}
        work_history = filters.get("work_history", [])
        target_role = filters.get("role", "")
        section_order = get_section_order(target_role, work_history)

        # Gather evidence notes for patch context
        node_result = await s.execute(
            select(SkillNode).where(
                SkillNode.user_id == user_id,
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
    print(f"  [generate] User         : {user_name} | verified skills: {skill_count} | evidence: {ev_count}")
    print(f"  [generate] Section order: {' → '.join(section_order)}")
    print(f"  [generate] Instructions : {filters.get('resume_instructions') or '(none)'}")
    print(f"  [generate] Calling tailor_resume() via {ai._quality.provider_name} (quality)...")

    t0 = time.monotonic()
    app = await generate_resume(job_id, user_id, ai)
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
            _save_docx(resume_md, job_id, title, user_name)
            return

        print("\n  [L] Looks good    [E] Edit    [S] Skip to next job")
        choice = input("  > ").strip().lower()

        if choice == "l":
            _save_docx(resume_md, job_id, title, user_name)
            return

        elif choice == "e":
            request = input("\n  What to change? > ").strip()
            if not request:
                print("  (empty — skipping edit)")
                continue

            print(f"\n  [patch] Request      : \"{request}\"")
            print(f"  [patch] Sending to {ai._quality.provider_name} — thinking...")

            t0 = time.monotonic()
            try:
                patch_output = await ai.patch_resume(resume_md, request, evidence_notes=evidence_notes)
            except Exception as e:
                print(f"  [patch] ✗ FAILED — {e}")
                continue
            elapsed = time.monotonic() - t0

            patched_sections = _sections_patched(patch_output)
            cannot_apply = 'CANNOT_APPLY' in [s.upper() for s in patched_sections]
            is_reorder    = bool(re.search(r'<reorder>', patch_output, re.IGNORECASE))

            print(f"  [patch] Done in {elapsed:.1f}s\n")

            # ── Show what the AI decided to do ────────────────────────────────
            if cannot_apply:
                reason_m = re.search(r'<section name="CANNOT_APPLY">(.*?)</section>',
                                     patch_output, re.DOTALL | re.IGNORECASE)
                reason = reason_m.group(1).strip() if reason_m else "no reason given"
                print(f"  [AI decision] Cannot apply this edit.")
                print(f"  [AI reason  ] {reason}")
                print(f"  [tip        ] Include the actual content in your request (e.g. 'add activities: CS Society 2022-2024, VP role').")
                continue

            if is_reorder:
                order_m = re.search(r'<reorder>(.*?)</reorder>', patch_output, re.IGNORECASE | re.DOTALL)
                new_order = order_m.group(1).strip() if order_m else "?"
                print(f"  [AI decision] Reorder sections → {new_order}")

            visible = [s for s in patched_sections if s.upper() != 'CANNOT_APPLY']
            if visible:
                print(f"  [AI decision] Edit section(s): {', '.join(visible)}")
                _sep("AI patch output")
                for m in re.finditer(r'<section name="([^"]+)">(.*?)</section>', patch_output, re.DOTALL):
                    sec = m.group(1).strip()
                    if sec.upper() == 'CANNOT_APPLY':
                        continue
                    content = m.group(2).strip()
                    print(f"\n  ▶ {sec}\n")
                    for line in content.splitlines():
                        print(f"    {line}")
                _sep()

            before_len = len(resume_md)
            resume_md  = apply_patch(resume_md, patch_output)
            after_len  = len(resume_md)
            delta      = after_len - before_len
            print(f"\n  [applied] Resume length: {before_len} → {after_len} chars  "
                  f"({'Δ+' + str(delta) if delta >= 0 else 'Δ' + str(delta)})")

            # ── Save globally? (only if AI detected a static fact change) ────────
            hint = extract_save_hint(patch_output)
            if hint:
                print(f"\n  [💾] Static fact detected: \"{hint}\"")
                print(f"  Save globally so future resumes use it automatically?")
                print(f"  [D] Dates/names only  [A] All (incl. role title)  [N] No")
                save_choice = input("  > ").strip().lower()
                if save_choice in ("d", "a"):
                    include_roles = save_choice == "a"
                    await save_globally(user_id, patch_output, resume_md, include_role_titles=include_roles)
                    print(f"  [✓] Saved globally{'  (including role title)' if include_roles else ''}")

        elif choice == "s":
            print("  Skipping.")
            return

        else:
            print("  Invalid — try L, E, or S.")


def _save_docx(resume_md: str, job_id: str, title: str, user_name: str):
    slug = re.sub(r"[^\w]+", "_", title.lower())[:30]
    name_slug = re.sub(r"[^\w]+", "_", user_name.lower())[:12]
    out_path = Path("resume/output") / f"{name_slug}_test_{slug}_{job_id[:6]}.docx"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    render_docx(resume_md, out_path)
    size_kb = out_path.stat().st_size / 1024
    print(f"\n  [docx] ✓ Saved → {out_path}  ({size_kb:.1f} KB)")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    _sep("HireLoop — Resume Generation E2E Test", char="═", width=60)

    user_id, user_name = _pick_user()

    fast    = AIFactory.create_fast()
    quality = AIFactory.create_quality()
    ai      = HireLoopAI(fast_provider=fast, quality_provider=quality)

    print(f"\n  Fast    provider : {fast.provider_name}")
    print(f"  Quality provider : {quality.provider_name}")
    print(f"  Test DB          : {_TEST_DB}  (prod DB untouched)")

    from db.session import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        await _print_profile(session, user_id, user_name)
        selected_jobs = await _pick_jobs(session, user_id)

    if not selected_jobs:
        print("\n  Nothing to run. Exiting.")
    else:
        for job_id, title, company, fit in selected_jobs:
            await _run_job(job_id, title, company, fit, ai, user_id, user_name)

    print(f"\n  [info] Test DB kept at {_TEST_DB} — delete manually when done testing.")
    _sep("Done", char="═")


if __name__ == "__main__":
    asyncio.run(main())
