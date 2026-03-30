"""
Skill verification dialog — triggered by job card buttons.

Handlers:
  ConversationHandler (build_skill_verify_handler):
    entry: [✅ I know these]  →  job_skills_{job_id}  callback
    state VERIFY_CONTEXT: user types skill experience sentence
    Saves/updates SkillNode + SkillEvidence, then moves to next gap.
    When all gaps are done → summary message.

  Standalone callbacks (get_job_card_handlers):
    [⏭ Skip]     → mark job status="skipped"
    [📄 Full JD] → send raw_jd as a follow-up message

  Command (cmd_pending_jobs):
    /jobs or "📋 Pending Jobs" keyboard button → list pending jobs
"""

import logging
from datetime import datetime

from telegram import Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from db.models import Job, SkillEvidence, SkillNode, User
from db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

VERIFY_CONTEXT = 0


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _get_job(job_id: str) -> Job | None:
    from sqlalchemy import select
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Job).where(Job.id == job_id))
        return result.scalar_one_or_none()


async def _ask_next_gap(context: ContextTypes.DEFAULT_TYPE, msg) -> int:
    gaps = context.user_data.get("verify_gaps", [])
    idx  = context.user_data.get("verify_idx", 0)

    if idx >= len(gaps):
        return await _finish_verification(context, msg)

    skill      = gaps[idx]["skill"]
    importance = gaps[idx].get("importance", "preferred")
    total      = len(gaps)

    await msg.reply_text(
        f"Gap {idx + 1} of {total}: *{skill}* ({importance})\n\n"
        "Have you used this professionally? One sentence — company, what you built, duration.\n\n"
        "_Example: Managed ICU ward at City Hospital for 18 months_ or _Built APIs at Acme Corp for 8 months_\n\n"
        "Or type `skip` to pass.",
        parse_mode="Markdown",
    )
    return VERIFY_CONTEXT


async def _finish_verification(context: ContextTypes.DEFAULT_TYPE, msg) -> int:
    job_id  = context.user_data.get("verify_job_id")
    user_id = context.user_data.get("verify_user_id")

    await msg.reply_text("All gaps reviewed! Skill graph updated.")

    if job_id and user_id:
        from bot.handlers.job_approval import start_resume_generation
        ai = context.application.bot_data["ai"]
        await start_resume_generation(job_id, user_id, msg, ai)

    context.user_data.clear()
    return ConversationHandler.END


# ── ConversationHandler states ───────────────────────────────────────────────

async def job_skills_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry: user tapped [✅ I know these] on a job card."""
    query = update.callback_query
    await query.answer()

    job_id = query.data.split("job_skills_", 1)[1]
    job    = await _get_job(job_id)

    if not job:
        await query.edit_message_text("Job not found — it may have expired.")
        return ConversationHandler.END

    fit  = (job.parsed or {}).get("_fit", {})
    gaps = [
        g for g in fit.get("missing_required", [])
        if g.get("importance") in ("required", "preferred")
    ]

    if not gaps:
        await query.edit_message_text(
            "No skill gaps — your profile already covers everything."
        )
        from bot.handlers.job_approval import start_resume_generation
        ai = context.application.bot_data["ai"]
        await start_resume_generation(job_id, job.user_id, query.message, ai)
        return ConversationHandler.END

    context.user_data["verify_job_id"]  = job_id
    context.user_data["verify_user_id"] = job.user_id
    context.user_data["verify_gaps"]    = gaps
    context.user_data["verify_idx"]     = 0

    await query.edit_message_text(
        f"Found {len(gaps)} gap skill(s) to verify. I'll ask about each one.\n\n"
        "Your answers build your skill evidence graph — they'll auto-generate resume bullets later."
    )
    return await _ask_next_gap(context, query.message)


async def handle_verify_context(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User typed their skill context (or 'skip')."""
    text = update.message.text.strip()
    idx  = context.user_data.get("verify_idx", 0)
    gaps = context.user_data.get("verify_gaps", [])

    if idx >= len(gaps):
        return await _finish_verification(context, update.message)

    skill_name = gaps[idx]["skill"]
    job_id     = context.user_data.get("verify_job_id")

    if text.lower() != "skip":
        from sqlalchemy import select
        async with AsyncSessionLocal() as session:
            async with session.begin():
                job_result = await session.execute(select(Job).where(Job.id == job_id))
                job = job_result.scalar_one_or_none()

                if job:
                    node_result = await session.execute(
                        select(SkillNode).where(
                            SkillNode.user_id  == job.user_id,
                            SkillNode.skill_name == skill_name,
                        )
                    )
                    node = node_result.scalar_one_or_none()

                    if node:
                        node.status     = "verified_attested"
                        node.updated_at = datetime.utcnow()
                    else:
                        node = SkillNode(
                            user_id    = job.user_id,
                            skill_name = skill_name,
                            status     = "verified_attested",
                            confidence = "medium",
                            created_at = datetime.utcnow(),
                            updated_at = datetime.utcnow(),
                        )
                        session.add(node)
                        await session.flush()

                    session.add(SkillEvidence(
                        skill_node_id = node.id,
                        user_context  = text,
                        source        = "telegram",
                    ))

        safe_name = skill_name.replace("_", "\\_").replace("*", "\\*").replace("`", "\\`")
        await update.message.reply_text(f"Saved: *{safe_name}* ✅", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"Skipped: {skill_name}")

    context.user_data["verify_idx"] = idx + 1
    return await _ask_next_gap(context, update.message)


# ── Standalone job card callbacks ────────────────────────────────────────────

async def _do_skip_job(query, job_id: str, telegram_id: str, bot) -> None:
    """Shared logic: mark a job as skipped and queue the next pending card."""
    from sqlalchemy import select
    from jobs.scheduler import send_next_pending_card

    async with AsyncSessionLocal() as session:
        async with session.begin():
            result = await session.execute(select(Job).where(Job.id == job_id))
            job = result.scalar_one_or_none()
            if job:
                job.status = "skipped"
    await query.edit_message_text("Job skipped.")
    await send_next_pending_card(telegram_id, bot)


async def job_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles job_skip_{id} from the job card keyboard."""
    query  = update.callback_query
    await query.answer()
    job_id = query.data.split("job_skip_", 1)[1]
    await _do_skip_job(query, job_id, str(update.effective_user.id), context.bot)


async def job_skip_delivery(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles skip_job_{id} from resume delivery / approval keyboards."""
    query  = update.callback_query
    await query.answer()
    job_id = query.data.split("skip_job_", 1)[1]
    await _do_skip_job(query, job_id, str(update.effective_user.id), context.bot)


async def job_generate_anyway(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User tapped [➡️ Generate Anyway] — skip skill verify, go straight to resume gen."""
    query  = update.callback_query
    await query.answer()
    job_id = query.data.split("job_generate_", 1)[1]

    job = await _get_job(job_id)
    if not job:
        await query.edit_message_text("Job not found — it may have expired.")
        return

    from bot.handlers.job_approval import start_resume_generation
    ai = context.application.bot_data["ai"]
    await start_resume_generation(job_id, job.user_id, query.message, ai)


async def job_full_jd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query  = update.callback_query
    await query.answer()
    job_id = query.data.split("job_fulljd_", 1)[1]

    job = await _get_job(job_id)

    if not job or not job.raw_jd:
        await query.message.reply_text("Full JD not available.")
        return

    jd_text = job.raw_jd[:3000] + ("…" if len(job.raw_jd) > 3000 else "")
    link    = f"\n\n🔗 {job.url}" if job.url else ""

    # Send header (Markdown) + raw body (no parse_mode) as one plain message
    # to avoid crashes on unescaped * _ ` [ characters in the JD text.
    await query.message.reply_text(
        f"📄 {job.title} @ {job.company}\n\n{jd_text}{link}",
    )


# ── /jobs command ────────────────────────────────────────────────────────────

async def cmd_pending_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List the user's pending job cards."""
    from sqlalchemy import select
    from bot.keyboards import job_card_keyboard

    tg_id = str(update.effective_user.id)

    async with AsyncSessionLocal() as session:
        user_result = await session.execute(
            select(User).where(User.telegram_id == tg_id)
        )
        user = user_result.scalar_one_or_none()

        if not user:
            await update.message.reply_text("Run /start first to set up your profile.")
            return

        jobs_result = await session.execute(
            select(Job)
            .where(Job.user_id == user.id, Job.status == "pending")
            .order_by(Job.created_at.desc())
            .limit(10)
        )
        pending = jobs_result.scalars().all()

    if not pending:
        await update.message.reply_text(
            "No pending jobs right now.\n\nThe bot scrapes at 08:00 and 18:00 daily."
        )
        return

    await update.message.reply_text(f"You have {len(pending)} pending job(s):")

    def _md(t) -> str:
        return (str(t) if t else "").replace("\\", "\\\\").replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")

    for job in pending:
        fit    = (job.parsed or {}).get("_fit", {})

        matched = ", ".join(_md(s) for s in fit.get("matched_skills", [])[:4]) or "—"
        gaps    = fit.get("missing_required", [])
        gap_str = ", ".join(_md(g.get("skill", "?")) for g in gaps[:3]) if gaps else "None"

        text = (
            f"🏢 *{_md(job.title)}*\n"
            f"{_md(job.company)}\n\n"
            f"Fit Score: *{job.fit_score or 0:.0f}%*\n"
            f"✅ Matched: {matched}\n"
            f"❓ Gaps: {gap_str}"
        )
        fallback_url = job.url or "https://www.google.com"
        await update.message.reply_text(
            text,
            parse_mode="Markdown",
            reply_markup=job_card_keyboard(job.id, fallback_url),
        )


# ── Handler builders ─────────────────────────────────────────────────────────

def build_skill_verify_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(job_skills_start, pattern=r"^job_skills_"),
        ],
        states={
            VERIFY_CONTEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_verify_context),
            ],
        },
        fallbacks=[],
        allow_reentry=True,
    )


def get_job_card_handlers() -> list:
    """Standalone handlers for job card buttons."""
    return [
        CallbackQueryHandler(job_skip,            pattern=r"^job_skip_"),
        CallbackQueryHandler(job_skip_delivery,   pattern=r"^skip_job_"),
        CallbackQueryHandler(job_full_jd,         pattern=r"^job_fulljd_"),
        CallbackQueryHandler(job_generate_anyway, pattern=r"^job_generate_"),
    ]


def get_jobs_command_handler() -> CommandHandler:
    return CommandHandler("jobs", cmd_pending_jobs)
