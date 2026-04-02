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

Post-delivery edit loop:
  edit_resume_{job_id}   → ConversationHandler entry — ask what to change
  edit_done_{job_id}     → mark approved, send next card

My Applications:
  cmd_my_applications / "📁 My Applications" → list last 10 applications
  app_docx_{app_id} / app_pdf_{app_id} / app_cl_{app_id} → re-send files
"""

import io
import logging
import tempfile
from datetime import datetime
from pathlib import Path

from sqlalchemy import update as sa_update
from telegram import Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot.keyboards import (
    application_card_keyboard,
    cover_letter_ask_keyboard,
    post_delivery_keyboard,
    resume_delivery_keyboard,
)
from db.models import Application, Job
from db.session import AsyncSessionLocal
from jobs.scheduler import send_next_pending_card

logger = logging.getLogger(__name__)

EDIT_AWAITING_REQUEST = 10  # ConversationHandler state


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


async def _load_app_by_id(app_id: int) -> Application | None:
    from sqlalchemy import select
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Application).where(Application.id == app_id)
        )
        return result.scalar_one_or_none()


async def _mark_job_approved(job_id: str) -> None:
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(
                sa_update(Job).where(Job.id == job_id).values(status="approved")
            )


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

def _md(t: object) -> str:
    return (str(t) if t else "").replace("\\", "\\\\").replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")


async def start_resume_generation(
    job_id:  str,
    user_id: str,
    msg,              # telegram.Message to reply to
    ai,               # HireLoopAI instance
) -> None:
    """
    Generate resume for job_id, then ask about cover letter before showing delivery options.
    Called from skill_verify after all skill gaps are resolved (or when none exist).
    """
    from resume.generator import generate_resume

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
    title    = job.title   if job else "the job"
    company  = job.company if job else ""
    required = bool(job and job.cover_letter_required)

    cl_prompt = (
        "📝 This posting requires a cover letter. Want me to write one?"
        if required else
        "Want to add a cover letter? (not required, but can help)"
    )

    await thinking.edit_text(
        f"✅ Resume ready for *{_md(title)}*{f' @ {_md(company)}' if company else ''}!\n\n"
        f"{cl_prompt}",
        parse_mode="Markdown",
        reply_markup=cover_letter_ask_keyboard(job_id),
    )


# ── Cover letter prompt callbacks ────────────────────────────────────────────

async def cl_yes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query  = update.callback_query
    await query.answer()
    job_id = query.data.split("cl_yes_", 1)[1]
    ai     = context.bot_data.get("ai")

    # Look up user_id from telegram_id (context.user_data["db_user_id"] is never set)
    from sqlalchemy import select
    from db.models import User
    tg_id = str(update.effective_user.id)
    async with AsyncSessionLocal() as session:
        u = await session.execute(select(User).where(User.telegram_id == tg_id))
        user_obj = u.scalar_one_or_none()
    user_id = user_obj.id if user_obj else None

    await query.edit_message_text("Writing your cover letter...")

    from resume.generator import generate_cover_letter
    try:
        app = await generate_cover_letter(job_id, user_id, ai)
    except Exception as e:
        logger.error("[job_approval] generate_cover_letter raised: %s", e)
        app = None

    job = await _load_job(job_id)
    title   = job.title   if job else "the job"
    company = job.company if job else ""
    cl_note = (
        "\n📝 Cover letter ready — will be sent with the resume."
        if (app and app.cover_letter_markdown) else
        "\n⚠️ Cover letter generation failed — proceeding without one."
    )

    await query.edit_message_text(
        f"✅ Resume ready for *{_md(title)}*{f' @ {_md(company)}' if company else ''}!\n\n"
        f"Pick a format:{cl_note}",
        parse_mode="Markdown",
        reply_markup=resume_delivery_keyboard(job_id),
    )


async def cl_no(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query  = update.callback_query
    await query.answer()
    job_id = query.data.split("cl_no_", 1)[1]

    job = await _load_job(job_id)
    title   = job.title   if job else "the job"
    company = job.company if job else ""

    await query.edit_message_text(
        f"✅ Resume ready for *{_md(title)}*{f' @ {_md(company)}' if company else ''}!\n\n"
        "Pick a format:",
        parse_mode="Markdown",
        reply_markup=resume_delivery_keyboard(job_id),
    )


# ── Shared post-delivery helper ───────────────────────────────────────────────

async def _finish_delivery(chat_id: int, job: Job | None, job_id: str, formats: set, context) -> None:
    """Send done message with post-delivery keyboard (edit / looks good / apply)."""
    url = job.url if job else None
    await context.bot.send_message(
        chat_id=chat_id,
        text="Done! Good luck 🍀  Need any edits?",
        reply_markup=post_delivery_keyboard(job_id, url),
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
        context.user_data["edit_job_id"]   = job_id
        context.user_data["edit_app_id"]   = app.id
        context.user_data["edit_formats"]  = {"docx"}
        await _finish_delivery(query.message.chat_id, job, job_id, {"docx"}, context)
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
        context.user_data["edit_job_id"]   = job_id
        context.user_data["edit_app_id"]   = app.id
        context.user_data["edit_formats"]  = {"pdf"}
        await _finish_delivery(query.message.chat_id, job, job_id, {"pdf"}, context)
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
        context.user_data["edit_job_id"]   = job_id
        context.user_data["edit_app_id"]   = app.id
        context.user_data["edit_formats"]  = {"docx", "pdf"}
        await _finish_delivery(query.message.chat_id, job, job_id, {"docx", "pdf"}, context)
    except Exception as e:
        logger.error("[job_approval] both delivery failed: %s", e)
        await query.message.reply_text("File send failed — try again.")


# ── Post-delivery edit loop ───────────────────────────────────────────────────

async def edit_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User tapped Looks good — mark approved and send next card."""
    query  = update.callback_query
    await query.answer()
    job_id = query.data.split("edit_done_", 1)[1]
    await query.edit_message_text("Great! Moving on...")
    await _mark_job_approved(job_id)
    context.user_data.pop("edit_job_id",  None)
    context.user_data.pop("edit_app_id",  None)
    context.user_data.pop("edit_formats", None)
    await send_next_pending_card(str(update.effective_user.id), context.bot)
    return ConversationHandler.END


async def edit_resume_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry: user tapped ✏️ Edit resume — ask what to change."""
    query  = update.callback_query
    await query.answer()
    job_id = query.data.split("edit_resume_", 1)[1]
    context.user_data["edit_job_id"] = job_id
    await query.message.reply_text(
        "What needs changing? Describe in plain English.\n\n"
        "_Example: Rewrite the SUMMARY to focus more on backend experience_\n\n"
        "Or type `cancel` to go back.",
        parse_mode="Markdown",
    )
    return EDIT_AWAITING_REQUEST


async def edit_resume_apply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User typed their edit request — call AI patch and re-send files."""
    text = update.message.text.strip()
    if text.lower() == "cancel":
        await update.message.reply_text("Edit cancelled.")
        return ConversationHandler.END

    job_id  = context.user_data.get("edit_job_id")
    app_id  = context.user_data.get("edit_app_id")
    formats = context.user_data.get("edit_formats", {"docx"})
    ai      = context.application.bot_data["ai"]

    if not job_id or not app_id:
        await update.message.reply_text("Session expired — regenerate via 📋 Pending Jobs.")
        return ConversationHandler.END

    thinking = await update.message.reply_text("Applying your edit...")

    # Load current resume markdown
    app = await _load_app_by_id(app_id)
    if not app or not app.resume_markdown:
        await thinking.edit_text("Resume not found. Regenerate via 📋 Pending Jobs.")
        return ConversationHandler.END

    try:
        patch_output = await ai.patch_resume(app.resume_markdown, text)
    except Exception as e:
        logger.error("[job_approval] patch_resume failed: %s", e)
        await thinking.edit_text("Edit failed — try rephrasing or try again.")
        return EDIT_AWAITING_REQUEST

    from resume.generator import apply_patch
    updated_md = apply_patch(app.resume_markdown, patch_output)

    # Persist updated markdown
    async with AsyncSessionLocal() as session:
        async with session.begin():
            app_row = await session.get(Application, app_id)
            if app_row:
                app_row.resume_markdown = updated_md

    app.resume_markdown = updated_md

    # Re-send file(s)
    job = await _load_job(job_id)
    try:
        if "docx" in formats:
            await _send_docx(update.message.chat_id, job, app, context.bot)
        if "pdf" in formats:
            await _send_pdf(update.message.chat_id, job, app, context.bot)
    except Exception as e:
        logger.error("[job_approval] re-send after patch failed: %s", e)
        await thinking.edit_text("Edit saved but file send failed — try again.")
        return EDIT_AWAITING_REQUEST

    url = job.url if job else None
    await thinking.edit_text(
        "Updated! Anything else?",
        reply_markup=post_delivery_keyboard(job_id, url),
    )
    return EDIT_AWAITING_REQUEST


# ── My Applications ───────────────────────────────────────────────────────────

async def cmd_my_applications(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show last 10 applications with re-download buttons."""
    from sqlalchemy import select
    from db.models import User

    tg_id = str(update.effective_user.id)

    async with AsyncSessionLocal() as session:
        user_result = await session.execute(
            select(User).where(User.telegram_id == tg_id)
        )
        user = user_result.scalar_one_or_none()
        if not user:
            await update.message.reply_text("Run /start first to set up your profile.")
            return

        apps_result = await session.execute(
            select(Application, Job)
            .join(Job, Application.job_id == Job.id)
            .where(Job.user_id == user.id)
            .order_by(Application.applied_at.desc())
            .limit(10)
        )
        rows = apps_result.all()

    if not rows:
        await update.message.reply_text(
            "No applications yet — generate your first resume from a pending job card."
        )
        return

    await update.message.reply_text(
        f"📁 *Your last {len(rows)} application{'s' if len(rows) != 1 else ''}:*",
        parse_mode="Markdown",
    )

    for app, job in rows:
        fit_score = job.fit_score or (job.parsed or {}).get("_fit", {}).get("fit_score", 0)
        applied_str = app.applied_at.strftime("%b %d, %Y") if app.applied_at else "—"
        text = (
            f"📄 *{_md(job.title)}* @ {_md(job.company)}\n"
            f"Applied: {applied_str}  |  Fit: {int(fit_score or 0)}%"
        )
        kb = application_card_keyboard(
            app_id=app.id,
            job_id=job.id,
            job_url=job.url,
            has_cl=bool(app.cover_letter_markdown),
        )
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)


async def app_docx(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query  = update.callback_query
    await query.answer()
    app_id = int(query.data.split("app_docx_", 1)[1])
    app = await _load_app_by_id(app_id)
    if not app or not app.resume_markdown:
        await query.message.reply_text("Resume not found.")
        return
    job = await _load_job(app.job_id)
    try:
        await _send_docx(query.message.chat_id, job, app, context.bot)
    except Exception as e:
        logger.error("[job_approval] app_docx failed: %s", e)
        await query.message.reply_text("Send failed — try again.")


async def app_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query  = update.callback_query
    await query.answer()
    app_id = int(query.data.split("app_pdf_", 1)[1])
    app = await _load_app_by_id(app_id)
    if not app or not app.resume_markdown:
        await query.message.reply_text("Resume not found.")
        return
    job = await _load_job(app.job_id)
    try:
        await _send_pdf(query.message.chat_id, job, app, context.bot)
    except Exception as e:
        logger.error("[job_approval] app_pdf failed: %s", e)
        await query.message.reply_text("Send failed — try again.")


async def app_cl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query  = update.callback_query
    await query.answer()
    app_id = int(query.data.split("app_cl_", 1)[1])
    app = await _load_app_by_id(app_id)
    if not app or not app.cover_letter_markdown:
        await query.message.reply_text("Cover letter not found.")
        return
    job = await _load_job(app.job_id)
    try:
        await _send_cover_letter(query.message.chat_id, job, app, context.bot)
    except Exception as e:
        logger.error("[job_approval] app_cl failed: %s", e)
        await query.message.reply_text("Send failed — try again.")


# ── Handler builders ──────────────────────────────────────────────────────────

def get_job_approval_handlers() -> list:
    return [
        CallbackQueryHandler(cl_yes,       pattern=r"^cl_yes_"),
        CallbackQueryHandler(cl_no,        pattern=r"^cl_no_"),
        CallbackQueryHandler(deliver_docx, pattern=r"^deliver_docx_"),
        CallbackQueryHandler(deliver_pdf,  pattern=r"^deliver_pdf_"),
        CallbackQueryHandler(deliver_both, pattern=r"^deliver_both_"),
        CallbackQueryHandler(edit_done,    pattern=r"^edit_done_"),
        # app history re-downloads
        CallbackQueryHandler(app_docx,     pattern=r"^app_docx_"),
        CallbackQueryHandler(app_pdf,      pattern=r"^app_pdf_"),
        CallbackQueryHandler(app_cl,       pattern=r"^app_cl_"),
    ]


def build_resume_edit_handler() -> ConversationHandler:
    """ConversationHandler for the post-delivery edit loop."""
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(edit_resume_start, pattern=r"^edit_resume_"),
        ],
        states={
            EDIT_AWAITING_REQUEST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_resume_apply),
            ],
        },
        fallbacks=[],
        allow_reentry=True,
    )


def get_my_applications_handlers() -> list:
    return [
        CommandHandler("myapps", cmd_my_applications),
    ]
