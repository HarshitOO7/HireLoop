"""
/addskills — add more skills to the graph without wiping existing ones.
Also triggered by the "📎 Add Resume" persistent keyboard button.

Paths:
  A) Upload resume → AI parse → dedup vs existing → verify medium/low → merge
  B) Add manually → type skill name → optional context → merge
"""

import logging
import time
from datetime import datetime

from sqlalchemy import select
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
    MAIN_KEYBOARD,
    add_skill_confirm_keyboard,
    add_skill_done_uploading_keyboard,
    add_skills_menu_keyboard,
)
from bot.onboarding import _extract_docx, _extract_pdf, _normalize_skill
from db.models import SkillEvidence, SkillNode, User
from db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

# ── States ───────────────────────────────────────────────────────────────────
(
    ADD_MENU,
    ADD_UPLOAD,
    ADD_CONFIRM,
    ADD_CONTEXT,
    ADD_MANUAL_NAME,
    ADD_MANUAL_CTX,
) = range(6)

_CONF_RANK = {"high": 3, "medium": 2, "low": 1}


# ── DB merge (never wipes) ───────────────────────────────────────────────────

async def _merge_skills_to_db(telegram_id: str, new_skills: list) -> tuple[int, int]:
    """Merge new_skills into the existing skill graph without deleting anything.
    Returns (added, updated).
    """
    added = updated = 0

    async with AsyncSessionLocal() as session:
        async with session.begin():
            result = await session.execute(select(User).where(User.telegram_id == telegram_id))
            user = result.scalar_one_or_none()
            if not user:
                return 0, 0

            existing_result = await session.execute(
                select(SkillNode).where(SkillNode.user_id == user.id)
            )
            existing_nodes: dict[str, SkillNode] = {
                _normalize_skill(n.skill_name): n for n in existing_result.scalars()
            }

            for skill in new_skills:
                key = _normalize_skill(skill["skill_name"])
                context_text = skill.get("user_context", "") or skill.get("evidence", "")
                source = "telegram" if skill.get("status") == "verified_attested" else "resume"

                if key in existing_nodes:
                    node = existing_nodes[key]
                    # Upgrade confidence if new is higher
                    new_rank = _CONF_RANK.get(skill.get("confidence", "medium"), 0)
                    old_rank = _CONF_RANK.get(node.confidence or "medium", 0)
                    if new_rank > old_rank:
                        node.confidence = skill["confidence"]
                    # Upgrade status: partial → verified_resume → verified_attested
                    status_rank = {"partial": 0, "verified_resume": 1, "verified_attested": 2}
                    if status_rank.get(skill.get("status", ""), -1) > status_rank.get(node.status or "", -1):
                        node.status = skill["status"]
                    node.updated_at = datetime.utcnow()
                    if context_text:
                        session.add(SkillEvidence(
                            skill_node_id=node.id,
                            user_context=context_text,
                            source=source,
                        ))
                    updated += 1
                else:
                    node = SkillNode(
                        user_id=user.id,
                        skill_name=skill["skill_name"],
                        status=skill.get("status", "verified_resume"),
                        confidence=skill.get("confidence", "medium"),
                        created_at=datetime.utcnow(),
                        updated_at=datetime.utcnow(),
                    )
                    session.add(node)
                    await session.flush()
                    if context_text:
                        session.add(SkillEvidence(
                            skill_node_id=node.id,
                            user_context=context_text,
                            source=source,
                        ))
                    added += 1

    logger.info("[add_skills] merge done — added=%d updated=%d", added, updated)
    return added, updated


# ── Entry point ──────────────────────────────────────────────────────────────

async def cmd_addskills(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tg_id = str(update.effective_user.id)
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.telegram_id == tg_id))
        user = result.scalar_one_or_none()

    if not user or not user.onboarded:
        await update.message.reply_text("Run /start first to set up your profile.")
        return ConversationHandler.END

    context.user_data["add_resume_texts"] = []
    context.user_data["add_confirmed"] = []
    context.user_data["add_pending"] = []
    context.user_data["add_pending_idx"] = 0
    context.user_data["add_existing_keys"] = set()

    await update.message.reply_text(
        "How would you like to add skills?",
        reply_markup=add_skills_menu_keyboard(),
    )
    return ADD_MENU


async def add_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "addskills_upload":
        sent = await query.edit_message_text(
            "Send your resume (PDF or Word). I'll extract new skills and merge them "
            "into your graph without removing anything.",
            reply_markup=add_skill_done_uploading_keyboard(),
        )
        context.user_data["add_done_btn_msg_id"] = sent.message_id
        return ADD_UPLOAD

    if query.data == "addskills_manual":
        await query.edit_message_text(
            "Type the skill name you want to add:\n\nExample: Kubernetes"
        )
        return ADD_MANUAL_NAME

    # addskills_cancel
    await query.edit_message_text("No changes made.")
    return ConversationHandler.END


# ── Path A: upload resume ────────────────────────────────────────────────────

async def handle_add_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    doc = update.message.document
    fname = (doc.file_name or "").lower()

    if not (fname.endswith(".pdf") or fname.endswith(".doc") or fname.endswith(".docx")):
        await update.message.reply_text("Please send a PDF or Word (.docx) file.")
        return ADD_UPLOAD

    if len(context.user_data.get("add_resume_texts", [])) >= 4:
        await update.message.reply_text("4 resumes is the max. Tap Done to continue.")
        return ADD_UPLOAD

    await update.message.reply_text("Reading your resume...")
    file = await doc.get_file()
    data = bytes(await file.download_as_bytearray())

    try:
        text = _extract_pdf(data) if fname.endswith(".pdf") else _extract_docx(data)
    except Exception as e:
        logger.error("File extraction failed: %s", e)
        await update.message.reply_text("Couldn't read that file. Try a different format.")
        return ADD_UPLOAD

    context.user_data.setdefault("add_resume_texts", []).append(text)
    count = len(context.user_data["add_resume_texts"])

    prev_msg_id = context.user_data.get("add_done_btn_msg_id")
    if prev_msg_id:
        try:
            await context.bot.delete_message(
                chat_id=update.effective_chat.id, message_id=prev_msg_id
            )
        except Exception:
            pass

    sent = await update.message.reply_text(
        f"Got it! {count} resume{'s' if count > 1 else ''} uploaded. Send another or tap Done.",
        reply_markup=add_skill_done_uploading_keyboard(),
    )
    context.user_data["add_done_btn_msg_id"] = sent.message_id
    return ADD_UPLOAD


async def done_adding_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    resume_texts = context.user_data.get("add_resume_texts", [])
    if not resume_texts:
        await query.edit_message_text(
            "Please upload at least one resume first.",
            reply_markup=add_skill_done_uploading_keyboard(),
        )
        return ADD_UPLOAD

    await query.edit_message_text("Analyzing resume(s)...")

    ai = context.application.bot_data["ai"]
    tg_id = str(update.effective_user.id)
    all_skills = []
    t_total = time.monotonic()

    # Load existing normalized keys to detect what's truly new
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.telegram_id == tg_id))
        user = result.scalar_one_or_none()
        existing_result = await session.execute(
            select(SkillNode).where(SkillNode.user_id == user.id)
        )
        existing_keys = {_normalize_skill(n.skill_name) for n in existing_result.scalars()}
    context.user_data["add_existing_keys"] = existing_keys

    for i, text in enumerate(resume_texts, 1):
        t_file = time.monotonic()
        try:
            parsed = await ai.parse_resume(text)
            raw_skills = parsed.get("skills", [])
            logger.info("[add_skills] resume %d: %d raw skills in %.2fs",
                        i, len(raw_skills), time.monotonic() - t_file)
            all_skills.extend(raw_skills)
        except Exception as e:
            logger.error("[add_skills] resume %d parse error: %s", i, e)

    # Dedup within uploaded files
    skill_map: dict = {}
    for s in all_skills:
        key = _normalize_skill(s["skill_name"])
        if key not in skill_map or _CONF_RANK.get(s["confidence"], 0) > _CONF_RANK.get(skill_map[key]["confidence"], 0):
            skill_map[key] = s
    merged = list(skill_map.values())

    new_skills = [s for s in merged if _normalize_skill(s["skill_name"]) not in existing_keys]
    already_have = [s for s in merged if _normalize_skill(s["skill_name"]) in existing_keys]

    logger.info("[add_skills] %d total extracted, %d new, %d already in graph — %.2fs",
                len(merged), len(new_skills), len(already_have), time.monotonic() - t_total)

    if not new_skills:
        note = f" — all {len(already_have)} extracted skills are already in your graph" if already_have else ""
        await query.message.reply_text(
            f"No new skills found{note}. Nothing was changed.",
            reply_markup=MAIN_KEYBOARD,
        )
        return ConversationHandler.END

    high = [s for s in new_skills if s["confidence"] == "high"]
    pending = [s for s in new_skills if s["confidence"] in ("medium", "low")]

    confirmed = [{**s, "status": "verified_resume", "user_context": s.get("evidence", "")} for s in high]
    context.user_data["add_confirmed"] = confirmed
    context.user_data["add_pending"] = pending
    context.user_data["add_pending_idx"] = 0

    info_parts = []
    if already_have:
        info_parts.append(f"{len(already_have)} already in your graph (skipped)")
    if high:
        names = ", ".join(s["skill_name"] for s in high)
        info_parts.append(f"Auto-confirmed {len(high)} high-confidence: {names}")
    if info_parts:
        await query.message.reply_text(" · ".join(info_parts))

    if not pending:
        added, updated = await _merge_skills_to_db(tg_id, confirmed)
        await query.message.reply_text(
            f"Done! Added {added} new skill(s), updated {updated}.",
            reply_markup=MAIN_KEYBOARD,
        )
        return ConversationHandler.END

    return await _show_next_add_skill(update, context)


async def _show_next_add_skill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    pending = context.user_data.get("add_pending", [])
    idx = context.user_data.get("add_pending_idx", 0)
    msg = update.callback_query.message if update.callback_query else update.message

    if idx >= len(pending):
        tg_id = str(update.effective_user.id)
        all_confirmed = context.user_data.get("add_confirmed", [])
        added, updated = await _merge_skills_to_db(tg_id, all_confirmed)
        await msg.reply_text(
            f"Done! Added {added} new skill(s), updated {updated} existing.",
            reply_markup=MAIN_KEYBOARD,
        )
        return ConversationHandler.END

    skill = pending[idx]
    existing_keys = context.user_data.get("add_existing_keys", set())
    tag = "UPDATE" if _normalize_skill(skill["skill_name"]) in existing_keys else "NEW"
    conf_label = skill.get("confidence", "medium")

    await msg.reply_text(
        f"Skill {idx + 1} of {len(pending)} [{tag}]\n\n"
        f"*{skill['skill_name']}* ({conf_label} confidence)\n\n"
        "Include this in your graph?",
        parse_mode="Markdown",
        reply_markup=add_skill_confirm_keyboard(idx),
    )
    return ADD_CONFIRM


async def add_skill_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split("_")[-1])
    skill = context.user_data["add_pending"][idx]
    context.user_data["add_confirmed"].append(
        {**skill, "status": "verified_resume", "user_context": skill.get("evidence", "")}
    )
    await query.edit_message_text(f"Added: {skill['skill_name']} \u2705")
    context.user_data["add_pending_idx"] = idx + 1
    return await _show_next_add_skill(update, context)


async def add_skill_context_requested(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split("_")[-1])
    skill = context.user_data["add_pending"][idx]
    context.user_data["add_context_for_idx"] = idx
    await query.edit_message_text(
        f"Tell me about your *{skill['skill_name']}* experience:\n\n"
        "One sentence — company, what you built, how long.\n\n"
        "Example: _Led patient triage at City Hospital for 2 years_ or _Built APIs at Acme for 8 months_",
        parse_mode="Markdown",
    )
    return ADD_CONTEXT


async def handle_add_skill_context(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    idx = context.user_data["add_context_for_idx"]
    skill = context.user_data["add_pending"][idx]
    context.user_data["add_confirmed"].append({
        **skill, "status": "verified_attested", "user_context": update.message.text.strip()
    })
    await update.message.reply_text(f"Saved context for {skill['skill_name']} \u2705")
    context.user_data["add_pending_idx"] = idx + 1
    return await _show_next_add_skill(update, context)


async def add_skill_removed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split("_")[-1])
    skill = context.user_data["add_pending"][idx]
    await query.edit_message_text(f"Skipped: {skill['skill_name']}")
    context.user_data["add_pending_idx"] = idx + 1
    return await _show_next_add_skill(update, context)


# ── Path B: manual entry ─────────────────────────────────────────────────────

async def handle_manual_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("Please type a skill name.")
        return ADD_MANUAL_NAME

    context.user_data["add_manual_skill_name"] = name
    await update.message.reply_text(
        f"Got it — *{name}*.\n\n"
        "Add a one-line context? (company, what you built, duration)\n\n"
        "Or type *skip* to save without context.",
        parse_mode="Markdown",
    )
    return ADD_MANUAL_CTX


async def handle_manual_ctx(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tg_id = str(update.effective_user.id)
    name = context.user_data["add_manual_skill_name"]
    text = update.message.text.strip()
    user_context = "" if text.lower() == "skip" else text

    skill = {
        "skill_name": name,
        "confidence": "high",
        "status": "verified_attested",
        "user_context": user_context,
        "evidence": "",
    }
    added, updated = await _merge_skills_to_db(tg_id, [skill])
    action = "Updated" if updated else "Added"
    await update.message.reply_text(
        f"{action} *{name}* to your skill graph \u2705\n\n"
        "Type another skill name to keep adding, or /cancel to stop.",
        parse_mode="Markdown",
    )
    return ADD_MANUAL_NAME


async def cancel_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Stopped.", reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END


# ── ConversationHandler builder ──────────────────────────────────────────────

def build_add_skills_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("addskills", cmd_addskills),
            MessageHandler(filters.Regex(r"^📎 Add Resume$"), cmd_addskills),
        ],
        states={
            ADD_MENU: [
                CallbackQueryHandler(add_menu_choice, pattern=r"^addskills_"),
            ],
            ADD_UPLOAD: [
                MessageHandler(filters.Document.ALL, handle_add_document),
                CallbackQueryHandler(done_adding_resume, pattern="^add_done_uploading$"),
            ],
            ADD_CONFIRM: [
                CallbackQueryHandler(add_skill_confirmed,         pattern=r"^add_confirm_\d+$"),
                CallbackQueryHandler(add_skill_context_requested, pattern=r"^add_ctx_\d+$"),
                CallbackQueryHandler(add_skill_removed,           pattern=r"^add_remove_\d+$"),
            ],
            ADD_CONTEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_add_skill_context),
            ],
            ADD_MANUAL_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_manual_name),
            ],
            ADD_MANUAL_CTX: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_manual_ctx),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_add)],
        allow_reentry=True,
    )
