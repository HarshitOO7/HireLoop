"""
Job approval and resume delivery handler.

Entry points:
  start_resume_generation(job_id, user_id, msg, ai)
    ← called by skill_verify._finish_verification (after gap skills verified)
    ← called by skill_verify.job_skills_start (when job has no skill gaps)

Flow:
  1. Send "Generating your resume..." message
  2. Call resume.generator.generate_resume()
  3. Edit message → "✅ Resume ready! Choose format:"
  4. Show delivery keyboard [📄 Word] [📋 PDF] [📦 Both] [⏭ Skip]

Delivery callbacks:
  deliver_docx_{job_id}  → send .docx file
  deliver_pdf_{job_id}   → send .pdf
  deliver_both_{job_id}  → send both
  (skip_job_{job_id} is handled by skill_verify.get_job_card_handlers)
"""

import io
import logging
import tempfile
from pathlib import Path

from telegram import Update
from telegram.ext import CallbackQueryHandler, ContextTypes

from db.models import Application, Job
from db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)


# ── DB helpers ────────────────────────────────────────────────────────────────

async def _load_job(job_id: str) -> Job | None:
    from sqlalchemy import select
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Job).where(Job.id == job_id))
        return result.scalar_one_or_none()


async def _load_app(job_id: str) -> Application | None:
    from sqlalchemy import select
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Application).where(Application.job_id == job_id)
        )
        return result.scalar_one_or_none()


# ── File builders ─────────────────────────────────────────────────────────────

def _safe_name(text: str) -> str:
    return (text or "resume").replace(" ", "_").replace("/", "_")[:40]


async def _send_docx(chat_id: int, job: Job, app: Application, bot) -> None:
    from resume.docx_export import render_docx
    filename = f"{_safe_name(job.company)}_{_safe_name(job.title)}.docx"
    with tempfile.TemporaryDirectory() as tmp:
        path = render_docx(app.resume_markdown, Path(tmp) / filename)
        with open(path, "rb") as f:
            await bot.send_document(chat_id=chat_id, document=f, filename=filename,
                                    caption=f"📄 Tailored resume — {job.title} @ {job.company}")


async def _send_pdf(chat_id: int, job: Job, app: Application, bot) -> None:
    from resume.pdf_export import render_pdf
    filename = f"{_safe_name(job.company)}_{_safe_name(job.title)}.pdf"
    with tempfile.TemporaryDirectory() as tmp:
        path = render_pdf(app.resume_markdown, output_path=str(Path(tmp) / filename))
        with open(path, "rb") as f:
            await bot.send_document(chat_id=chat_id, document=f, filename=filename,
                                    caption=f"📋 Tailored resume (PDF) — {job.title} @ {job.company}")


async def _send_cover_letter(chat_id: int, job: Job, app: Application, bot) -> None:
    filename = f"cover_letter_{_safe_name(job.company)}.txt"
    content  = (app.cover_letter_markdown or "").encode("utf-8")
    await bot.send_document(
        chat_id  = chat_id,
        document = io.BytesIO(content),
        filename = filename,
        caption  = "📝 Cover letter",
    )


# ── Generation entry point ────────────────────────────────────────────────────

async def start_resume_generation(
    job_id:  str,
    user_id: str,
    msg,              # telegram.Message to reply to
    ai,               # HireLoopAI instance
) -> None:
    """
    Generate resume for job_id and prompt user to choose a delivery format.
    Called from skill_verify after all skill gaps are resolved (or when none exist).
    """
    from resume.generator import generate_resume
    from bot.keyboards import resume_delivery_keyboard

    thinking = await msg.reply_text(
        "Generating your tailored resume — this takes ~30 seconds..."
    )

    try:
        app = await generate_resume(job_id, user_id, ai)
    except Exception as e:
        logger.error("[job_approval] generate_resume raised: %s", e, exc_info=True)
        await thinking.edit_text(
            "Resume generation failed. Your skill data is saved — try again via 📋 Pending Jobs."
        )
        return

    if not app or not app.resume_markdown:
        await thinking.edit_text(
            "Couldn't generate resume. Make sure you've uploaded a base resume first (📎 Add Resume)."
        )
        return

    job = await _load_job(job_id)
    title   = job.title   if job else "the job"
    company = job.company if job else ""

    cl_note = "\n📝 Cover letter also ready — will be sent with the resume." if app.cover_letter_markdown else ""

    def _md(t) -> str:
        return (str(t) if t else "").replace("\\", "\\\\").replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")

    await thinking.edit_text(
        f"✅ Resume ready for *{_md(title)}*{f' @ {_md(company)}' if company else ''}!\n\n"
        f"Pick a format:{cl_note}",
        parse_mode="Markdown",
        reply_markup=resume_delivery_keyboard(job_id),
    )


# ── Delivery callbacks ────────────────────────────────────────────────────────

async def deliver_docx(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query  = update.callback_query
    await query.answer()
    job_id = query.data.split("deliver_docx_", 1)[1]

    job = await _load_job(job_id)
    app = await _load_app(job_id)
    if not app or not app.resume_markdown:
        await query.edit_message_text("Resume not found — please regenerate via 📋 Pending Jobs.")
        return

    await query.edit_message_text("Sending your Word file...")
    try:
        await _send_docx(query.message.chat_id, job, app, context.bot)
        if app.cover_letter_markdown:
            await _send_cover_letter(query.message.chat_id, job, app, context.bot)
        await query.message.reply_text("Done! Good luck with the application 🍀")
    except Exception as e:
        logger.error("[job_approval] docx delivery failed: %s", e)
        await query.message.reply_text("File send failed — try again.")


async def deliver_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query  = update.callback_query
    await query.answer()
    job_id = query.data.split("deliver_pdf_", 1)[1]

    job = await _load_job(job_id)
    app = await _load_app(job_id)
    if not app or not app.resume_markdown:
        await query.edit_message_text("Resume not found — please regenerate via 📋 Pending Jobs.")
        return

    await query.edit_message_text("Sending your PDF...")
    try:
        await _send_pdf(query.message.chat_id, job, app, context.bot)
        if app.cover_letter_markdown:
            await _send_cover_letter(query.message.chat_id, job, app, context.bot)
        await query.message.reply_text("Done! Good luck with the application 🍀")
    except Exception as e:
        logger.error("[job_approval] pdf delivery failed: %s", e)
        await query.message.reply_text("File send failed — try again.")


async def deliver_both(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query  = update.callback_query
    await query.answer()
    job_id = query.data.split("deliver_both_", 1)[1]

    job = await _load_job(job_id)
    app = await _load_app(job_id)
    if not app or not app.resume_markdown:
        await query.edit_message_text("Resume not found — please regenerate via 📋 Pending Jobs.")
        return

    await query.edit_message_text("Sending Word + PDF...")
    try:
        await _send_docx(query.message.chat_id, job, app, context.bot)
        await _send_pdf(query.message.chat_id, job, app, context.bot)
        if app.cover_letter_markdown:
            await _send_cover_letter(query.message.chat_id, job, app, context.bot)
        await query.message.reply_text("Done! Good luck with the application 🍀")
    except Exception as e:
        logger.error("[job_approval] both delivery failed: %s", e)
        await query.message.reply_text("File send failed — try again.")


# ── Handler list for main.py ──────────────────────────────────────────────────

def get_job_approval_handlers() -> list:
    return [
        CallbackQueryHandler(deliver_docx, pattern=r"^deliver_docx_"),
        CallbackQueryHandler(deliver_pdf,  pattern=r"^deliver_pdf_"),
        CallbackQueryHandler(deliver_both, pattern=r"^deliver_both_"),
    ]
