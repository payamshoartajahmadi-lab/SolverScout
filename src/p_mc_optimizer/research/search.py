"""Tavily implementation behind a provider-independent search result contract."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import httpx

from .transport import ResearchError, request


TAVILY_ORIGIN = "https://api.tavily.com"
TAVILY_ENDPOINT = f"{TAVILY_ORIGIN}/search"
_MAX_RESPONSE = 1024 * 1024



def _retry_timestamp(value: str | None) -> str | None:
    if not value or len(value) > 120:
        return None
    try:
        if value.isascii() and value.isdecimal():
            timestamp = datetime.now(timezone.utc) + timedelta(seconds=min(int(value), 365 * 86400))
        else:
            timestamp = parsedate_to_datetime(value)
            if timestamp.tzinfo is None:
                return None
        return timestamp.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError):
        return None


class TavilySearch:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None):
        self.transport = transport

    def search(
        self,
        text: str,
        domains: tuple[str, ...],
        count: int,
        timeout: float,
        credential: str,
        sanitize: Callable[[str], str],
    ) -> list[dict]:
        if not domains or len(domains) > 300:
            raise ResearchError("SEARCH_SCOPE_UNSUPPORTED")
        payload = {
            "query": text,
            "include_domains": list(domains),
            "include_domains_mode": "restrict",
            "max_results": count,
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
            "include_published_date": True,
            "auto_parameters": False,
        }
        try:
            response = request(
                "POST", TAVILY_ENDPOINT, timeout, _MAX_RESPONSE,
                {
                    "Authorization": f"Bearer {credential}",
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip, identity",
                },
                body=payload, transport=self.transport,
            )
        except (TimeoutError, httpx.TimeoutException):
            raise ResearchError("SEARCH_TIMEOUT") from None
        except httpx.HTTPError:
            raise ResearchError("SEARCH_OUTCOME_UNKNOWN") from None
        if response.status in {401, 403}:
            raise ResearchError("SEARCH_AUTH_FAILED")
        if response.status == 429:
            retry = _retry_timestamp(response.headers.get("Retry-After"))
            raise ResearchError("SEARCH_RATE_LIMITED", retry_after=retry)
        if response.status in {400, 422}:
            raise ResearchError("SEARCH_SCOPE_UNSUPPORTED")
        if response.status != 200:
            raise ResearchError("SEARCH_UNREACHABLE")
        try:
            parsed = json.loads(response.body)
        except (ValueError, UnicodeError):
            raise ResearchError("SEARCH_INVALID_RESPONSE") from None
        if not isinstance(parsed, dict) or not isinstance(parsed.get("results"), list):
            raise ResearchError("SEARCH_INVALID_RESPONSE")

        def clean(item: dict, field: str, limit: int) -> str:
            value = item.get(field)
            return sanitize(value)[:limit] if isinstance(value, str) else ""

        return [
            {
                "url": clean(item, "url", 4096),
                "title": clean(item, "title", 400),
                "snippet": clean(item, "content", 1200),
                "published_at": clean(item, "published_date", 100),
                "provider_rank": rank,
            }
            for rank, item in enumerate(parsed["results"][:count], start=1)
            if isinstance(item, dict) and isinstance(item.get("url"), str) and item["url"]
        ]
