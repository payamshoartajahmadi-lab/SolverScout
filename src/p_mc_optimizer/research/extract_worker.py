"""Isolated document parser, run with a clean environment and a wall timeout."""

from __future__ import annotations

import io
import json
import re
import sys
from html.parser import HTMLParser


class PageParser(HTMLParser):
    _hidden = {"script", "style", "noscript", "template", "svg", "head"}
    _blocks = {"address", "article", "br", "div", "h1", "h2", "h3", "h4", "li", "p", "section", "tr"}

    def __init__(self, limit: int, link_limit: int = 0):
        super().__init__()
        self.parts: list[str] = []
        self.length = 0
        self.limit = limit
        self.hidden: list[str] = []
        self.meta: dict[str, str] = {}
        self.login = False
        self.link_limit = link_limit
        self.links: dict[str, None] = {}
        self.title = ""
        self.in_title = False
        self.language: str | None = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "a" and not self.hidden and len(self.links) < self.link_limit:
            href = values.get("href")
            if href and len(href) <= 4096:
                self.links[href] = None
        if tag == "title":
            self.in_title = True
        if tag == "html":
            self.language = (values.get("lang") or "")[:32] or None
        if tag == "meta":
            key = values.get("name") or values.get("property") or ""
            if key.lower() in {
                "citation_publication_date", "citation_date", "article:published_time",
                "article:modified_time", "isaccessibleforfree",
            }:
                self.meta[key.lower()] = (values.get("content") or "")[:100]
        if tag == "input" and (values.get("type") or "").lower() == "password":
            self.login = True
        if tag in self._hidden:
            self.hidden.append(tag)
        elif tag in self._blocks:
            self.handle_data("\n\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if self.hidden and self.hidden[-1] == tag:
            self.hidden.pop()
        elif tag in self._blocks:
            self.handle_data("\n\n")

    def handle_data(self, data):
        if self.in_title and len(self.title) < 500:
            self.title += data[:500 - len(self.title)]
        if not self.hidden and self.length <= self.limit:
            part = data[: self.limit - self.length + 1]
            self.parts.append(part)
            self.length += len(part)


def _pdf(payload: bytes, limit: int) -> dict:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(payload), strict=True)
    if reader.is_encrypted:
        return {"error": "SOURCE_ACCESS_RESTRICTED"}
    pages = []
    length = 0
    truncated = False
    missing_page_text = False
    for page in reader.pages:
        text = (page.extract_text() or "").strip()
        if text:
            remaining = limit - length - (2 if pages else 0)
            truncated = len(text) > remaining
            pages.append(text[:max(0, remaining)])
            length += len(pages[-1]) + (2 if len(pages) > 1 else 0)
            if truncated:
                break
        else:
            missing_page_text = True
    if not any(pages):
        return {"error": "SOURCE_PDF_NO_TEXT"}
    return {"text": "\n\n".join(pages), "truncated": truncated or missing_page_text}


def parse(payload: bytes, content_type: str, level: str, limit: int, link_limit: int = 0) -> dict:
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type == "application/pdf":
        return _pdf(payload, limit)
    charset = re.search(r"charset\s*=\s*[\"']?([a-zA-Z0-9_-]{1,32})", content_type, re.I)
    encoding = charset.group(1) if charset else "utf-8"
    decoded = payload.decode(encoding, errors="replace")
    if media_type in {"text/html", "application/xhtml+xml"}:
        parser = PageParser(limit, link_limit)
        parser.feed(decoded)
        parser.close()
        if parser.login:
            return {"error": "SOURCE_LOGIN_REQUIRED"}
        if parser.meta.get("isaccessibleforfree", "").lower() in {"false", "0"}:
            return {"error": "SOURCE_PAYWALL"}
        text = re.sub(r"[^\S\n]+", " ", "".join(parser.parts))
        text = re.sub(r" *\n[ \n]*", "\n\n", text).strip()
        return {
            "text": text[:limit], "truncated": parser.length > limit, "meta": parser.meta,
            "title": parser.title.strip(), "language": parser.language, "links": list(parser.links),
        }
    if media_type == "text/plain":
        return {"text": decoded[:limit].strip(), "truncated": len(decoded) > limit}
    return {"error": "SOURCE_UNSUPPORTED_CONTENT"}


def main():
    content_type, level, limit, *link_limit = sys.argv[1:]
    try:
        result = parse(sys.stdin.buffer.read(), content_type, level, int(limit), int(link_limit[0]) if link_limit else 0)
    except Exception:
        result = {"error": "SOURCE_EXTRACTION_FAILED"}
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
