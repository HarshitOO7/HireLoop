<p align="center">
  <h1 align="center">HireLoop</h1>
  <p align="center">
    <b>An autonomous job-hunting agent that runs in Telegram, thinks with AI, and never applies without your approval.</b>
  </p>
  <p align="center">
    <img src="https://img.shields.io/badge/python-3.11+-blue?style=flat-square&logo=python&logoColor=white" />
    <img src="https://img.shields.io/badge/interface-Telegram-2CA5E0?style=flat-square&logo=telegram&logoColor=white" />
    <img src="https://img.shields.io/badge/AI-provider--agnostic-8A2BE2?style=flat-square" />
    <img src="https://img.shields.io/badge/database-SQLite-003B57?style=flat-square&logo=sqlite&logoColor=white" />
    <img src="https://img.shields.io/badge/status-active%20%C2%B7%20self--hosted-brightgreen?style=flat-square" />
    <img src="https://img.shields.io/badge/license-MIT-green?style=flat-square" />
  </p>
</p>

<!--
TODO(demo): record a ~15s screen capture of the Telegram flow — /fetchnow → a job card
with fit score → tap approve → resume .docx delivered — and save it to docs/demo.gif.
A "show me it works" clip is the single highest-signal thing on this page.
-->
<p align="center"><img src="docs/demo.gif" alt="HireLoop Telegram demo" width="640" /></p>

---

## What it does

Applying to jobs by hand is slow and repetitive: read the posting, judge your fit, re-tailor your resume, reformat it so an ATS won't mangle it, apply, log it, repeat. HireLoop does the grunt work and keeps you in the loop for the decisions that matter.

It scrapes fresh postings on a schedule, scores each against a **skill graph** it builds from your resume, generates a per-job tailored resume, and asks for your approval before anything represents you. In practice it turns ~20–30 minutes of manual tailoring per application into a few taps in a Telegram chat — an ~80% cut in hands-on time (personal-use estimate, not a benchmark).

**Who it's for:** a single operator (me). It's self-hosted on Oracle Cloud's Always-Free tier, Telegram-first, and runs 24/7.

**Non-negotiables, enforced in code:**
- No skill is claimed without your explicit confirmation.
- No resume is generated-and-sent without your approval.
- No application fires without per-job sign-off.

---

## Architecture

The agent is a pipeline with **human-in-the-loop gates** at the two points where trust matters — what skills you claim, and what resume goes out.

```
                 ┌──────────────── APScheduler loop (08:00 / 18:00, your timezone) ───────────────┐
                 ▼                                                                                 │
  Scrape (JobSpy)  →  Filter + dedup  →  LLM parse + fit-score  →  [ HITL: verify skills ]         │
   Indeed/LinkedIn/    stale / seen /      cheap model, per job        confirm · add context ·      │
   Glassdoor           company cooldown                                remove                       │
                                                                            │                       │
                                                                            ▼                       │
        deliver .docx / PDF  ←  [ HITL: approve resume ]  ←  LLM tailor resume  ←  skill evidence ───┘
              + log                approve · edit · skip      premium model         injected from graph
```

**Provider-agnostic LLM layer.** Every model call goes through an `AIProvider` interface (`ai/base.py`) built by a factory from env vars (`ai/factory.py`). Providers implemented: Anthropic, OpenAI, Gemini, Groq, DeepSeek, Grok (xAI), Ollama. Calls are routed across three tiers so cost tracks stakes:
- **Fast** (bulk, cheap) — parse every scraped job, score fit, expand role titles.
- **Quality** (high-stakes) — tailor the resume that actually goes out, write cover letters, answer screening questions.
- **Fallback** — a second quality provider, used automatically when the primary errors.

Swapping any slot is one line in `.env` — no code change. This exists because provider cost, latency, and availability move constantly; being able to arbitrage them (and survive one going down) matters more than betting on a single vendor.

**Retrieval → generation.** Confirmed skills and your own words about them are persisted as a structured **skill graph** (`SkillNode` + `SkillEvidence`). On each new job, the relevant evidence is retrieved and injected into the resume prompt — you explain "Kafka at Acme, 8 months, async order pipeline" once, and every future Kafka job reuses it. It's retrieval-augmented generation grounded in your own verified experience.

**Guardrails & observability — what gets checked before output ships.**
- **Anti-hallucination:** degree, dates, and GPA are extracted once and injected as a locked `<facts>` block into every tailoring call, so the model can't invent them; global "humanization" rules strip AI tells (em-dashes, buzzwords).
- **Scraper health checks:** per-board result counts are recorded every run; a `/health` command shows each board's status, and you get a DM if a board returns nothing 3 scrapes in a row (a real IP block looks exactly like "no results" otherwise).
- **Schema self-reconciliation:** on boot, any model column missing from the live DB is added automatically — a class of silent write-failure bug (see below) can't recur.

**How the agent takes actions:** a typed tool layer in Python — the JobSpy scraper, the `.docx`/PDF renderers, and per-board scraping adapters, each a narrow surface the pipeline calls to do real work.

---

## Tech stack

Honest to the repo (`requirements.txt`):

- **Language/runtime:** Python 3.11, fully async.
- **Interface:** `python-telegram-bot` (long-polling — no web server exposed).
- **Data:** SQLAlchemy async + **SQLite** (`aiosqlite`), Alembic migrations.
- **LLMs:** Anthropic · OpenAI · Gemini · Groq · DeepSeek · Grok · Ollama, behind one interface.
- **Scraping:** JobSpy (`python-jobspy`) + a `curl-cffi` patch for Glassdoor's Cloudflare/geo-redirect.
- **Scheduling:** APScheduler (embedded in the bot process).
- **Resume output:** `python-docx` (ATS-controlled `.docx`, primary) + `reportlab` (PDF on request).
- **Ops:** Docker · **Litestream** (streams SQLite to OCI Object Storage for disaster recovery) · GitHub Actions CI/CD · OCI Ampere A1 (Always-Free).

---

## How LLMs fail here

For several days, **every scraped job scored 0% fit**, with fluent, confident reasoning like *"candidate has no skills listed; all required skills are missing — very poor fit."*

That output is the trap: it is **indistinguishable from a correct low-fit verdict.** The model wasn't broken. It was faithfully scoring an input that had silently gone empty. Two bugs were compounding underneath:

1. Editing filters ran an onboarding-save path that **wiped the entire skill graph** (a SQLAlchemy in-place-JSON mutation that never persisted, plus a save with an empty skill list). The model was reasoning over a candidate with zero skills.
2. **Every job INSERT was failing** on a DB column the model had but the deployed table didn't — so the "0% jobs" I kept inspecting were actually error tracebacks, not saved results.

The lesson that shaped the rest of the system: **plausible LLM output is the most dangerous failure mode.** A crash gets caught; a confident, well-reasoned wrong answer sails straight through, because nothing *looks* wrong. You cannot eval your way out of this at the model layer — the model was correct. You need observability into the model's **inputs and the pipeline around it.**

What changed as a direct result: the scraper **health checks + alerts** (is data even arriving?), the **schema self-reconciliation** on boot (so a missing column can't silently kill every write again), and leaning harder on the **human-in-the-loop gates** — the skill-verify step surfaces the exact profile the model is about to reason over, so an empty one is visible to a human before it ever reaches scoring.

---

## Setup

**Prerequisites:** Python 3.11+, a Telegram bot token from [@BotFather](https://t.me/BotFather), and at least one AI provider key.

```bash
git clone https://github.com/yourname/hireloop.git
cd hireloop
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # then edit — minimum below
```

```env
TELEGRAM_BOT_TOKEN=from_botfather
AI_FAST_PROVIDER=deepseek                 # bulk/cheap: parse + score
AI_FAST_API_KEY=your_key
AI_QUALITY_PROVIDER=anthropic             # high-stakes: the resume that ships
AI_QUALITY_API_KEY=your_key
AI_FALLBACK_PROVIDER=grok                 # optional quality backup
AI_FALLBACK_API_KEY=your_key
DATABASE_URL=sqlite:///hireloop.db
ALLOWED_TELEGRAM_IDS=                     # comma-separated IDs; blank = open
```

```bash
python bot/main.py
```

The schema is created on first run. Open Telegram, find your bot, send `/start`. Any provider slot swaps with a one-line `.env` change (defaults: `deepseek-chat`, `claude-sonnet-4-6`, `grok-3-fast`). Deploy notes and internals live in [`CLAUDE.md`](CLAUDE.md).

---

## Design decisions

- **Human-in-the-loop over full autonomy.** The gates aren't a limitation, they're the point: this resume represents *me*. The agent does everything up to the decision, then a human signs off. It also makes failures debuggable — the skill-verify gate is where the "0% fit" bug above becomes visible.
- **Provider-agnostic + tiered routing.** A cheap model runs 50+ times a day (parse, score); a premium model runs only on the artifact that actually ships. Cost tracks stakes, and a fallback provider means one vendor outage doesn't stop the pipeline.
- **SQLite + Litestream.** A full relational store with a durable backup and no database server to run: $0 to host for a single operator, streamed to object storage every few minutes for disaster recovery. The schema stays portable if it ever needs to scale to many users.
- **Resume as Markdown, rendered on demand.** The tailored resume is stored as Markdown (LLM-native, tiny in the DB) and rendered to `.docx`/PDF only when requested — `python-docx` (not pandoc) so ATS-critical styles are controlled exactly and Markdown tables never become Word tables.

---

## Roadmap & known limitations

**Planned**
- Embedding / vector-based fit scoring (semantic RAG over the skill graph).
- Multi-user support (Postgres migration — the schema is already portable).
- Auto-apply via Playwright (Workday / Greenhouse / Lever) with screenshot proof.
- Recruiter finder, Gmail outcome loop, and a web dashboard.

**Known limitations (honest)**
- Scraping depends on boards that tolerate a datacenter IP; LinkedIn is flaky, Google Jobs is skipped, Indeed/Glassdoor are the reliable ones.
- Single-user by design — no auth/multi-tenancy yet.
- No formal eval harness yet; correctness relies on the guardrails and human gates above.

---

## License

MIT — do whatever you want, but don't blame me if your resume gets too good.
