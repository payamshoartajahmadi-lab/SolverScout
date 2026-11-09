"""Run the fixed, bounded research pipeline inside the local backend."""

from __future__ import annotations

import json
import re
from collections import defaultdict, deque
from datetime import datetime
from time import monotonic

from filelock import FileLock, Timeout

from .contracts import ResearchOutcome, SearchHit, SourceDocument
from .evidence import keywords, passages
from .extraction import PARSER_VERSION, metadata_date
from .fetch import DocumentFetcher
from .search import TavilySearch
from .source_policy import SourcePolicyError
from .storage import digest, now
from .transport import ResearchError


def _failure(error: ResearchError) -> dict:
    return {"code": error.code, "retry_after": error.retry_after}



def _retry_in_future(retry_after):
    try:
        return datetime.fromisoformat(retry_after) > now()
    except (ValueError, TypeError):
        return False


class _Session:
    def __init__(self, store, job_id, limits, policy, sanitize, cancelled, resume):
        self.store = store
        self.job_id = job_id
        self.limits = limits
        self.policy = policy
        self.sanitize = sanitize
        self.cancelled = cancelled
        self.resume = resume
        self.started = monotonic()
        self.already_active = store.ledger(job_id)["active_seconds"]

    def check_cancelled(self):
        if self.cancelled():
            raise ResearchError("RESEARCH_CANCELLED")

    def timeout(self):
        self.check_cancelled()
        left = self.limits.active_timeout_seconds - self.already_active - (monotonic() - self.started)
        if left <= 0:
            raise ResearchError("RESEARCH_BUDGET_EXHAUSTED")
        return min(left, self.limits.request_timeout_seconds)

    def add_active(self, elapsed):
        self.store.add_active(self.job_id, elapsed)

    def reserve(self, invocation_id, kind, payload):
        self.check_cancelled()
        existing = self.store.reserve(self.job_id, invocation_id, kind, payload, self.limits)
        if existing and existing["state"] == "reserved":
            self.store.settle(
                self.job_id, invocation_id, "unknown_outcome", {}, self.limits.request_timeout_seconds,
            )
            existing["state"] = "unknown_outcome"
        return existing

    def search(self, adapter, query, credential, domains):
        timeout = self.timeout()
        payload = {"query": query.model_dump(mode="json"), "domains": domains, "limits": self.limits.model_dump()}
        for attempt in range(self.limits.max_search_attempts):
            invocation = f"search:{query.query_id}:{attempt}"
            existing = self.reserve(invocation, "search", payload)
            if existing is None:
                break
            if existing["state"] == "unknown_outcome":
                raise ResearchError("SEARCH_OUTCOME_UNKNOWN")
            result = json.loads(existing["payload"])
            if existing["state"] == "completed":
                return result
            code = result["code"]
            retry_after = result.get("retry_after")
            if not self.resume or code not in {"SEARCH_AUTH_FAILED", "SEARCH_RATE_LIMITED"}:
                raise ResearchError(code, retry_after=retry_after)
            if retry_after and _retry_in_future(retry_after):
                raise ResearchError(code, retry_after=retry_after)
        else:
            raise ResearchError("RESEARCH_BUDGET_EXHAUSTED")
        started = monotonic()
        try:
            result = adapter.search(
                query.text, domains, self.limits.max_results_per_query, timeout, credential, self.sanitize,
            )
        except ResearchError as exc:
            state = "unknown_outcome" if exc.code == "SEARCH_OUTCOME_UNKNOWN" else "failed"
            self.store.settle(self.job_id, invocation, state, _failure(exc), monotonic() - started)
            raise
        self.store.settle(self.job_id, invocation, "completed", result, monotonic() - started)
        return result

    def get(self, transport, kind, key, url, max_bytes, conditional):
        timeout = self.timeout()
        invocation = kind + ":" + key
        limit = max_bytes
        if kind == "fetch":
            limit = min(
                limit, self.limits.max_total_bytes - self.store.ledger(self.job_id)["expanded_bytes"],
            )
            if limit <= 0:
                raise ResearchError("RESEARCH_BUDGET_EXHAUSTED")
        previous = self.reserve(invocation, kind, {"url": url, "conditional": conditional})
        if previous:
            # Complete documents have their own checkpoint. A bare HTTP
            # reservation is never sent a second time after an interruption.
            raise ResearchError("SOURCE_OUTCOME_UNKNOWN")
        started = monotonic()
        try:
            response = transport.get(url, timeout, limit, conditional)
        except ResearchError as exc:
            used = limit if kind == "fetch" and exc.code == "RESEARCH_DATA_LIMIT" else 0
            self.store.settle(
                self.job_id, invocation, "failed", _failure(exc), monotonic() - started, used,
            )
            raise
        used = len(response.body) if kind == "fetch" else 0
        self.store.settle(
            self.job_id, invocation, "completed", {"status": response.status}, monotonic() - started, used,
        )
        return response


def _safe_hit(raw, query_id, policy, source_ids):
    hit = SearchHit(
        query_id=query_id, url=raw["url"], title=raw["title"], snippet=raw["snippet"],
        provider_rank=raw["provider_rank"], published_at=metadata_date(raw.get("published_at")),
    )
    try:
        if "[REDACTED]" in hit.url:
            raise SourcePolicyError("Sensitive URL")
        url, source_id = policy.authorize(hit.url, source_ids)
    except SourcePolicyError as exc:
        return hit.model_copy(update={"rejection_code": exc.code})
    return hit.model_copy(update={"canonical_url": url, "source_id": source_id})


def _choose_documents(hits, count):
    selected = {}
    ordered = defaultdict(list)
    for hit in hits:
        if hit.canonical_url:
            ordered[hit.query_id].append(hit)
    queues = [
        deque(sorted(group, key=lambda item: (item.provider_rank, item.canonical_url)))
        for group in ordered.values()
    ]
    rejected = {}
    while any(queues):
        for queue in queues:
            if not queue:
                continue
            hit = queue.popleft()
            url = hit.canonical_url
            if url in selected:
                rejected[(hit.query_id, hit.provider_rank)] = "SOURCE_DUPLICATE"
            elif len(selected) >= count:
                rejected[(hit.query_id, hit.provider_rank)] = "RESEARCH_DOCUMENT_LIMIT"
            else:
                selected[url] = hit
    annotated = tuple(
        hit.model_copy(update={"rejection_code": rejected[(hit.query_id, hit.provider_rank)]})
        if (hit.query_id, hit.provider_rank) in rejected else hit for hit in hits
    )
    return tuple(selected.values()), annotated


class ResearchGateway:
    def __init__(self, store, *, search=None, fetch=None):
        self.store = store
        self.search = search or TavilySearch()
        self.fetch = fetch or DocumentFetcher()

    @staticmethod
    def _policy_key(policy):
        return digest([policy.hash, "tavily", PARSER_VERSION, "und"])

    def _valid_cache(self, payload, policy, max_age, source_ids=None):
        doc = SourceDocument.model_validate(payload)
        if doc.fetch_status == "failed" or doc.validated_at is None:
            return None
        age = (now() - doc.validated_at).total_seconds() / 3600
        if age < 0 or (max_age is not None and age > max_age):
            return None
        try:
            policy.authorize(doc.requested_url, source_ids)
            policy.authorize(doc.final_url, source_ids)
        except SourcePolicyError:
            return None
        text = self.store.load_text(doc.text_sha256)
        return (doc, text, age) if text is not None else None

    def cache_available(self, policy, max_cache_age_hours):
        return any(
            self._valid_cache(payload, policy, max_cache_age_hours)
            for payload in self.store.documents_for_policy(self._policy_key(policy))
        )

    def _cached(self, session, plan, max_age, key):
        found = []
        seen = set()
        for payload in self.store.documents_for_policy(key):
            session.check_cancelled()
            valid = self._valid_cache(payload, session.policy, max_age)
            if valid is None:
                continue
            doc, text, age = valid
            queries = [
                query.text for query in plan.queries if doc.source_id in query.source_ids
            ] if plan else []
            if (plan is None or queries) and session.sanitize(text) == text:
                score = len(set().union(*(keywords(query) for query in queries)).intersection(keywords(text)))
                found.append((-score, age, doc.requested_url, doc, text, queries))
        found.sort(key=lambda row: row[:3])
        for _, age, url, doc, text, queries in found:
            if url in seen:
                continue
            seen.add(url)
            if len(seen) > session.limits.max_documents:
                break
            new = doc.model_copy(update={
                "document_id": digest([session.job_id, url])[:32],
                "fetch_status": "cached", "cache_age_hours": age,
            })
            existing = self.store.lookup_document(session.job_id, url)
            if existing is None:
                self.store.save_document(session.job_id, new, key + ":" + digest(url))
            yield new, text, queries

    def _live(self, session, plan, credential, key, on_phase, documents, hits, reasons):
        queries = {query.query_id: query for query in plan.queries}
        on_phase("searching")
        for query in queries.values():
            if session.sanitize(query.text) != query.text or re.search(r"\[REDACTED\]", query.text):
                raise ResearchError("SEARCH_QUERY_SECRET_REJECTED")
            domains = tuple(sorted({rule.hostname for rule in session.policy.rules if rule.source_id in query.source_ids}))
            raw = session.search(self.search, query, credential, domains)
            hits.extend(_safe_hit(item, query.query_id, session.policy, query.source_ids) for item in raw)
        selected, annotated = _choose_documents(hits, session.limits.max_documents)
        hits[:] = annotated
        on_phase("fetching_evidence")
        cache_keys = {item.canonical_url: key + ":" + digest(item.canonical_url) for item in selected}
        cached = {}
        for payload in self.store.cached_documents(tuple(cache_keys.values())):
            candidate = self._valid_cache(payload, session.policy, None)
            if candidate is None:
                continue
            doc, _, _ = candidate
            old = cached.get(doc.requested_url)
            if old is None or doc.validated_at > old.validated_at:
                cached[doc.requested_url] = doc
        related = defaultdict(list)
        for hit in hits:
            if hit.canonical_url in cache_keys:
                related[hit.canonical_url].append(queries[hit.query_id].text)
        for hit in selected:
            session.check_cancelled()
            url = hit.canonical_url
            sources = queries[hit.query_id].source_ids
            previous = self.store.lookup_document(session.job_id, url)
            if previous:
                document = SourceDocument.model_validate(previous)
                text = self.store.load_text(document.text_sha256)
            else:
                document, text = self.fetch.fetch(session, hit, sources, cached.get(url))
                self.store.save_document(session.job_id, document, cache_keys[url], text)
            documents.append((document, text, related[url]))
            if document.reason_code:
                reasons.append(document.reason_code)
            if document.reason_code in {"RESEARCH_BUDGET_EXHAUSTED", "RESEARCH_DATA_LIMIT"}:
                break

    def run(
        self, job_id, plan, policy, limits, *, mode="live", max_cache_age_hours=24,
        credential=None, cancelled=lambda: False, sanitize=None, on_phase=None, resume=False,
    ):
        if mode not in {"live", "cache_only"}:
            raise ResearchError("SEARCH_CONFIGURATION_REQUIRED")
        if not policy.snapshot.active_sources or (mode == "live" and (not credential or plan is None)):
            raise ResearchError("SEARCH_CONFIGURATION_REQUIRED")
        if plan is not None:
            plan = plan.normalized(policy.snapshot, limits)
        base_sanitize = sanitize or (lambda value: value)

        def clean(value):
            value = base_sanitize(value)
            return value.replace(credential, "[REDACTED]") if credential else value

        lock = FileLock(self.store.artifact_dir / (digest(job_id) + ".lock"), timeout=0)
        try:
            lock.acquire()
        except Timeout:
            raise ResearchError("RESEARCH_ALREADY_RUNNING") from None
        try:
            previous = self.store.begin_run(job_id, {
                "plan": plan.model_dump(mode="json") if plan else None, "mode": mode, "policy": policy.hash,
                "limits": limits.model_dump(mode="json"), "max_cache_age_hours": max_cache_age_hours,
            })
            if previous and not (
                resume and previous["status"] == "failed"
                and set(previous["reason_codes"]).intersection({"SEARCH_AUTH_FAILED", "SEARCH_RATE_LIMITED"})
                and previous["search_attempts"] < limits.max_search_attempts
                and previous["http_requests"] < limits.max_http_requests
            ):
                return ResearchOutcome.model_validate(previous)
            session = _Session(self.store, job_id, limits, policy, clean, cancelled, resume)
            documents, hits, reasons = [], [], []
            status, retry_after = "completed", None
            phase = on_phase or (lambda name: None)
            try:
                session.check_cancelled()
                if mode == "cache_only":
                    phase("fetching_evidence")
                    documents.extend(self._cached(session, plan, max_cache_age_hours, self._policy_key(policy)))
                else:
                    self._live(session, plan, credential, self._policy_key(policy), phase, documents, hits, reasons)
                session.check_cancelled()
            except ResearchError as exc:
                reasons.append(exc.code)
                retry_after = exc.retry_after
                if exc.code == "RESEARCH_CANCELLED":
                    status = "cancelled"
                elif exc.code != "RESEARCH_BUDGET_EXHAUSTED":
                    status = "failed"
            evidence = passages((row for row in documents if row[1] is not None), limits.max_passage_chars)
            if status == "completed" and not evidence:
                status = "insufficient_evidence"
                reasons.append("RESEARCH_EVIDENCE_INSUFFICIENT")
            elif status == "completed" and reasons:
                status = "partial"
            counted = self.store.ledger(job_id)["active_seconds"] - session.already_active
            overhead = max(0, monotonic() - session.started - counted)
            self.store.add_active(job_id, overhead)
            outcome = ResearchOutcome(
                status=status, documents=tuple(row[0] for row in documents), evidence=evidence,
                query_plan=plan, hits=tuple(hits), cache_only=mode == "cache_only", retry_after=retry_after,
                reason_codes=tuple(dict.fromkeys(reasons)), **self.store.ledger(job_id),
            )
            self.store.save_outcome(job_id, outcome.model_dump(mode="json"), evidence)
            return outcome
        finally:
            lock.release()
