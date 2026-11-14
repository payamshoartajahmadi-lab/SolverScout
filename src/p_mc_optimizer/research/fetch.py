"""Redirect policy, robots checks and extraction for a single document."""

from __future__ import annotations

import hashlib
from time import monotonic
from urllib.parse import urljoin, urlsplit

from protego import Protego

from .contracts import SourceDocument
from .extraction import extract, extraction_profile, metadata_date
from .source_policy import SourcePolicyError
from .storage import digest, now
from .transport import PinnedTransport, ResearchError


_REDIRECT = {301, 302, 303, 307, 308}


class DocumentFetcher:
    def __init__(self, transport=None):
        self.transport = transport or PinnedTransport()

    def _robots(self, session, url, sources):
        host = urlsplit(url).hostname
        with session.store.connect() as conn:
            cached = conn.execute("SELECT * FROM robots_cache WHERE hostname=?", (host,)).fetchone()
        from datetime import datetime, timedelta

        if cached is not None:
            age = now() - datetime.fromisoformat(cached["validated_at"])
            if not timedelta() <= age < timedelta(hours=24):
                cached = None
        if cached is None:
            start_url = f"https://{host}/robots.txt"
            try:
                response, _, _ = self._follow(
                    session, start_url, sources, "robots", 512 * 1024, {}, host=host,
                )
                if response.status == 200:
                    text = response.body.decode("utf-8-sig")
                    if session.sanitize(text) != text or "\x00" in text or text.lstrip().startswith("<"):
                        raise ResearchError("SOURCE_POLICY_UNAVAILABLE")
                    mode = "rules"
                elif response.status in {404, 410, 401, 403}:
                    mode = "allow" if response.status in {404, 410} else "deny"
                    text = None
                else:
                    raise ResearchError("SOURCE_POLICY_UNAVAILABLE")
            except ResearchError as exc:
                if exc.code in {"RESEARCH_BUDGET_EXHAUSTED", "RESEARCH_CANCELLED"}:
                    raise
                raise ResearchError("SOURCE_POLICY_UNAVAILABLE") from None
            except (ValueError, UnicodeError):
                raise ResearchError("SOURCE_POLICY_UNAVAILABLE") from None
            with session.store.connect(write=True) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO robots_cache VALUES(?,?,?,?)",
                    (host, now().isoformat(), mode, text),
                )
            cached = {"mode": mode, "payload": text}
        if cached["mode"] == "deny" or (
            cached["mode"] == "rules" and not Protego.parse(cached["payload"]).can_fetch(url, "P_MC_Research/2.0")
        ):
            raise ResearchError("SOURCE_ROBOTS_DISALLOWED")

    def _follow(self, session, requested, sources, kind, max_bytes, conditional, *, host=None, validator_url=None):
        url = requested
        chain = []
        for hop in range(session.limits.max_redirects + 1):
            try:
                if host is None:
                    url, _ = session.policy.authorize(url, sources)
                    self._robots(session, url, sources)
                else:
                    url, _ = session.policy.authorize(url, sources, robots=True, robots_host=host)
            except SourcePolicyError as exc:
                error = ResearchError(exc.code)
                error.redirect_chain = tuple(chain)
                raise error from None
            except ResearchError as exc:
                exc.redirect_chain = tuple(chain)
                raise

            response = session.get(
                self.transport, kind, digest([requested, hop])[:32], url, max_bytes,
                conditional if url == validator_url else {},
            )
            if response.status not in _REDIRECT:
                return response, url, tuple(chain)
            target = response.headers.get("location")
            if not target or hop == session.limits.max_redirects:
                raise ResearchError("SOURCE_REDIRECT_LIMIT")
            try:
                url = urljoin(url, target)
                parts = urlsplit(url)
            except ValueError:
                error = ResearchError("SOURCE_NOT_ALLOWED")
                error.redirect_chain = tuple(chain)
                raise error from None
            if len(url) > 4096 or parts.username is not None or parts.password is not None or session.sanitize(url) != url:
                error = ResearchError("SOURCE_NOT_ALLOWED")
                error.redirect_chain = tuple(chain)
                raise error
            chain.append(url)
        raise ResearchError("SOURCE_REDIRECT_LIMIT")

    def fetch(self, session, hit, sources, cached, *, capture=None, link_limit=0):
        requested = hit.canonical_url
        args = {
            "document_id": digest([session.job_id, requested])[:32],
            "source_id": hit.source_id,
            "requested_url": requested,
            "parser_version": extraction_profile(session.limits.max_text_chars),
            "published_at": hit.published_at,
            "updated_at": hit.updated_at,
        }
        conditional = {}
        same_extraction = cached is not None and cached.parser_version == args["parser_version"]
        if same_extraction:
            for name, value in (("If-None-Match", cached.etag), ("If-Modified-Since", cached.last_modified)):
                if value and value.isascii() and "\r" not in value and "\n" not in value:
                    conditional[name] = value
        try:
            response, final_url, chain = self._follow(
                session, requested, sources, "fetch", session.limits.max_document_bytes, conditional,
                validator_url=cached.final_url if cached else None,
            )
            args.update(final_url=final_url, redirect_chain=chain)
            validated_at = now()
            if response.status == 304:
                if cached is not None and not same_extraction:
                    raise ResearchError("SOURCE_PARSER_REEXTRACTION_REQUIRED")
                if not cached or not conditional or cached.final_url != final_url:
                    raise ResearchError("SOURCE_INVALID_REVALIDATION")
                text = session.store.load_text(cached.text_sha256)
                if text is None or session.sanitize(text) != text:
                    raise ResearchError("SOURCE_INVALID_REVALIDATION")
                return cached.model_copy(update={
                    **args, "validated_at": validated_at, "fetch_status": "revalidated",
                    "published_at": cached.published_at, "updated_at": cached.updated_at,
                }), text
            if response.status == 402:
                raise ResearchError("SOURCE_PAYWALL")
            if response.status in {401, 403}:
                raise ResearchError("SOURCE_ACCESS_RESTRICTED")
            if response.status != 200:
                raise ResearchError("SOURCE_UNREACHABLE")
            if urlsplit(final_url).path.lower().rstrip("/").endswith(("/login", "/signin", "/sign-in")):
                raise ResearchError("SOURCE_LOGIN_REQUIRED")
            content_type = response.headers.get("content-type", "")[:250]
            content_type = session.sanitize(content_type)
            media = content_type.split(";", 1)[0].strip().lower()
            level = "documentation"
            if media == "application/pdf":
                level = "full_text"
            elif urlsplit(final_url).path.startswith("/abs/"):
                level = "abstract"
            args.update(
                fetched_at=validated_at, validated_at=validated_at, content_type=content_type,
                bytes_sha256=hashlib.sha256(response.body).hexdigest(),
            )
            started = monotonic()
            try:
                parsed = extract(
                    response.body, content_type, level,
                    session.limits.max_text_chars, session.timeout(), link_limit=link_limit,
                )
            finally:
                session.add_active(monotonic() - started)
            text = session.sanitize(parsed["text"])
            if not text.strip():
                raise ResearchError("SOURCE_EMPTY_TEXT")
            meta = parsed.get("meta", {})
            if parsed["truncated"] and level == "full_text":
                # The PDF was read but a full-text claim would overstate extraction.
                level = "documentation"
            args["published_at"] = metadata_date(
                meta.get("citation_publication_date") or meta.get("citation_date") or meta.get("article:published_time"),
            ) or args["published_at"]
            args["updated_at"] = metadata_date(meta.get("article:modified_time")) or args["updated_at"]

            def validator(name):
                val = response.headers.get(name)
                return val if val and len(val) <= 256 and session.sanitize(val) == val else None

            document = SourceDocument(
                **args, content_level=level, fetch_status="read",
                text_sha256=hashlib.sha256(text.encode()).hexdigest(),
                truncated=parsed["truncated"], etag=validator("etag"), last_modified=validator("last-modified"),
            )
            if capture is not None:
                capture(response, parsed)
            return document, text
        except ResearchError as exc:
            if exc.code == "RESEARCH_CANCELLED":
                raise
            if exc.redirect_chain:
                args["redirect_chain"] = exc.redirect_chain
            return SourceDocument(**args, fetch_status="failed", reason_code=exc.code), None
