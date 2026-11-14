"""Versioned, auditable writer metadata handling before strict validation.

This boundary never repairs prose or evidence. The original provider result is
retained by the workflow journal; only an optional, unused fact reference can be
removed from the effective draft that subsequently undergoes normal validation
and independent semantic review.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json

from p_mc_optimizer.llm.narrative_models import WriterContextV3
from p_mc_optimizer.reporting.evidence_v3 import value_hash
from p_mc_optimizer.reporting.narrative_validation import TOKEN, validate_draft


def normalize_optional_fact_refs(context, raw_response, *, policy_version="1.3"):
    """Return an effective response and its delta without mutating either input.

    Historical generations bypass this boundary. For generation 1.3, the whole
    response must have valid structure, request binding, scope and known IDs.
    Duplicate references, malformed tokens and missing token references are not
    normalization candidates. Section-required references are always retained,
    including when unused: the strict validator must still reject that defect.
    """
    effective = deepcopy(raw_response)
    if policy_version in {"1.0", "1.1", "1.2"}:
        return effective, None
    if policy_version != "1.3":
        raise ValueError("Unknown report generation policy")
    try:
        raw_hash = sha256(raw_response).hexdigest() if isinstance(raw_response, bytes) else value_hash(raw_response)
    except (TypeError, ValueError, UnicodeError):
        raw_hash = None
    audit = {
        "schema_version": "1.0", "report_generation_policy_version": policy_version,
        "status": "ineligible", "reason": "invalid_structure_request_scope_or_identifiers",
        "raw_response_sha256": raw_hash,
        "effective_response_sha256": raw_hash,
        "removed_fact_refs": [], "text_changed": False, "evidence_refs_changed": False,
    }
    if raw_hash is None or not isinstance(raw_response, dict):
        return effective, audit
    checked = validate_draft(context, raw_response)
    if checked.global_error or any(item.code in {
        "SCHEMA_INVALID", "UNKNOWN_FACT", "UNKNOWN_EVIDENCE", "OUT_OF_SCOPE_REPAIR",
        "MISSING_REQUIRED_SECTION", "CONTEXT_TOO_LARGE",
    } for item in checked.issues):
        return effective, audit
    ctx = WriterContextV3.model_validate(context)
    requirements = {item.section_id: set(item.required_fact_refs) for item in ctx.evidence.sections}
    facts = {item.fact_id for item in ctx.evidence.facts}
    # Gate every paragraph before changing any metadata. In particular, removing
    # a duplicate unused reference would incorrectly conceal a separate defect.
    for section in raw_response["sections"]:
        if section["section_id"] not in requirements or section["section_id"] not in ctx.section_scope:
            return effective, audit
        for paragraph in section["paragraphs"]:
            refs = paragraph["fact_refs"]
            tokens = set(TOKEN.findall(paragraph["text_fa"]))
            residue = TOKEN.sub("", paragraph["text_fa"])
            if (len(refs) != len(set(refs)) or tokens - set(refs)
                    or (tokens | set(refs)) - facts or "{{" in residue or "}}" in residue):
                audit["reason"] = "invalid_token_or_reference_binding"
                return effective, audit
    for section in effective["sections"]:
        required = requirements[section["section_id"]]
        for paragraph in section["paragraphs"]:
            tokens = set(TOKEN.findall(paragraph["text_fa"]))
            retained = []
            for index, ref in enumerate(paragraph["fact_refs"]):
                if ref not in tokens and ref not in required:
                    audit["removed_fact_refs"].append({
                        "section_id": section["section_id"], "paragraph_id": paragraph["paragraph_id"],
                        "fact_id": ref, "fact_ref_index": index,
                    })
                else:
                    retained.append(ref)
            paragraph["fact_refs"] = retained
    changed = bool(audit["removed_fact_refs"])
    audit.update(status="applied" if changed else "unchanged",
                 reason="unused_known_optional_fact_refs_removed" if changed else "no_eligible_unused_fact_refs",
                 effective_response_sha256=value_hash(effective))
    return effective, audit


def contextual_binding_feedback(context, raw_response, issues):
    """Describe remaining binding faults without changing validator decisions.

    Only generation 1.3 callers use this feedback. Legacy issue text and hashes
    therefore remain unchanged, as do all validation and review verdicts.
    """
    ctx = WriterContextV3.model_validate(context)
    requirements = {item.section_id: set(item.required_fact_refs) for item in ctx.evidence.sections}
    paragraphs = {}
    if isinstance(raw_response, dict) and isinstance(raw_response.get("sections"), list):
        for section in raw_response["sections"]:
            if not isinstance(section, dict) or not isinstance(section.get("section_id"), str):
                continue
            for paragraph in section.get("paragraphs", []) if isinstance(section.get("paragraphs"), list) else []:
                if isinstance(paragraph, dict) and isinstance(paragraph.get("paragraph_id"), str):
                    paragraphs[(section.get("section_id"), paragraph.get("paragraph_id"))] = paragraph
    output = []
    for issue in issues:
        paragraph = paragraphs.get((issue.section_id, issue.paragraph_id))
        if issue.code != "INVALID_BINDING" or not paragraph:
            output.append(issue)
            continue
        refs, text = paragraph.get("fact_refs"), paragraph.get("text_fa")
        if not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs) or not isinstance(text, str):
            output.append(issue)
            continue
        tokens = set(TOKEN.findall(text))
        mandatory_unused = (set(refs) - tokens) & requirements.get(issue.section_id, set())
        detail = {"section_id": issue.section_id, "paragraph_id": issue.paragraph_id,
                  "mandatory_unused_fact_refs": sorted(mandatory_unused),
                  "token_ids_not_in_fact_refs": sorted(tokens - set(refs)),
                  "fact_refs_not_used_as_tokens": sorted(set(refs) - tokens)}
        instruction = (
            "اختلاف فقط در همین بخش و پاراگراف اصلاح شود. fact_refs باید دقیقاً شناسه‌های یکتای توکن‌های همان پاراگراف باشد. "
            "ارجاع ضروریِ بخش را حذف نکنید؛ توکن آن را در متن مناسب همان بخش بیاورید. "
            "توکن بدون ارجاع، ارجاع تکراری یا ناشناخته و توکن ناقص باید اصلاح شوند. "
            "متن و شواهد و بخش‌های پذیرفته‌شده را خارج از دامنهٔ اصلاح تغییر ندهید. جزئیات: "
            + json.dumps(detail, ensure_ascii=False, separators=(",", ":"))
        )
        output.append(issue.model_copy(update={"required_change_fa": instruction}))
    return output
