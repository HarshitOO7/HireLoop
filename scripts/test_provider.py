"""
Week 1 smoke test — run from project root:
  python scripts/test_provider.py

Tests both fast and quality providers against a dummy JD + skill profile.
Done when: both providers respond with valid JSON fit analysis.
"""

import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from ai.factory import AIFactory
from ai.service import HireLoopAI

DUMMY_JD = """
Software Engineer — Backend (Python)
Acme Corp · Toronto, ON · Remote-friendly

We're looking for a backend engineer with:
- 3+ years Python (required)
- FastAPI or Django (required)
- PostgreSQL (required)
- Redis experience (preferred)
- Kafka knowledge (nice to have)
- Strong async/await understanding

Please include a cover letter explaining your interest in Acme Corp.
Salary: $90,000–$120,000 CAD

Contact: Jane Smith, jane@acme.com
"""

DUMMY_PROFILE = {
    "skills": [
        {"skill_name": "Python", "status": "verified_resume", "confidence": "high"},
        {"skill_name": "FastAPI", "status": "verified_attested", "confidence": "high"},
        {"skill_name": "PostgreSQL", "status": "verified_resume", "confidence": "medium"},
        {"skill_name": "Redis", "status": "partial", "confidence": "low"},
        {"skill_name": "React", "status": "verified_resume", "confidence": "medium"},
    ],
    "variant_tags": ["backend", "fullstack"],
}


def section(title: str):
    print(f"\n{'-' * 50}")
    print(f"  {title}")
    print('-' * 50)


async def main():
    section("HireLoop Week 1 - Provider Smoke Test")

    fast = AIFactory.create_fast()
    quality = AIFactory.create_quality()

    print(f"  Fast provider   : {fast.provider_name}")
    print(f"  Quality provider: {quality.provider_name}")

    ai = HireLoopAI(fast_provider=fast, quality_provider=quality)

    # Test 1: parse_job (fast provider)
    section("1. parse_job() — fast provider")
    try:
        parsed = await ai.parse_job(DUMMY_JD)
        assert "title" in parsed, "Missing 'title' in parse_job result"
        assert "requires_cover_letter" in parsed, "Missing 'requires_cover_letter'"
        print(json.dumps(parsed, indent=2))
        print("  [PASS] parse_job")
    except Exception as e:
        print(f"  [FAIL] parse_job: {e}")
        sys.exit(1)

    # Test 2: analyze_fit (fast provider)
    section("2. analyze_fit() — fast provider")
    try:
        fit = await ai.analyze_fit(parsed, DUMMY_PROFILE)
        assert "fit_score" in fit, "Missing 'fit_score'"
        assert "action" in fit, "Missing 'action'"
        assert isinstance(fit["fit_score"], (int, float)), "fit_score must be numeric"
        print(json.dumps(fit, indent=2))
        print(f"  [PASS] analyze_fit  (score={fit['fit_score']}, action={fit['action']})")
    except Exception as e:
        print(f"  [FAIL] analyze_fit: {e}")
        sys.exit(1)

    # Test 3: tailor_resume (quality provider)
    section("3. tailor_resume() — quality provider")
    DUMMY_RESUME = """
# Alex Developer
alex@example.com | github.com/alex

## Experience
**Senior Backend Engineer — StartupXYZ** (2022–Present)
- Built FastAPI microservices handling 10k req/s
- Designed PostgreSQL schemas for financial data

**Backend Engineer — OldCo** (2020–2022)
- Python/Django REST APIs
- Redis caching layer

## Skills
Python, FastAPI, Django, PostgreSQL, Redis, Docker
"""
    verified_skills = [s for s in DUMMY_PROFILE["skills"] if s["status"].startswith("verified_")]
    try:
        resume = await ai.tailor_resume(parsed, fit, DUMMY_RESUME, verified_skills)
        assert len(resume) > 100, "Resume output too short"
        print(resume[:600] + "\n  [... truncated ...]")
        print("  [PASS] tailor_resume")
    except Exception as e:
        print(f"  [FAIL] tailor_resume: {e}")
        sys.exit(1)

    section("ALL TESTS PASSED - Week 1 complete")
    print("  Next step: Week 2 - Telegram bot + onboarding\n")


if __name__ == "__main__":
    asyncio.run(main())
