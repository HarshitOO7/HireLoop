"""
Jina Reader wrapper — fetches a job description from any public URL.

Usage:
    text = await fetch_jd_from_url("https://jobs.lever.co/acme/123")
    parsed = await ai.parse_job(text)
"""

import logging

import httpx

logger = logging.getLogger(__name__)

_JINA_BASE = "https://r.jina.ai/"
_TIMEOUT = 30


async def fetch_jd_from_url(url: str) -> str:
    """
    Fetch and return plain-text job description from any URL via Jina Reader.
    Raises httpx.HTTPError on failure.
    """
    jina_url = f"{_JINA_BASE}{url}"
    logger.info("[parser] fetching via Jina Reader: %s", url)

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(jina_url, headers={"Accept": "text/plain"})
        resp.raise_for_status()

    text = resp.text.strip()
    logger.info("[parser] fetched %d chars from %s", len(text), url)
    return text
