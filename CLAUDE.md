# HireLoop — Claude Code Context
# Drop this file as CLAUDE.md in your project root.
# Claude Code reads it automatically on every session.

---

## What We're Building

HireLoop — a human-in-the-loop autonomous job hunting agent.

- Finds jobs → analyzes fit → verifies skills with YOU → generates tailored resume → you approve → (Phase 3) auto-applies
- Telegram is the PRIMARY interface — all triggers, approvals, uploads via Telegram bot
- Web dashboard comes in Phase 3 only — do NOT build it now
- No skill is ever claimed without explicit user confirmation
- No resume is sent without user approval
- No application fires without per-job approval

---

## Project Structure

```
hireloop/
├── CLAUDE.md
├── .env.example
├── .env                           # gitignored
├── .gitignore
├── requirements.txt
├── docker-compose.yml
│
├── ai/                            # AI provider abstraction layer
│   ├── base.py                    # Abstract AIProvider class
│   ├── factory.py                 # AIFactory.create_fast() / create_quality()
│   ├── service.py                 # HireLoopAI — all 6 agent tasks (tiered)
│   ├── __init__.py
│   └── providers/
│       ├── anthropic_provider.py
│       ├── openai_provider.py
│       ├── gemini_provider.py
│       ├── groq_provider.py       # Fast/cheap — default for bulk tasks
│       ├── ollama_provider.py     # Local free option
│       └── __init__.py
│
├── db/
│   ├── models.py                  # SQLAlchemy — skill graph schema
│   ├── session.py
│   └── migrations/                # Alembic
│
├── bot/
│   ├── main.py
│   ├── onboarding.py              # 6-step Telegram wizard
│   ├── keyboards.py               # All InlineKeyboardMarkup builders
│   └── handlers/
│       ├── resume_upload.py
│       ├── skill_verify.py
│       ├── job_approval.py
│       └── settings.py
│
├── jobs/
│   ├── scraper.py                 # JobSpy wrapper
│   ├── parser.py                  # Jina Reader for pasted URLs
│   ├── filters.py
│   └── scheduler.py               # APScheduler — embedded in bot process
│
├── resume/
│   ├── generator.py               # tailor_resume() → store Markdown TEXT in DB
│   ├── section_order.py           # infer section order from profile (zero tokens, pure Python)
│   ├── docx_export.py             # Markdown → .docx via python-docx (ATS-safe, primary)
│   ├── pdf_export.py              # Markdown → PDF via reportlab (on-request) ✅
│   ├── variants/                  # base resume markdown files
│   └── output/                    # generated files (gitignored)
│
├── tests/
└── scripts/
    ├── test_provider.py           # AI provider smoke test
    └── test_resume_generation.py  # Resume gen E2E: real DB copy → generate → edit → DOCX
```

---

## Environment Variables

```env
# Fast provider — cheap/fast — used for scraping + fit scoring (high volume)
AI_FAST_PROVIDER=groq
AI_FAST_API_KEY=your_groq_key
AI_FAST_MODEL=                          # blank = use provider default

# Quality provider — best model — used for resume + cover letter generation
AI_QUALITY_PROVIDER=anthropic
AI_QUALITY_API_KEY=your_anthropic_key
AI_QUALITY_MODEL=                       # blank = use provider default

AI_MAX_TOKENS=4096
AI_TEMPERATURE=0.3

# Defaults per provider:
# anthropic  → claude-sonnet-4-6
# openai     → gpt-4o
# gemini     → gemini-2.0-flash
# groq       → llama-3.3-70b-versatile
# ollama     → llama3.2 (local, free)

TELEGRAM_BOT_TOKEN=from_botfather
SERPAPI_KEY=  # unused for now (Google Jobs scraping broken, revisit Phase 2)
DATABASE_URL=sqlite:///hireloop.db
OLLAMA_HOST=http://localhost:11434
```

---

## AI Provider System — Tiered Architecture

Two provider slots: **fast** (bulk/cheap) and **quality** (resume/cover letter).

```python
# Instantiate both providers from .env
fast    = AIFactory.create_fast()      # e.g. Groq llama-3.3-70b
quality = AIFactory.create_quality()   # e.g. Anthropic claude-sonnet-4-6

ai = HireLoopAI(fast_provider=fast, quality_provider=quality)

# High volume — uses fast provider (cheap)
job     = await ai.parse_job(raw_jd_text)
skills  = await ai.parse_resume(resume_text)
fit     = await ai.analyze_fit(job, user_profile)

# High stakes — uses quality provider (best)
resume  = await ai.tailor_resume(job, fit, base_resume, verified_skills)
cl      = await ai.write_cover_letter(job, user_profile, fit)
answers = await ai.answer_screening_questions(questions, job, user_profile)
```

### Task → Provider mapping
| Task | Provider | Reason |
|---|---|---|
| parse_job | fast | Runs on every scraped job (50+/day) |
| parse_resume | fast | One-time, not quality-critical |
| analyze_fit | fast | Runs on every job above threshold |
| tailor_resume | quality | Goes on your resume — must be best |
| write_cover_letter | quality | Represents you to recruiters |
| answer_screening_questions | quality | High-stakes interview gating |

### Adding a new provider
1. Create `ai/providers/myprovider.py`
2. Subclass `AIProvider` from `ai/base.py`
3. Implement `complete()` and `provider_name`
4. Add to `AIFactory._build()` match statement in `factory.py`
5. Add to `_DEFAULT_MODELS` dict

---

## Database Schema — Skill Graph

Build in `db/models.py` using SQLAlchemy async.

```python
class User(Base):
    __tablename__ = "users"
    id              = Column(String, primary_key=True)  # UUID
    telegram_id     = Column(String, unique=True)
    name            = Column(String)
    filters         = Column(JSON)     # role, location, salary, remote, blacklist
    notify_freq     = Column(String)   # "daily" | "realtime"
    min_fit_score   = Column(Integer, default=60)
    daily_app_limit = Column(Integer, default=5)
    onboarded       = Column(Boolean, default=False)
    created_at      = Column(DateTime)

class SkillNode(Base):
    __tablename__ = "skill_nodes"
    id           = Column(Integer, primary_key=True)
    user_id      = Column(String, ForeignKey("users.id"))
    skill_name   = Column(String)
    # verified_resume | verified_attested | partial | gap
    status       = Column(String)
    # high | medium | low — from resume parse
    confidence   = Column(String)
    created_at   = Column(DateTime)
    updated_at   = Column(DateTime)

class SkillEvidence(Base):
    __tablename__ = "skill_evidence"
    id               = Column(Integer, primary_key=True)
    skill_node_id    = Column(Integer, ForeignKey("skill_nodes.id"))
    company          = Column(String)
    role_title       = Column(String)
    duration_months  = Column(Integer)
    last_used_year   = Column(Integer)
    user_context     = Column(Text)   # user's own words
    generated_bullet = Column(Text)   # Claude's polished bullet
    source           = Column(String) # "resume" | "telegram" | "manual"

class Job(Base):
    __tablename__ = "jobs"
    id                    = Column(String, primary_key=True)  # UUID
    user_id               = Column(String, ForeignKey("users.id"))
    title                 = Column(String)
    company               = Column(String)
    url                   = Column(String)
    raw_jd                = Column(Text)
    parsed                = Column(JSON)    # output of parse_job()
    fit_score             = Column(Float)
    cover_letter_required = Column(Boolean, default=False)
    recruiter_name        = Column(String)  # Phase 2
    recruiter_linkedin    = Column(String)  # Phase 2
    # pending|skill_verify|approved|skipped|applied
    status                = Column(String)
    created_at            = Column(DateTime)

class Application(Base):
    __tablename__ = "applications"
    id                 = Column(Integer, primary_key=True)
    job_id             = Column(String, ForeignKey("jobs.id"))
    resume_path        = Column(String)
    cover_letter_path  = Column(String)
    applied_at         = Column(DateTime)
    # interview | rejected | ghosted | offer
    outcome            = Column(String)
    outcome_source     = Column(String)  # email | manual | telegram
    outcome_at         = Column(DateTime)
```

---

## Telegram Bot

### Commands (register with @BotFather)
```
/start     - Onboarding wizard (or re-run)
/skills    - View skill graph summary
/resume    - Upload new resume version
/jobs      - Pending jobs waiting for action
/history   - Past applications + outcomes
/settings  - Edit all preferences
/filters   - Quick filter access
/pause     - Pause job hunting
/help      - Full command list
```

### Persistent keyboard (always visible)
```python
MAIN_KEYBOARD = ReplyKeyboardMarkup([
    ["📎 Add Resume",   "🎛️ Edit Filters"],
    ["📊 My Skills",    "📋 Pending Jobs"],
    ["⏸ Pause Agent",  "⚙️ Settings"],
], resize_keyboard=True)
```

---

## Onboarding Flow (6 ConversationHandler states)

```
STATE: WELCOME
  Send welcome message + [✅ Let's go] button

STATE: UPLOAD_RESUME
  "Send 1–4 resume PDFs or Word docs"
  On each document: download → extract text → call ai.parse_resume()
  Button: [✅ Done uploading]

STATE: CONFIRM_SKILLS
  Show extracted skills grouped by confidence
  High confidence: auto-confirmed
  Medium/Low: show [✅ Confirm] [✏️ Add context] [❌ Remove] per skill
  If "Add context": ask for one sentence → save as user_context
  Save all confirmed to SkillNode + SkillEvidence

STATE: SET_FILTERS
  Ask role, location, remote pref, min salary, blacklist companies
  Save to user.filters JSON

STATE: SET_FREQUENCY
  [📬 Daily digest] [⚡ Real-time] [2x per day]
  Min fit score: [50%] [60%] [70%] [80%]

STATE: DONE
  "All set! Running first job search now..."
  Trigger first scrape
```

---

## Job Notification Format (send this in Telegram)

Default card is CONDENSED — never dump the full JD on the user.
"View Full JD" button sends the full posting as a separate message on demand.

```
🏢 {title}
{company} · {location} · {salary_or_range}

Fit Score: {score}% · {action_label}

✅ Matched: Python, FastAPI, PostgreSQL
❓ Gaps: Django (required), Kafka (preferred)

[✅ I know these] [⏭ Skip] [📄 Full JD] [🔗 Open Link]
```

### [📄 Full JD] button behaviour
Sends a follow-up message (does NOT replace the card):
```
📄 {title} @ {company}

{raw_jd, truncated to 3000 chars if longer}

🔗 {url}
```

### After user confirms skills
- Parse context → update SkillNode → create SkillEvidence
- Recalculate fit score
- "Updated! New score: X%. Generating resume..."

---

## Cover Letter Logic — CRITICAL

ONLY generate a cover letter when one of these is true:
1. job.cover_letter_required = True (JD mentioned it)
2. User explicitly tapped [📝 Add Cover Letter] button

NEVER auto-generate without one of those triggers.

Detection in parse_job() scans for:
"cover letter", "covering letter", "letter of motivation", "please include"

---

## Skill Graph — Evidence Travels Automatically

Once a skill is confirmed with context, it generates bullets forever:

```python
# User confirms: "Kafka at Acme Corp, 8 months, order processing pipeline"
# Saved as:
SkillNode(skill_name="Apache Kafka", status="verified_attested")
SkillEvidence(company="Acme Corp", duration_months=8,
              user_context="built async order processing pipeline")

# Next Kafka job → Claude auto-writes:
# "Designed Kafka-based event pipeline for order processing at Acme Corp (8 months)"
# User never explains Kafka again.
```

---

## Job Scraping

### Primary — JobSpy (free, use this)
```python
from jobspy import scrape_jobs

jobs = scrape_jobs(
    site_name=["indeed", "linkedin", "glassdoor"],
    search_term=user_filters["role"],
    location=user_filters["location"],
    is_remote=user_filters.get("remote_only", False),
    results_wanted=25,
    hours_old=24,
)
```

### User-pasted URLs — Jina Reader
```python
import httpx
async def fetch_job_from_url(url: str) -> str:
    async with httpx.AsyncClient() as client:
        r = await client.get(f"https://r.jina.ai/{url}", timeout=30)
        return r.text
```

Filters run BEFORE passing jobs to fit analysis:
- Salary minimum
- Blacklisted companies/industries
- Seniority match
- Deduplicate by URL hash
- Fit score threshold (skip notification if below user.min_fit_score)

---

## Phase 1 Build Order

### Phase 1 — Complete ✅

All features shipped and tested:

- **Foundation**: AI provider layer (5 providers), factory, tiered routing, in-memory cache
- **Database**: SQLAlchemy async, skill graph schema (User · SkillNode · SkillEvidence · Job · Application)
- **Telegram bot**: onboarding wizard (12 states), skill verify, job approval, settings, /addskills
- **Job scraper**: JobSpy (Indeed ✅ · LinkedIn ⚠️ partial · Glassdoor ✅ · Google ❌ skipped), Jina URL paste, APScheduler, dedup
- **Resume pipeline**: generator, section order (pure Python), DOCX export (python-docx), PDF export (reportlab)
- **Edit loop**: patch_resume() + apply_patch() → AI-targeted section edits
- **Quality controls**: input caps (two-layer), standing instructions, experience filtering (max 4 entries, drop >10yr)
- **Application tracking**: /myapps history, outcome logging

Test scripts:
- `python scripts/test_provider.py`           — AI provider smoke test
- `python scripts/test_resume_generation.py`  — resume gen E2E (real DB copy, interactive approve/edit loop)

---

## Coding Rules — Always Follow These

1. Always use async/await — bot and AI calls are all async
2. Never hardcode a provider — always AIFactory.create_fast()/create_quality()
3. Never include a skill in a resume unless SkillNode.status is "verified_*"
4. Never generate a cover letter unless job.cover_letter_required=True OR user requested
5. SQLAlchemy models only in db/models.py — no raw SQL strings
6. All InlineKeyboardMarkup in bot/keyboards.py — not inline in handlers
7. ConversationHandler for all multi-step Telegram flows
8. All secrets from .env only — never hardcode
9. Log everything to DB — every job seen, skill verified, application made
10. Filters run BEFORE scraping — pass to JobSpy, don't post-filter a huge list
11. Resume stored as Markdown TEXT in DB — render to .docx (python-docx) or PDF (reportlab) on-demand
12. .docx is primary format (ATS-safe); PDF is on-request only — use python-docx NOT pypandoc (pypandoc converts MD tables → Word tables = ATS killer)
13. Section order inferred from profile via section_order.py — zero tokens, pure Python decision tree
12. Background scheduling via APScheduler AsyncIOScheduler — not n8n

---

## Do NOT Build in Phase 1

- Web dashboard → Phase 3
- Auto-apply / Playwright → Phase 3
- Recruiter finder → Phase 2
- Gmail/email integration → Phase 3
- Multi-user SaaS auth → Phase 2+
- Stripe billing → Phase 2
- Company career page scraping → Phase 2

---

## Phase 2 (after 30 days of real usage)
- Recruiter finder (3-tier: parse JD → LinkedIn search → web search)
- Application rate limiter (daily cap + 30-day same-company cooldown)
- Outcome tracking (interview? reject? offer?)
- Salary intel step before fit analysis
- Multi-user via Telegram Supergroup Topics
- Hosted VPS + Postgres migration

## Phase 3 (6–12 months)
- Auto-apply: Playwright fills Workday/Greenhouse/Lever forms
- Platform classifier (identify ATS before filling)
- Screenshot proof of every submission
- Gmail integration for outcome loop
- Web dashboard (Next.js)
- Supabase multi-tenant auth

---

## Current Status

Phase 1 complete. All features shipped.

Scraper status:
- Indeed ✅
- LinkedIn ⚠️ not fully tested (may have bugs)
- Google Jobs ❌ skipped (JS-only page, JobSpy broken, revisit Phase 2)
- Glassdoor ✅ working via curl_cffi patch (see jobs/glassdoor_patch.py)

### Glassdoor fix summary
Canadian IPs get geo-redirected glassdoor.com → glassdoor.ca, where Cloudflare
blocks tls_client (JobSpy default) with 403.  Fix: monkey-patch in jobs/glassdoor_patch.py
replaces JobSpy's create_session with curl_cffi (Chrome124 impersonation) and tolerates
partial GraphQL errors (SEO-only 503s are non-fatal).  Applied at import time in scraper.py.

### Test scripts
```bash
python scripts/test_provider.py           # AI provider smoke test (fast + quality)
python scripts/test_resume_generation.py  # Resume gen E2E — real DB copy, interactive
```

`test_resume_generation.py` copies hireloop.db → hireloop_test.db, runs generate_resume()
with real user data, shows full logs (section order, AI timing, sections modified on edit),
and provides an interactive [L]ooks good / [E]dit / [S]kip loop with DOCX output.
