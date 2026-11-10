"""Local extraction without executing source content."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .transport import ResearchError


PARSER_VERSION = "htmlparser-python3.12+pypdf7.0.0+protego0.7.0-v2.0.1"


def extract(payload: bytes, content_type: str, level: str, limit: int, timeout: float, *, link_limit: int = 0) -> dict:
    if timeout <= 0:
        raise ResearchError("RESEARCH_BUDGET_EXHAUSTED")
    try:
        result = subprocess.run(
            [
                sys.executable, "-I", str(Path(__file__).with_name("extract_worker.py")),
                content_type[:250], level, str(limit), str(link_limit),
            ],
            input=payload, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout, check=True, env={},
        )
        parsed = json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        raise ResearchError("SOURCE_PARSER_TIMEOUT") from None
    except (OSError, subprocess.CalledProcessError, ValueError):
        raise ResearchError("SOURCE_EXTRACTION_FAILED") from None
    if parsed.get("error"):
        raise ResearchError(parsed["error"])
    if not parsed.get("text", "").strip():
        raise ResearchError("SOURCE_EMPTY_TEXT")
    return parsed


def metadata_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("/", "-"))
    except ValueError:
        return None
    if parsed.tzinfo is None and len(value) != 10:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
