"""Deterministic bindings and partial section validation, never a truth oracle."""
from __future__ import annotations

from dataclasses import dataclass, field
from collections import Counter
import json
import re
from typing import Any
from pydantic import ValidationError

from p_mc_optimizer.llm.narrative_models import (
    NarrativeSectionV3, NarrativeDraftV3, NarrativeReviewV1, WriterContextV3, ReviewerContextV1,
    ReportEvidenceV3, RenderedSectionV1, RenderedParagraphV1, ValidationIssueV1, SECTION_ORDER, REQUIRED_SECTIONS,
    InvalidSectionRepairV1,
)
from p_mc_optimizer.reporting.evidence_v3 import canonical_bytes, value_hash, evidence_hash

TOKEN = re.compile(r"\{\{fact:([a-zA-Z][a-zA-Z0-9_.-]*)\}\}")
_URL = re.compile(r"(?:https?://|www\.)\S+", re.I)
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_HASH = re.compile(r"\b[0-9a-f]{40,64}\b", re.I)
_MARKUP = re.compile(r"<[^>]*>|```|\[[^\]]*\]\([^)]*\)|(?:javascript|data):", re.I)
_INTERNAL = re.compile(r"mealpy:[^\s،؛]+|\b[a-zA-Z_][\w.-]*\.(?:py|json|sqlite3|jsonl)\b|(?:/Users/|/private/|/var/)[^\s]+")
_EXECUTABLE = re.compile(r"\b(?:eval|exec|compile|__import__)\s*\(|\bos\.system\s*\(|\bsubprocess\.|\bpip\s+install\b|\bcurl\s+https?",re.I)


class NarrativeValidationError(ValueError):
    def __init__(self, *issues):
        self.issues = list(issues[0]) if len(issues) == 1 and isinstance(issues[0], list) else list(issues)
        super().__init__(self.issues[0].message_fa if self.issues else "متن با قرارداد گزارش سازگار نیست.")


@dataclass
class DraftValidation:
    valid_sections: list[NarrativeSectionV3] = field(default_factory=list)
    issues: list[ValidationIssueV1] = field(default_factory=list)
    global_error: bool = False
    omitted_sections: list[str] = field(default_factory=list)
    invalid_sections: list[InvalidSectionRepairV1] = field(default_factory=list)

    @property
    def valid(self):
        return not self.issues and not self.global_error


def issue(code, section=None, paragraph=None, refs=(), message=None):
    messages = {
        "SCHEMA_INVALID":"ساختار پاسخ با قرارداد متن سازگار نیست.",
        "CONTEXT_TOO_LARGE":"اندازهٔ زمینه یا متن از سقف مجاز بیشتر است.",
        "CONTEXT_MISMATCH":"بستهٔ شواهد پاسخ با اجرای جاری یکسان نیست.",
        "REQUEST_MISMATCH":"پاسخ به درخواست ثبت‌شده متصل نیست.",
        "DRAFT_MISMATCH":"بازبینی به پیش‌نویس همین درخواست متصل نیست.",
        "UNKNOWN_FACT":"توکن به دادهٔ موجود در این اجرا متصل نیست.",
        "UNKNOWN_EVIDENCE":"ارجاع شاهد در این بسته وجود ندارد.",
        "INVALID_BINDING":"فهرست factها با توکن‌های همان پاراگراف برابر نیست.",
        "UNBOUND_NUMBER":"کمیت خام خارج از توکن معتبر آمده است.",
        "MISSING_REQUIRED_SECTION":"بخش لازم در پاسخ موجود نیست.",
        "MISSING_REQUIRED_FACT":"داده‌های ضروری بخش در متن پوشش داده نشده‌اند.",
        "OUT_OF_SCOPE_REPAIR":"اصلاح از دامنهٔ ثبت‌شده فراتر رفته است.",
        "UNSAFE_MARKUP":"نشانه‌گذاری یا شناسهٔ فنی در متن عادی مجاز نیست.",
    }
    return ValidationIssueV1(section_id=section, paragraph_id=paragraph, code=code, evidence_refs=list(refs), message_fa=message or messages.get(code,"متن نیازمند اصلاح است."), required_change_fa="همان بخش را با داده‌ها و ارجاع‌های معتبر همین درخواست اصلاح کنید؛ بخش‌های پذیرفته‌شده را تغییر ندهید.")


def _data(value):
    return value.model_dump(mode="json") if hasattr(value,"model_dump") else value


def request_hash(context):
    data = dict(_data(context));data.pop("request_hash",None)
    return value_hash(data)


def section_hash(section):
    return value_hash(_data(section))


def ordered_sections(sections):
    sections = [NarrativeSectionV3.model_validate(s) if isinstance(s,dict) else s for s in sections]
    return sorted(sections,key=lambda s:SECTION_ORDER.index(s.section_id))


def draft_hash(context_hash, sections):
    return value_hash({"schema_version":"3.0","context_hash":context_hash,"sections":[_data(s) for s in ordered_sections(sections)]})


def render_sections(evidence, sections):
    evidence = ReportEvidenceV3.model_validate(evidence) if isinstance(evidence,dict) else evidence
    mapping = {f.fact_id:f.display_text for f in evidence.facts}
    output = []
    for section in ordered_sections(sections):
        paragraphs = []
        for p in section.paragraphs:
            def replace(match):
                if match[1] not in mapping:
                    raise NarrativeValidationError(issue("UNKNOWN_FACT",section.section_id,p.paragraph_id))
                return mapping[match[1]]
            # re.sub processes only the original template, never newly inserted values.
            paragraphs.append(RenderedParagraphV1(paragraph_id=p.paragraph_id,text_fa=TOKEN.sub(replace,p.text_fa)))
        output.append(RenderedSectionV1(section_id=section.section_id,paragraphs=paragraphs))
    return output


def _context_checks(ctx):
    errors=[]
    if evidence_hash(ctx.evidence)!=ctx.context_hash:errors.append(issue("CONTEXT_MISMATCH"))
    if request_hash(ctx)!=ctx.request_hash:errors.append(issue("REQUEST_MISMATCH"))
    if len(canonical_bytes(ctx))>196608:errors.append(issue("CONTEXT_TOO_LARGE"))
    if len(canonical_bytes(ctx.evidence))>65536:errors.append(issue("CONTEXT_TOO_LARGE"))
    if len(ctx.section_scope)!=len(set(ctx.section_scope)):errors.append(issue("SCHEMA_INVALID"))
    return errors


def _parse(value):
    if isinstance(value,(bytes,str)):
        from p_mc_optimizer.domain.models import strict_json_loads
        return strict_json_loads(value,limit=196608)
    return _data(value)


def _repair_text(value, limit):
    """Project strings without transporting credentials, headers or private paths.

    This supplements the gateway's known-key sanitizer for synthetic/offline inputs.
    It is not a reason to copy arbitrary fields into the repair transport.
    """
    if not isinstance(value, str):
        return None, value is not None
    # Normalize before inspection so removing a control cannot reveal an
    # uninspected credential name or token (for example api_\x00key).
    text = "".join("\ufffd" if 0xD800 <= ord(c) <= 0xDFFF else c for c in value if ord(c) >= 32 or c in "\n\r\t")
    text = re.sub(r"\b(?:sk-|tvly-|gh[pousr]_|github_pat_)[A-Za-z0-9_-]{6,}", "[REDACTED]", text)
    text = re.sub(r"\bAKIA[A-Z0-9]{16}\b", "[REDACTED]", text)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[REDACTED]", text)
    text = re.sub(r"(?i)\b(?:Bearer|Basic)\s+[^\s\"'<>;,]+", "[REDACTED]", text)
    # A dotted public contract such as numeric_execution.authorization is not a header.
    text = re.sub(r"(?i)(?<![\w.])(?:authorization|proxy-authorization)[\"']?\s*[:=]\s*[^\r\n]+", "[REDACTED]", text)
    text = re.sub(r"(?i)(?<![\w.])(?:cookie|set-cookie)[\"']?\s*[:=]\s*[^\r\n]+", "[REDACTED]", text)
    text = re.sub(r"(?i)(?<![\w.])(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|secret|password|token)[\"']?\s*[:=]\s*(?:\"(?:\\.|[^\"\\\r\n])*\"|'(?:\\.|[^'\\\r\n])*'|[^\s,;]+)", "[REDACTED]", text)
    text = re.sub(r"(?i)https?://[^\s/@]+:[^\s/@]+@[^\s\"'<>]+", "[REDACTED]", text)
    text = re.sub(r"(?i)(?:[a-z]:[\\/]|/(?:Users|home|private|var|tmp|root)/|~[\\/]|\\\\)[^\s\"'<>]+", "[PRIVATE_PATH]", text)
    text = re.sub(r"(?i)(?:[^\s\"'<>]*[\\/])?(?:_private_qa[\\/][^\s\"'<>]*|\.env(?:\.[a-z0-9_-]+)?)(?![\w.])", "[PRIVATE_PATH]", text)
    return text[:limit], text != value or len(text) > limit


def _repair_refs(value):
    if not isinstance(value, list):
        return [], value is not None
    output, changed = [], len(value) > 32
    for item in value[:32]:
        projected, redacted = _repair_text(item, 128)
        changed = changed or redacted
        if projected is not None:
            output.append(projected)
        else:
            changed = True
    return output, changed


def _structural_error(section_index, detail):
    """Use server-owned rules and safe locations, never exception input/msg/ctx."""
    fields = {"section_id", "paragraphs", "paragraph_id", "claim_kind", "text_fa", "fact_refs", "evidence_refs"}
    path = f"sections[{section_index}]"
    location = detail.get("loc", ())
    for part in location[:8]:
        path += f"[{part}]" if type(part) is int else "." + (part if part in fields else "<extra_field>")
    last = next((part for part in reversed(location) if isinstance(part, str) and part in fields), None)
    kind = detail.get("type", "schema_invalid")
    # Pydantic error types are implementation identifiers, not model-supplied text.
    if not isinstance(kind, str) or not re.fullmatch(r"[a-z_]{1,80}", kind):
        kind = "schema_invalid"
    rules = {
        "paragraph_id": "^[a-z][a-z0-9_]{0,47}$; unique across all new and accepted paragraphs",
        "paragraphs": "a list of at most three narrative paragraphs",
        "claim_kind": "one of recorded, derived, interpretation",
        "text_fa": "a string containing 1 to 2200 characters",
        "fact_refs": "a list of strings exactly matching the unique fact-token IDs in this paragraph",
        "evidence_refs": "a list of unique evidence-ID strings belonging to the frozen section evidence",
        "section_id": "one of " + ", ".join(SECTION_ORDER),
    }
    rule = ("only the declared fields are allowed; remove the extra field" if kind == "extra_forbidden"
            else rules.get(last, "a closed NarrativeSectionV3 object with all required fields"))
    if kind == "missing":
        rule = "required field; " + rule
    instruction = "فیلد مشخص‌شده را طبق قاعدهٔ ثبت‌شده اصلاح کنید؛ بخش‌های پذیرفته‌شده تغییر نکنند."
    if last == "paragraph_id" and kind != "extra_forbidden":
        instruction = ("paragraph_id را خودتان با الگوی ^[a-z][a-z0-9_]{0,47}$ بسازید: ابتدا حرف کوچک لاتین، "
                       "سپس فقط حروف کوچک، رقم یا زیرخط؛ خط تیره مجاز نیست. نمونهٔ معتبر problem_main است. "
                       "شناسه در کل پاراگراف‌های جدید و پذیرفته‌شده یکتا بماند؛ سرور شناسه را خودکار تغییر نمی‌دهد.")
    return {"field_path": path[:240], "error_type": kind, "expected_rule": rule[:256],
            "repair_instruction_fa": instruction[:640]}


def _invalid_section_snapshot(section_id, raw, section_index, error):
    details = (error.errors(include_input=False, include_url=False, include_context=False)
               if isinstance(error, ValidationError) else [{"loc": (), "type": "schema_invalid"}])
    snapshots = []
    paragraphs = raw.get("paragraphs")
    changed = bool(set(raw) - {"section_id", "paragraphs"})
    if not isinstance(paragraphs, list):
        paragraphs, changed = [], True
    changed = changed or len(paragraphs) > 3 or len(details) > 12
    paragraph_fields = {"paragraph_id", "claim_kind", "text_fa", "fact_refs", "evidence_refs"}
    for index, paragraph in enumerate(paragraphs[:3]):
        modified = not isinstance(paragraph, dict)
        source = paragraph if isinstance(paragraph, dict) else {}
        modified = modified or bool(set(source) - paragraph_fields)
        snapshot = {"paragraph_index": index}
        for name, limit in (("paragraph_id", 160), ("claim_kind", 80), ("text_fa", 2200)):
            snapshot[name], trimmed = _repair_text(source.get(name), limit)
            modified = modified or trimmed
        for name in ("fact_refs", "evidence_refs"):
            snapshot[name], trimmed = _repair_refs(source.get(name))
            modified = modified or trimmed
        snapshot["truncated"] = modified
        snapshots.append(snapshot)
        changed = changed or modified
    projected = {"payload_kind": "invalid_section", "section_id": section_id,
                 "paragraphs": snapshots,
                 "structural_errors": [_structural_error(section_index, item) for item in details[:12]],
                 "truncated": bool(changed)}
    # A malicious collection of long refs must not crowd out the useful prose.
    # Trimming applies only to this explicit snapshot, never to the source draft.
    while len(canonical_bytes(projected)) > 16384:
        projected["truncated"] = True
        ref_lists = [(p, key) for p in snapshots for key in ("fact_refs", "evidence_refs") if p[key]]
        if ref_lists:
            paragraph, name = max(ref_lists, key=lambda item: len(canonical_bytes(item[0][item[1]])))
            paragraph[name].pop()
            paragraph["truncated"] = True
            continue
        texts = [p for p in snapshots if p["text_fa"] and len(p["text_fa"]) > 128]
        if texts:
            paragraph = max(texts, key=lambda p: len(p["text_fa"]))
            paragraph["text_fa"] = paragraph["text_fa"][:max(128, len(paragraph["text_fa"]) // 2)]
            paragraph["truncated"] = True
        elif len(projected["structural_errors"]) > 1:
            projected["structural_errors"].pop()
        else:
            # All individual fields are bounded; this final branch is defensive.
            snapshots.pop()
    return InvalidSectionRepairV1.model_validate(projected)


def _structural_issue(snapshot):
    first = snapshot.structural_errors[0]
    diagnostic = {"section_id": snapshot.section_id, "field_errors": [first.model_dump(mode="json")],
                  "additional_errors_in_snapshot": len(snapshot.structural_errors) - 1}
    detail = issue("SCHEMA_INVALID", snapshot.section_id,
                   message="خطای ساختاری بخش؛ جزئیات امن برای اصلاح: " + json.dumps(diagnostic, ensure_ascii=False, separators=(",", ":")))
    return detail.model_copy(update={"required_change_fa": first.repair_instruction_fa})


def validate_draft(context, raw_response) -> DraftValidation:
    result=DraftValidation()
    try:
        ctx=WriterContextV3.model_validate(_data(context))
        errors=_context_checks(ctx)
        if errors:raise NarrativeValidationError(errors)
        raw=_parse(raw_response)
        if not isinstance(raw,dict) or set(raw)!={"schema_version","context_hash","request_hash","sections"} or raw.get("schema_version")!="3.0" or not isinstance(raw.get("sections"),list):
            raise NarrativeValidationError(issue("SCHEMA_INVALID"))
        if raw["context_hash"]!=ctx.context_hash:raise NarrativeValidationError(issue("CONTEXT_MISMATCH"))
        if raw["request_hash"]!=ctx.request_hash:raise NarrativeValidationError(issue("REQUEST_MISMATCH"))
        if ctx.mode=="draft" and (ctx.accepted_sections or ctx.failed_sections or ctx.repair_issues or ctx.previous_response_text is not None or ctx.previous_response_truncated):
            raise NarrativeValidationError(issue("SCHEMA_INVALID"))
        if ctx.mode=="repair" and set(ctx.section_scope)&{s.section_id for s in ctx.accepted_sections}:
            raise NarrativeValidationError(issue("OUT_OF_SCOPE_REPAIR"))
    except NarrativeValidationError as error:
        return DraftValidation(issues=error.issues,global_error=True)
    except (ValueError,TypeError,ValidationError,UnicodeError):
        return DraftValidation(issues=[issue("SCHEMA_INVALID")],global_error=True)
    ids=[s.get("section_id") if isinstance(s,dict) else None for s in raw["sections"]]
    if any(s not in SECTION_ORDER for s in ids):
        return DraftValidation(issues=[issue("SCHEMA_INVALID")],global_error=True)
    if ctx.mode=="repair" and set(ids)!=set(ctx.section_scope):
        return DraftValidation(issues=[issue("OUT_OF_SCOPE_REPAIR")],global_error=True)
    if ctx.mode=="draft" and (set(ctx.section_scope)!=set(SECTION_ORDER)):
        return DraftValidation(issues=[issue("OUT_OF_SCOPE_REPAIR")],global_error=True)
    facts={f.fact_id:f for f in ctx.evidence.facts};sources={s.evidence_id:s for s in ctx.evidence.sources}
    accepted={s.section_id:section_hash(s) for s in ctx.accepted_sections}
    paragraph_ids=set(); total=0
    for s in ctx.accepted_sections:
        for p in s.paragraphs:paragraph_ids.add(p.paragraph_id);total+=len(p.text_fa)
    requirements={s.section_id:s for s in ctx.evidence.sections}
    technical=[str(f.value) for f in ctx.evidence.facts if f.fact_id.startswith("variable.") and f.fact_id.endswith(".name") and f.availability=="available"]
    for name in ("DE","GWO","WOA","PSO","ABC","CMA-ES"):
        if any(name in m.display_name_fa for m in ctx.evidence.methods):technical.append(name)
    for section_id in ctx.section_scope:
        matching=[s for s in raw["sections"] if s["section_id"]==section_id]
        if not matching:
            if section_id in REQUIRED_SECTIONS or ctx.mode=="repair":result.issues.append(issue("MISSING_REQUIRED_SECTION",section_id))
            continue
        if len(matching)!=1:
            result.issues.append(issue("SCHEMA_INVALID",section_id));continue
        try:section=NarrativeSectionV3.model_validate(matching[0])
        except (ValidationError,ValueError,TypeError) as error:
            snapshot = _invalid_section_snapshot(section_id, matching[0], raw["sections"].index(matching[0]), error)
            result.invalid_sections.append(snapshot)
            result.issues.append(_structural_issue(snapshot));continue
        if section_id in accepted and section_hash(section)!=accepted[section_id]:
            result.issues.append(issue("OUT_OF_SCOPE_REPAIR",section_id));continue
        if not section.paragraphs:
            if section_id=="mathematical_analysis" and ctx.mode=="repair":result.omitted_sections.append(section_id)
            else:result.issues.append(issue("SCHEMA_INVALID",section_id))
            continue
        local=[];covered=set()
        for p in section.paragraphs:
            total+=len(p.text_fa)
            if p.paragraph_id in paragraph_ids:local.append(issue("SCHEMA_INVALID",section_id,p.paragraph_id))
            paragraph_ids.add(p.paragraph_id)
            tokens=TOKEN.findall(p.text_fa);covered.update(tokens)
            differences={"token_ids_not_in_fact_refs":sorted(set(tokens)-set(p.fact_refs)),
                         "fact_refs_not_used_as_tokens":sorted(set(p.fact_refs)-set(tokens)),
                         "duplicate_fact_refs":sorted(f for f,count in Counter(p.fact_refs).items() if count>1)}
            if any(differences.values()):
                detail=issue("INVALID_BINDING",section_id,p.paragraph_id,message="اختلاف دقیق اتصال در این پاراگراف: "+json.dumps(differences,ensure_ascii=False,separators=(",",":")))
                local.append(detail.model_copy(update={"required_change_fa":"توکن‌های همین پاراگراف و fact_refs را مطابق اختلاف ثبت‌شده اصلاح کنید؛ معیار انتخاب را با توکن مربوط در همان بخش بیان کنید. بخش‌های پذیرفته‌شده را تغییر ندهید؛ سرور هیچ ارجاع یا توکنی را خودکار تغییر نمی‌دهد."}))
            for f in set(tokens)|set(p.fact_refs):
                if f not in facts:local.append(issue("UNKNOWN_FACT",section_id,p.paragraph_id))
            if not p.evidence_refs or len(p.evidence_refs)!=len(set(p.evidence_refs)) or set(p.evidence_refs)-sources.keys():local.append(issue("UNKNOWN_EVIDENCE",section_id,p.paragraph_id))
            allowed=set(requirements[section_id].allowed_evidence_refs)
            if set(p.evidence_refs)-allowed:local.append(issue("UNKNOWN_EVIDENCE",section_id,p.paragraph_id))
            if section_id=="mathematical_analysis" and p.claim_kind!="interpretation":local.append(issue("SCHEMA_INVALID",section_id,p.paragraph_id))
            residue=TOKEN.sub("",p.text_fa)
            if "{{" in residue or "}}" in residue or "{{fact" in residue:local.append(issue("INVALID_BINDING",section_id,p.paragraph_id))
            if _URL.search(residue) or _UUID.search(residue) or _HASH.search(residue) or _MARKUP.search(residue) or _INTERNAL.search(residue) or _EXECUTABLE.search(residue):local.append(issue("UNSAFE_MARKUP",section_id,p.paragraph_id))
            for word in sorted(technical,key=len,reverse=True):residue=re.sub(r"(?<!\w)"+re.escape(word)+r"(?!\w)","",residue)
            number_at=next((i for i,c in enumerate(residue) if c.isdigit()),None)
            if number_at is not None:
                snippet=residue[max(0,number_at-32):number_at+48]
                detail=issue("UNBOUND_NUMBER",section_id,p.paragraph_id,message="کمیت خام بدون اتصال در پاراگراف: "+json.dumps({"paragraph_id":p.paragraph_id,"unbound_number_snippet":snippet},ensure_ascii=False,separators=(",",":")))
                local.append(detail.model_copy(update={"required_change_fa":"برای قطعهٔ مشخص‌شده از fact موجود و معتبر استفاده کنید، یا جزئیات غیرضروری را حذف کنید؛ مقدار یا روش را از شباهت متن حدس نزنید و بخش‌های پذیرفته‌شده را تغییر ندهید."}))
            # Mathematical constants alone do not ground an interpretation.
            if tokens and all(f.startswith("constant.") for f in tokens) and not any(r.startswith("report:") or r.startswith("source:") for r in p.evidence_refs):local.append(issue("UNKNOWN_EVIDENCE",section_id,p.paragraph_id))
        missing=set(requirements[section_id].required_fact_refs)-covered
        if missing:
            detail=issue("MISSING_REQUIRED_FACT",section_id,message="داده‌های ضروری بدون توکن در همین بخش: "+json.dumps({"missing_required_fact_tokens":sorted(missing)},ensure_ascii=False,separators=(",",":")))
            required_tokens="، ".join("{{fact:"+fact+"}}" for fact in sorted(missing))
            local.append(detail.model_copy(update={"required_change_fa":"توکن‌های الزامی "+required_tokens+" را واقعاً در متن همین بخش بیاورید و fact_refs را با توکن‌های متن برابر نگه دارید؛ ذکر مفهوم یا ارجاع به‌تنهایی کافی نیست. بخش‌های پذیرفته‌شده را تغییر ندهید."}))
        if ctx.evidence.evidence_policy_version=="1.1" and section_id=="candidate_rationale" and not any("report:proposal" in p.evidence_refs for p in section.paragraphs):
            local.append(issue("UNKNOWN_EVIDENCE",section_id,message="بخش دلیل پیشنهاد باید دست‌کم به report:proposal همین اجرا ارجاع دهد؛ ارجاع موجود ولی نامرتبط، جای این شاهد را نمی‌گیرد."))
        if local:result.issues.extend(local)
        else:result.valid_sections.append(section)
    if total>16000:
        result.issues.append(issue("CONTEXT_TOO_LARGE"));result.global_error=True;result.valid_sections=[]
    return result


def validate_review(context, raw_response) -> NarrativeReviewV1:
    try:
        ctx=ReviewerContextV1.model_validate(_data(context));errors=_context_checks(ctx)
        if errors:raise NarrativeValidationError(errors)
        result=NarrativeReviewV1.model_validate(_parse(raw_response))
    except NarrativeValidationError:raise
    except (ValidationError,ValueError,TypeError,UnicodeError):raise NarrativeValidationError(issue("SCHEMA_INVALID")) from None
    issues=[]
    if result.context_hash!=ctx.context_hash:issues.append(issue("CONTEXT_MISMATCH"))
    if result.request_hash!=ctx.request_hash:issues.append(issue("REQUEST_MISMATCH"))
    if result.draft_hash!=ctx.draft_hash or draft_hash(ctx.context_hash,ctx.draft_sections)!=ctx.draft_hash:issues.append(issue("DRAFT_MISMATCH"))
    if [_data(s) for s in render_sections(ctx.evidence,ctx.draft_sections)]!=[_data(s) for s in ctx.rendered_sections]:issues.append(issue("DRAFT_MISMATCH"))
    by_id={s.section_id:s for s in ctx.draft_sections}
    if len(by_id)!=len(ctx.draft_sections):issues.append(issue("SCHEMA_INVALID"))
    expected={s.section_id:s.sha256 for s in ctx.accepted_section_hashes}
    if len(expected)!=len(ctx.accepted_section_hashes) or any(k not in by_id or section_hash(by_id[k])!=v for k,v in expected.items()):issues.append(issue("OUT_OF_SCOPE_REPAIR"))
    reviews=[r.section_id for r in result.section_reviews]
    if len(reviews)!=len(set(reviews)) or set(reviews)!=set(ctx.section_scope):issues.append(issue("OUT_OF_SCOPE_REPAIR"))
    sources={s.evidence_id for s in ctx.evidence.sources}
    for review in result.section_reviews:
        if (review.verdict=="accept" and review.issues) or (review.verdict=="reject" and not review.issues):issues.append(issue("SCHEMA_INVALID",review.section_id))
        paragraph_ids={p.paragraph_id for p in by_id.get(review.section_id,NarrativeSectionV3(section_id=review.section_id,paragraphs=[])).paragraphs}
        for detail in review.issues:
            if detail.section_id!=review.section_id or (detail.paragraph_id is not None and detail.paragraph_id not in paragraph_ids) or set(detail.evidence_refs)-sources:issues.append(issue("UNKNOWN_EVIDENCE",review.section_id,detail.paragraph_id))
    if issues:raise NarrativeValidationError(issues)
    return result
