"""Isolated document parser, run with a clean environment and a wall timeout."""

from __future__ import annotations

import io
import json
import re
import sys
from html.parser import HTMLParser


class PageParser(HTMLParser):
    _hidden = {"script", "style", "noscript", "template", "svg", "head", "nav", "aside"}
    _blocks = {"address", "article", "br", "div", "h1", "h2", "h3", "h4", "li", "p", "section", "tr", "dt", "dd"}

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
        self.sections: list[dict] = []
        self.dl_depth = 0
        self.class_depth: int | None = None
        self.skip_dl: int | None = None
        self.current: dict | None = None
        self.signature = False
        self.signature_done = False

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if tag == "dl":
            self.dl_depth += 1
            if not self.hidden and self.class_depth is None and {"py", "class"} <= classes:
                self.class_depth = self.dl_depth
                self.current = {"anchor": "", "parts": [], "length": 0, "truncated": False}
                self.signature_done = False
            elif self.current is not None and "py" in classes:
                self.skip_dl = self.skip_dl or self.dl_depth
        if tag == "dt" and self.current is not None and self.dl_depth == self.class_depth and not self.signature_done:
            anchor = values.get("id") or ""
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:-]{0,255}", anchor):
                self.current["anchor"] = anchor
            self.signature = True
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
        hidden = tag in self._hidden or classes.intersection({"toctree-wrapper", "contents", "headerlink", "viewcode-link"}) or values.get("role") == "navigation"
        if self.hidden or hidden:
            if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
                self.hidden.append(tag)
        elif tag in self._blocks:
            self.handle_data("\n\n")

    def handle_endtag(self, tag):
        if tag == "dt" and self.signature:
            self.signature = False
            self.signature_done = True
        if tag == "dl":
            if self.skip_dl == self.dl_depth:
                self.skip_dl = None
            if self.current is not None and self.class_depth == self.dl_depth:
                if self.current["anchor"]:
                    self.sections.append(self.current)
                self.current = None
                self.class_depth = None
            self.dl_depth = max(0, self.dl_depth - 1)
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
        if self.current is not None and not self.hidden and not self.signature and self.skip_dl is None:
            # Scan the whole bounded HTML, retaining a description for every
            # real class, including those beyond the old document-prefix cap.
            remaining = 12_000 - self.current["length"]
            if remaining > 0:
                part = data[:remaining]
                self.current["parts"].append(part)
                self.current["length"] += len(part)
            if len(data) > max(0, remaining):
                self.current["truncated"] = True


def _normalize_text(value):
    value = re.sub(r"[^\S\n]+", " ", value)
    return re.sub(r" *\n[ \n]*", "\n\n", value).strip()


def _section_text(parser, limit):
    """Balance the text budget across real API class definitions, not ToC entries."""
    sections = [(row["anchor"], _normalize_text("".join(row["parts"]))) for row in parser.sections]
    sections = [(anchor, body) for anchor, body in sections if body]
    if not sections:
        return _normalize_text("".join(parser.parts))[:limit], parser.length > limit
    headers = [f"## {anchor}\n\n" for anchor, _ in sections]
    available = limit - sum(map(len, headers)) - 2 * (len(sections) - 1)
    share = max(0, available // len(sections))
    text = "\n\n".join(header + body[:share] for header, (_, body) in zip(headers, sections))
    return text[:limit], (len(text) > limit or any(len(body) > share for _, body in sections)
                         or any(row["truncated"] for row in parser.sections))


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
        text, truncated = _section_text(parser, limit)
        return {
            "text": text, "truncated": truncated, "meta": parser.meta,
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
