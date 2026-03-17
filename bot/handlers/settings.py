"""
Handlers for post-onboarding commands.
"""

import html
import io
import logging
from datetime import datetime
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from db.session import AsyncSessionLocal
from db.models import User, SkillNode
from bot.keyboards import MAIN_KEYBOARD

logger = logging.getLogger(__name__)


def _e(value) -> str:
    """HTML-escape user data — prevents Telegram Markdown parser crashes."""
    return html.escape(str(value))


# ── Skill graph HTML renderer ───────────────────────────────────────────────

_STATUS_META = {
    "verified_attested": {
        "label": "Verified with Context",
        "dot": "#16a34a",
        "css": "s-verified_attested",
    },
    "verified_resume": {
        "label": "Verified from Resume",
        "dot": "#2563eb",
        "css": "s-verified_resume",
    },
    "partial": {
        "label": "Partial (no evidence)",
        "dot": "#d97706",
        "css": "s-partial",
    },
    "gap": {
        "label": "Gap (missing from profile)",
        "dot": "#dc2626",
        "css": "s-gap",
    },
}

_CONF_DOTS = {"high": "●●●", "medium": "●●○", "low": "●○○"}


def _build_skill_graph_html(user_name: str, nodes: list) -> str:
    by_status: dict[str, list] = {}
    for node in nodes:
        by_status.setdefault(node.status, []).append(node)

    total = len(nodes)
    verified = len(by_status.get("verified_attested", [])) + len(by_status.get("verified_resume", []))
    gaps = len(by_status.get("gap", []))
    generated_at = datetime.utcnow().strftime("%B %d, %Y at %H:%M UTC")

    # Build sections
    sections_html = ""
    for status, meta in _STATUS_META.items():
        status_nodes = by_status.get(status, [])
        if not status_nodes:
            continue

        pills_html = ""
        for node in sorted(status_nodes, key=lambda n: n.skill_name.lower()):
            conf = _CONF_DOTS.get(node.confidence or "medium", "●●○")
            evidence_html = ""
            for ev in (node.evidence or []):
                if ev.user_context:
                    evidence_html = (
                        f'<div class="pill-evidence">{html.escape(ev.user_context[:120])}</div>'
                    )
                    break
            pills_html += (
                f'<div class="pill {meta["css"]}">'
                f'<span class="pill-name">{html.escape(node.skill_name)}</span>'
                f'<span class="pill-conf">{conf}</span>'
                f"{evidence_html}"
                f"</div>"
            )

        sections_html += (
            f'<div class="section">'
            f'<div class="section-header">'
            f'<div class="section-dot" style="background:{meta["dot"]}"></div>'
            f'<span class="section-title">{meta["label"]}</span>'
            f'<span class="section-count">{len(status_nodes)}</span>'
            f"</div>"
            f'<div class="pills">{pills_html}</div>'
            f"</div>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Skill Graph \u2014 {html.escape(user_name)}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f8fafc;color:#1e293b;min-height:100vh}}
.header{{background:linear-gradient(135deg,#1e293b 0%,#334155 100%);color:#fff;padding:32px 28px}}
.header h1{{font-size:26px;font-weight:700;margin-bottom:6px}}
.header p{{opacity:.65;font-size:13px}}
.stats{{display:flex;gap:12px;margin-top:20px;flex-wrap:wrap}}
.stat{{background:rgba(255,255,255,.15);border-radius:10px;padding:10px 18px;min-width:80px}}
.stat-num{{font-size:22px;font-weight:700}}
.stat-lbl{{font-size:11px;opacity:.75;margin-top:2px}}
.content{{padding:24px 28px;max-width:960px;margin:0 auto}}
.legend{{display:flex;gap:14px;flex-wrap:wrap;align-items:center;margin-bottom:24px;padding:14px 18px;background:#fff;border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,.07)}}
.legend-item{{display:flex;align-items:center;gap:7px;font-size:12px;color:#475569}}
.legend-dot{{width:10px;height:10px;border-radius:50%;flex-shrink:0}}
.legend-conf{{margin-left:auto;color:#94a3b8;font-size:12px}}
.section{{margin-bottom:28px}}
.section-header{{display:flex;align-items:center;gap:10px;margin-bottom:14px}}
.section-dot{{width:12px;height:12px;border-radius:50%;flex-shrink:0}}
.section-title{{font-size:15px;font-weight:600}}
.section-count{{font-size:12px;color:#64748b;background:#f1f5f9;padding:2px 9px;border-radius:12px}}
.pills{{display:flex;flex-wrap:wrap;gap:10px}}
.pill{{border-radius:10px;padding:9px 14px;font-size:13px;font-weight:500;max-width:280px}}
.pill-name{{display:block;font-weight:600}}
.pill-conf{{display:block;font-size:11px;opacity:.6;margin-top:3px;letter-spacing:1px}}
.pill-evidence{{font-size:11px;opacity:.75;margin-top:6px;font-style:italic;line-height:1.4;border-top:1px solid rgba(0,0,0,.1);padding-top:6px}}
.s-verified_attested{{background:#dcfce7;color:#15803d;border:1px solid #86efac}}
.s-verified_resume{{background:#dbeafe;color:#1d4ed8;border:1px solid #93c5fd}}
.s-partial{{background:#fef3c7;color:#b45309;border:1px solid #fcd34d}}
.s-gap{{background:#fee2e2;color:#b91c1c;border:1px solid #fca5a5}}
footer{{text-align:center;color:#94a3b8;font-size:12px;padding:28px;margin-top:8px}}
</style>
</head>
<body>
<div class="header">
  <h1>&#x1F4CA; {html.escape(user_name)}\u2019s Skill Graph</h1>
  <p>Generated by HireLoop &middot; {generated_at}</p>
  <div class="stats">
    <div class="stat"><div class="stat-num">{total}</div><div class="stat-lbl">Total Skills</div></div>
    <div class="stat"><div class="stat-num">{verified}</div><div class="stat-lbl">Verified</div></div>
    <div class="stat"><div class="stat-num">{total - gaps}</div><div class="stat-lbl">You Have</div></div>
    <div class="stat"><div class="stat-num">{gaps}</div><div class="stat-lbl">Gaps</div></div>
  </div>
</div>
<div class="content">
  <div class="legend">
    <div class="legend-item"><div class="legend-dot" style="background:#16a34a"></div>Verified with context</div>
    <div class="legend-item"><div class="legend-dot" style="background:#2563eb"></div>From resume</div>
    <div class="legend-item"><div class="legend-dot" style="background:#d97706"></div>Partial</div>
    <div class="legend-item"><div class="legend-dot" style="background:#dc2626"></div>Gap</div>
    <div class="legend-conf">&bull;&bull;&bull; high &nbsp;&nbsp; &bull;&bull;&#9675; medium &nbsp;&nbsp; &bull;&#9675;&#9675; low</div>
  </div>
  {sections_html}
</div>
<footer>HireLoop &middot; /deleteskill SkillName to remove a skill &middot; /start to re-run onboarding</footer>
</body>
</html>"""


# ── Handlers ────────────────────────────────────────────────────────────────

async def cmd_skills(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.telegram_id == tg_id))
        user = result.scalar_one_or_none()
        if not user:
            await update.message.reply_text("You're not set up yet. Run /start first.")
            return
        nodes_result = await session.execute(
            select(SkillNode)
            .where(SkillNode.user_id == user.id)
            .options(selectinload(SkillNode.evidence))
        )
        nodes = nodes_result.scalars().all()

    if not nodes:
        await update.message.reply_text("No skills in your graph yet. Run /start to add your resume.")
        return

    # Quick count summary for the Telegram message
    by_status: dict[str, int] = {}
    for node in nodes:
        by_status[node.status] = by_status.get(node.status, 0) + 1

    labels = {
        "verified_attested": "&#x2705; Attested",
        "verified_resume":   "&#x1F4C4; Resume",
        "partial":           "&#x26A0;&#xFE0F; Partial",
        "gap":               "&#x274C; Gap",
    }
    parts = []
    for status, label in labels.items():
        count = by_status.get(status, 0)
        if count:
            parts.append(f"{label}: <b>{count}</b>")

    user_name = user.name or update.effective_user.first_name or "Your"
    summary = (
        f"<b>Skill Graph</b> \u2014 {len(nodes)} skills total\n"
        + " | ".join(parts)
        + "\n\n<i>Full visual report attached \u2193</i>"
    )

    # Generate and send HTML file
    html_bytes = _build_skill_graph_html(user_name, nodes).encode("utf-8")
    html_file = io.BytesIO(html_bytes)
    html_file.name = "skill_graph.html"

    await update.message.reply_text(summary, parse_mode="HTML")
    await update.message.reply_document(
        document=html_file,
        filename="skill_graph.html",
        caption="Open in your browser to view your full skill graph.",
    )


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    now_paused = not paused
    tail = "I won't notify you about new jobs." if now_paused else "Back to scanning jobs for you."
    status_word = "paused" if now_paused else "resumed"
    await update.message.reply_text(
        f"Job hunting <b>{status_word}</b>. {tail}",
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD,
    )


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.telegram_id == tg_id))
        user = result.scalar_one_or_none()

    if not user:
        await update.message.reply_text("Run /start to set up your profile.")
        return

    f = user.filters or {}
    blacklist = ", ".join(f.get("blacklist", [])) or "none"
    lines = [
        "<b>Current Settings</b>\n",
        "Role: " + _e(f.get("role", "not set")),
        "Location: " + _e(f.get("location", "any")),
        "Remote: " + _e(f.get("remote", "any")),
        "Min salary: " + _e(f.get("min_salary", 0) or "none"),
        "Blacklist: " + _e(blacklist),
        "Notify frequency: " + _e(user.notify_freq or "daily"),
        "Min fit score: " + _e(user.min_fit_score) + "%",
        "Status: " + ("paused" if f.get("paused") else "active"),
        "\nRun /start to update everything, or /filters to update just your filters.",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.telegram_id == tg_id))
        user = result.scalar_one_or_none()

    if not user:
        await update.message.reply_text("Run /start first.")
        return

    f = user.filters or {}
    blacklist = ", ".join(f.get("blacklist", [])) or "none"
    msg = (
        "<b>Current Filters</b>\n\n"
        + "Role: " + _e(f.get("role", "not set")) + "\n"
        + "Location: " + _e(f.get("location", "any")) + "\n"
        + "Remote: " + _e(f.get("remote", "any")) + "\n"
        + "Min salary: " + _e(f.get("min_salary", 0) or "none") + "\n"
        + "Blacklist: " + _e(blacklist) + "\n\n"
        + "To update, run /start (it preserves your skill graph)."
    )
    await update.message.reply_text(msg, parse_mode="HTML")


async def cmd_deleteskill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /deleteskill Python"""
    tg_id = str(update.effective_user.id)
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: /deleteskill SkillName\n\nExample: /deleteskill PHP\n\nRun /skills to see your full list."
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
                    "No skill matching '" + _e(skill_name) + "'. Run /skills to see your list.",
                    parse_mode="HTML",
                )
                return
            await session.delete(node)

    await update.message.reply_text(
        "Removed <b>" + _e(node.skill_name) + "</b> from your skill graph.",
        parse_mode="HTML",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>HireLoop Commands</b>\n\n"
        "/start -- onboarding wizard (or re-run to update)\n"
        "/skills -- view your skill graph\n"
        "/addskills -- add skills or upload a new resume\n"
        "/settings -- view all preferences\n"
        "/filters -- view current job filters\n"
        "/pause -- pause or resume job hunting\n"
        "/deleteskill -- remove a skill by name\n"
        "/cancel -- cancel current operation\n"
        "/help -- this message",
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD,
    )


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Resend the main keyboard — useful after bot restarts."""
    await update.message.reply_text("Here's your menu.", reply_markup=MAIN_KEYBOARD)


def get_settings_handlers():
    return [
        CommandHandler("skills", cmd_skills),
        CommandHandler("deleteskill", cmd_deleteskill),
        CommandHandler("pause", cmd_pause),
        CommandHandler("settings", cmd_settings),
        CommandHandler("filters", cmd_filters),
        CommandHandler("help", cmd_help),
        CommandHandler("menu", cmd_menu),
    ]
