"""
Handlers for post-onboarding commands:
  /skills   — show skill graph summary
  /resume   — upload a new resume to update skill graph
  /pause    — pause/resume job hunting
  /settings — show current settings
  /filters  — quick filter update
  /help     — command list
"""

import logging
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler, MessageHandler, filters
from sqlalchemy import select

from db.session import AsyncSessionLocal
from db.models import User, SkillNode, SkillEvidence
from bot.keyboards import MAIN_KEYBOARD

logger = logging.getLogger(__name__)


async def cmd_skills(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show skill graph grouped by status."""
    tg_id = str(update.effective_user.id)
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.telegram_id == tg_id))
        user = result.scalar_one_or_none()
        if not user:
            await update.message.reply_text("You're not set up yet. Run /start first.")
            return

        nodes_result = await session.execute(select(SkillNode).where(SkillNode.user_id == user.id))
        nodes = nodes_result.scalars().all()

    if not nodes:
        await update.message.reply_text("No skills in your graph yet. Run /start to add your resume.")
        return

    by_status: dict[str, list] = {}
    for node in nodes:
        by_status.setdefault(node.status, []).append(node.skill_name)

    lines = ["*Your Skill Graph*\n"]
    labels = {
        "verified_attested": "Verified with context",
        "verified_resume": "Verified from resume",
        "partial": "Partial (no context)",
        "gap": "Gap (missing)",
    }
    for status, label in labels.items():
        names = by_status.get(status, [])
        if names:
            lines.append(f"*{label}* ({len(names)})\n" + ", ".join(names))

    await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle job hunting pause."""
    tg_id = str(update.effective_user.id)
    async with AsyncSessionLocal() as session:
        async with session.begin():
            result = await session.execute(select(User).where(User.telegram_id == tg_id))
            user = result.scalar_one_or_none()
            if not user:
                await update.message.reply_text("Run /start first.")
                return

            filters_data = user.filters or {}
            paused = filters_data.get("paused", False)
            filters_data["paused"] = not paused
            user.filters = filters_data

    status = "paused" if not paused else "resumed"
    await update.message.reply_text(
        f"Job hunting *{status}*. " + ("I won't notify you about new jobs." if not paused else "Back to scanning jobs for you."),
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current user settings."""
    tg_id = str(update.effective_user.id)
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.telegram_id == tg_id))
        user = result.scalar_one_or_none()

    if not user:
        await update.message.reply_text("Run /start to set up your profile.")
        return

    f = user.filters or {}
    lines = [
        "*Current Settings*\n",
        f"Role: {f.get('role', 'not set')}",
        f"Location: {f.get('location', 'any')}",
        f"Remote: {f.get('remote', 'any')}",
        f"Min salary: {f.get('min_salary', 0) or 'none'}",
        f"Blacklist: {', '.join(f.get('blacklist', [])) or 'none'}",
        f"Notify frequency: {user.notify_freq or 'daily'}",
        f"Min fit score: {user.min_fit_score}%",
        f"Status: {'paused' if f.get('paused') else 'active'}",
        "\nRun /start to update everything, or /filters to update just your filters.",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current filters with a reminder to update via /start."""
    tg_id = str(update.effective_user.id)
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.telegram_id == tg_id))
        user = result.scalar_one_or_none()

    if not user:
        await update.message.reply_text("Run /start first.")
        return

    f = user.filters or {}
    await update.message.reply_text(
        f"*Current Filters*\n\n"
        f"Role: {f.get('role', 'not set')}\n"
        f"Location: {f.get('location', 'any')}\n"
        f"Remote: {f.get('remote', 'any')}\n"
        f"Min salary: {f.get('min_salary', 0) or 'none'}\n"
        f"Blacklist: {', '.join(f.get('blacklist', [])) or 'none'}\n\n"
        "To update, run /start (it preserves your skill graph).",
        parse_mode="Markdown",
    )


async def cmd_deleteskill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete a skill by name. Usage: /deleteskill Python"""
    tg_id = str(update.effective_user.id)
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: /deleteskill SkillName\n\nExample: /deleteskill PHP\n\n"
            "Run /skills to see your full list."
        )
        return

    skill_name = " ".join(args).strip()
    async with AsyncSessionLocal() as session:
        async with session.begin():
            result = await session.execute(select(User).where(User.telegram_id == tg_id))
            user = result.scalar_one_or_none()
            if not user:
                await update.message.reply_text("Run /start first.")
                return

            node_result = await session.execute(
                select(SkillNode).where(
                    SkillNode.user_id == user.id,
                    SkillNode.skill_name.ilike(skill_name),
                )
            )
            node = node_result.scalar_one_or_none()
            if not node:
                await update.message.reply_text(
                    f"No skill found matching '{skill_name}'.\nRun /skills to see your list."
                )
                return

            await session.delete(node)

    await update.message.reply_text(
        f"Removed *{node.skill_name}* from your skill graph.",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*HireLoop Commands*\n\n"
        "/start — onboarding wizard (or re-run to update)\n"
        "/skills — view your skill graph\n"
        "/resume — upload a new resume version\n"
        "/jobs — pending jobs waiting for your action\n"
        "/history — past applications + outcomes\n"
        "/settings — view all preferences\n"
        "/filters — view current job filters\n"
        "/pause — pause or resume job hunting\n"
        "/cancel — cancel current operation\n"
        "/help — this message",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )


def get_settings_handlers():
    return [
        CommandHandler("skills", cmd_skills),
        CommandHandler("deleteskill", cmd_deleteskill),
        CommandHandler("pause", cmd_pause),
        CommandHandler("settings", cmd_settings),
        CommandHandler("filters", cmd_filters),
        CommandHandler("help", cmd_help),
    ]
