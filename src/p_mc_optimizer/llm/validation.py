"""Semantic gates applied before a role result is used by the workflow."""
from __future__ import annotations

import re
import unicodedata

from p_mc_optimizer.domain.models import CandidatePlan, SOLVER_CONFIG_IDS
from p_mc_optimizer.llm.formulation import RoleOutputError, convert_formulation
from p_mc_optimizer.llm.models import ExplainerResponse

_FA_OPTIMALITY = r"(?:بهینگی|بهینه[‌ -]?بودن)(?:\s+(?:سراسری|کلی|جهانی|مطلق))?"
_FA_LIMITATION_TOPIC = rf"(?:{_FA_OPTIMALITY}|برتری\s+آماری|تضمین\s+موفقیت)"

# Mask the claim and its explicit negation together, leaving other claims visible.
_LIMITATION = re.compile(
    rf"{_FA_OPTIMALITY}\s+(?:اثبات|تضمین|تایید)\s+(?:نشده(?:\s+است)?|نمی[‌ ]?شود)\b"
    rf"|{_FA_LIMITATION_TOPIC}(?:\s+(?:یا|و)\s+{_FA_LIMITATION_TOPIC})*"
    r"\s+را\s+(?:اثبات|تایید|تضمین)\s+نمی[‌ ]?کند\b"
    rf"|ادعایی\s+درباره\s+{_FA_OPTIMALITY}(?:\s+یا\s+برتری\s+فراتر\s+از\s+این\s+اجرا)?\s+نیست\b"
    r"|(?:هیچ\s+)?تضمینی\s+(?:برای\s+بهینگی\s+)?وجود\s+ندارد"
    r"|(?:global )?optimality (?:has not been|is not|was not) (?:proven|certified|guaranteed)"
    r"|no (?:proof|guarantee|certificate) of (?:global )?optimality",
    re.IGNORECASE,
)
_CLAIM = re.compile(
    r"\b(?:optimal(?:ity)?|optimum|certified|guarantee\w*|proof|provably|unbeatable)\b"
    r"|\bno (?:better|superior)\b|\b(?:best|smallest|largest) possible\b"
    r"|بهینگی|بهینه[‌ -]?(?:ترین|بودن)|بهینه(?:\s+سراسری|\s+قطعی|\s+مطلق|\s+است|\s+شد|\s+یافت)"
    r"|تضمین|اثبات|قطعا|قطعاً|بی[‌ -]?رقیب"
    r"|بهتر[یى]?[^.!؟\n]{0,65}(?:وجود\s+ندارد|ممکن\s+نیست|نمی[‌ ]?توان(?:د)?\s+یافت|نخواهد\s+بود)"
    r"|(?:هیچ|نمی[‌ ]?توان)[^.!؟\n]{0,65}(?:بهتر[یى]?|مقدار\s+کمتری|مقدار\s+بیشتری)"
    r"|بهتر(?:ی)?\s+نمی[‌ ]?شود"
    r"|(?:کمترین|بیشترین|بهترین)\s+(?:مقدار\s+)?(?:ممکن|در\s+تمام|در\s+کل)",
    re.IGNORECASE,
)


def validate_role_output(role: str, response, context) -> None:
    if role == "formulator":
        convert_formulation(response, {source.source_ref: source.text for source in context.sources})
        return
    allowed = set(context.evidence_refs)
    if role == "advisor":
        _validate_advisor(response, context, allowed)
    else:
        _validate_explanation(response, context.selection_made, allowed)


def _validate_advisor(response: CandidatePlan, context, allowed: set[str]) -> None:
    candidates = response.candidates
    ids = [row.algorithm_id for row in candidates]
    compatible = {card.get("algorithm_id") for card in context.solver_cards}
    if len(ids) != context.candidate_count or len(set(ids)) != len(ids):
        raise RoleOutputError("candidates: wrong number of distinct algorithms")
    for row in candidates:
        if row.algorithm_id not in compatible or SOLVER_CONFIG_IDS.get(row.algorithm_id) != row.config_id:
            raise RoleOutputError("candidates: algorithm/config is not in the compatible Registry")
        if not row.rationale_fa.strip():
            raise RoleOutputError("candidates.rationale_fa: justification is required")
        if not row.evidence_refs or set(row.evidence_refs) - allowed:
            raise RoleOutputError("candidates.evidence_refs: missing or unknown evidence")


def _validate_explanation(
    response: ExplainerResponse, selection_made: bool, allowed: set[str]
) -> None:
    if not response.summary_fa.strip():
        raise RoleOutputError("summary_fa: explanation is empty")
    if not selection_made and response.selection_explanation_fa is not None:
        raise RoleOutputError("selection_explanation_fa: no selection was made")
    if not response.evidence_refs or set(response.evidence_refs) - allowed:
        raise RoleOutputError("evidence_refs: missing or unknown evidence")
    parts = [response.summary_fa, *response.limitations_fa]
    if response.selection_explanation_fa is not None:
        parts.append(response.selection_explanation_fa)
    for text in parts:
        if re.search(r"\d", text):
            raise RoleOutputError("explanation: refer to evidence instead of reproducing numbers")
        normalized = "".join(
            char for char in unicodedata.normalize("NFD", text)
            if not unicodedata.combining(char)
        ).replace("ي", "ی").replace("ك", "ک")
        if _CLAIM.search(_LIMITATION.sub("", normalized)):
            raise RoleOutputError("explanation: unsupported certainty or optimality claim")
