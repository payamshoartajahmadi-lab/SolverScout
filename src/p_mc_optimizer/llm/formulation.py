"""Trace LLM-proposed values to user text and construct a deterministic draft."""
from __future__ import annotations

import re
from collections.abc import Mapping
from math import isfinite
from typing import Any
from uuid import uuid4

from p_mc_optimizer.llm.models import FormulatorResponse


class RoleOutputError(ValueError):
    """Output contract violation suitable for a bounded schema repair."""

    def __init__(self, *field_errors: str):
        super().__init__("Role output violates its contract")
        self.field_errors = list(field_errors)


def _unescape(segment: str) -> str:
    if re.search(r"~(?![01])", segment):
        raise RoleOutputError("source_map.field_path: invalid JSON Pointer escape")
    return segment.replace("~1", "/").replace("~0", "~")


def _escape(segment: str) -> str:
    return segment.replace("~", "~0").replace("/", "~1")


def _pointer_value(root: dict, pointer: str, *, allow_unknown: bool = False) -> Any:
    if not pointer.startswith("/") or len(pointer) > 300:
        raise RoleOutputError("field_path: expected a JSON Pointer to extracted_fields")
    value: Any = root
    for segment in pointer[1:].split("/"):
        token = _unescape(segment)
        if value is None and allow_unknown:
            return None
        if isinstance(value, dict) and token in value:
            value = value[token]
        elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]{0,3}", token):
            index = int(token)
            if index >= len(value):
                raise RoleOutputError("field_path: list index is out of range")
            value = value[index]
        else:
            raise RoleOutputError("field_path: unknown field")
    return value


def _find_quotes(text: str, quote: str) -> list[dict[str, int]]:
    if not quote:
        return []
    return [
        {"start": match.start(), "end": match.start() + len(quote)}
        for match in re.finditer(f"(?={re.escape(quote)})", text)
    ]


_ASCII_NUMBER = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩−٫", "01234567890123456789-.")
_NUMBER = re.compile(r"(?<![\w.])[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?(?![\w.])")


def _quote_contains_number(quote: str, value: float) -> bool:
    for token in _NUMBER.findall(quote.translate(_ASCII_NUMBER)):
        parsed = float(token)
        if isfinite(parsed) and parsed == value:
            return True
    return False


def _verify_numeric_evidence(pointer: str, quote: str, value: Any) -> None:
    if type(value) in (int, float) and not _quote_contains_number(quote, value):
        raise RoleOutputError(f"{pointer}: the number does not occur in the source quote")


def _problem_pointer(pointer: str, extracted: dict) -> str | None:
    objective_prefix = "/objective/"
    if pointer.startswith(objective_prefix):
        name = pointer.removeprefix(objective_prefix)
        if name == "source_kind":
            return "/objective/source/kind"
        if name in {"expression", "function_id", "function_version"}:
            return f"/objective/source/{name}"
    parts = pointer.split("/")
    if len(parts) == 4 and parts[1] == "parameters" and parts[3] in {"name", "value"}:
        name = extracted["parameters"][int(parts[2])]["name"]
        return f"/parameters/{_escape(name)}"
    if pointer.startswith("/constraints/"):
        return None
    return pointer


def _required_paths(extracted: dict) -> list[str]:
    fields = ["/variables", "/objective", "/parameters", "/constraints", "/deterministic"]
    for index, _variable in enumerate(extracted["variables"] or []):
        fields.extend(f"/variables/{index}/{field}" for field in ("name", "type", "lower", "upper"))
    objective = extracted["objective"]
    if objective is not None:
        fields.extend(("/objective/sense", "/objective/source_kind"))
        if objective["source_kind"] == "expression":
            fields.append("/objective/expression")
        elif objective["source_kind"] == "registered_function":
            fields.extend(("/objective/function_id", "/objective/function_version"))
    for index, _parameter in enumerate(extracted["parameters"] or []):
        fields.append(f"/parameters/{index}/value")
    return fields


def _observed_paths(extracted: dict) -> list[str]:
    paths = [
        path
        for path in _required_paths(extracted)
        if path not in {
            "/variables", "/objective", "/parameters", "/constraints",
            "/deterministic", "/objective/source_kind", "/objective/function_version",
        }
    ]
    paths.extend(f"/parameters/{index}/name" for index, _ in enumerate(extracted["parameters"] or []))
    paths.extend(f"/constraints/{index}/source_text" for index, _ in enumerate(extracted["constraints"] or []))
    paths.extend(f"/variables/{index}/unit" for index, _ in enumerate(extracted["variables"] or []))
    if extracted["objective"] is not None:
        paths.append("/objective/unit")
    return [path for path in paths if _pointer_value(extracted, path) is not None]


def convert_formulation(
    response: FormulatorResponse | dict, sources: Mapping[str, str]
) -> dict:
    """Return workflow data plus the candidate ProblemInput payload when complete."""
    dto = FormulatorResponse.model_validate(response)
    extracted = dto.extracted_fields.model_dump(mode="json")
    parameters = extracted["parameters"] or []
    names = [entry["name"] for entry in parameters]
    if len(set(names)) != len(names):
        raise RoleOutputError("extracted_fields.parameters: names must be unique")

    source_map = []
    supported_paths = set()
    evidence_refs = set(sources)
    for item in dto.source_map:
        value = _pointer_value(extracted, item.field_path)
        if value is None:
            raise RoleOutputError("source_map: evidence cannot populate a null field")
        if item.source_ref not in sources:
            raise RoleOutputError("source_map.source_ref: unknown source identifier")
        matches = _find_quotes(sources[item.source_ref], item.quote)
        if not matches:
            raise RoleOutputError("source_map.quote: quote does not occur in source")
        _verify_numeric_evidence(item.field_path, item.quote, value)
        supported_paths.add(item.field_path)
        evidence_refs.add(f"field:{item.field_path}")
        source_map.append({
            "draft_field_path": item.field_path,
            "problem_field_path": _problem_pointer(item.field_path, extracted),
            "source_ref": item.source_ref,
            "quote": item.quote,
            "matches": matches,
            "match_count": len(matches),
        })

    for item in dto.normalizations:
        value = _pointer_value(extracted, item.field_path)
        normalized = str(value).lower() if isinstance(value, bool) else str(value)
        normalized_matches = item.normalized_text == normalized
        if type(value) in (int, float) and _NUMBER.fullmatch(item.normalized_text):
            normalized_matches = float(item.normalized_text) == value
        if (
            value is None or not normalized_matches
            or not item.reason_fa.strip() or not item.source_text.strip()
        ):
            raise RoleOutputError("normalizations: output does not match normalized field")
        matching_sources = [
            (source_ref, _find_quotes(text, item.source_text))
            for source_ref, text in sources.items()
        ]
        if not any(matches for _, matches in matching_sources):
            raise RoleOutputError("normalizations.source_text: quote does not occur in sources")
        _verify_numeric_evidence(item.field_path, item.source_text, value)
        supported_paths.add(item.field_path)
        for source_ref, matches in matching_sources:
            if matches:
                source_map.append({
                    "draft_field_path": item.field_path,
                    "problem_field_path": _problem_pointer(item.field_path, extracted),
                    "source_ref": source_ref,
                    "quote": item.source_text,
                    "matches": matches,
                    "match_count": len(matches),
                })

    unwitnessed = set(_observed_paths(extracted)) - supported_paths
    if unwitnessed:
        raise RoleOutputError(*(f"{path}: source evidence is required" for path in sorted(unwitnessed)))

    ambiguities = []
    for question in dto.clarification_questions:
        if not question.field_paths or not question.question_fa.strip():
            raise RoleOutputError("clarification_questions: include paths and a question")
        for path in question.field_paths:
            _pointer_value(extracted, path, allow_unknown=True)
            ambiguities.append({
                "question_id": str(uuid4()),
                "field_path": path,
                "question_fa": question.question_fa,
                "reason": question.reason,
            })

    missing = [path for path in _required_paths(extracted) if _pointer_value(extracted, path) is None]
    requested = {item["field_path"] for item in ambiguities}
    for path in missing:
        if path not in requested:
            ambiguities.append({
                "question_id": str(uuid4()),
                "field_path": path,
                "question_fa": f"مقدار مشخصِ {path} چیست؟",
                "reason": "missing",
            })

    unsupported = [item.model_dump(mode="json") for item in dto.unsupported_features]
    for feature in unsupported:
        if not feature["evidence_refs"] or set(feature["evidence_refs"]) - evidence_refs:
            raise RoleOutputError("unsupported_features.evidence_refs: missing or unknown evidence")

    variables = extracted["variables"]
    if variables is not None and not 2 <= len(variables) <= 30:
        if not ambiguities:
            unsupported.append({"kind": "dimension", "description_fa": "تعداد متغیرها باید از ۲ تا ۳۰ باشد.", "evidence_refs": []})
    if any(variable["type"] not in {None, "real"} for variable in variables or []):
        unsupported.append({"kind": "non_real", "description_fa": "فقط متغیر حقیقی در دامنهٔ نسخهٔ اول است.", "evidence_refs": []})
    if extracted["constraints"]:
        unsupported.append({"kind": "constraint", "description_fa": "قید عمومی در دامنهٔ نسخهٔ اول نیست.", "evidence_refs": []})
    if extracted["deterministic"] is False:
        unsupported.append({"kind": "nondeterministic", "description_fa": "تابع هدف باید قطعی باشد.", "evidence_refs": []})

    problem = None
    if not (missing or ambiguities or unsupported):
        objective = extracted["objective"]
        source = {"kind": objective["source_kind"]}
        if source["kind"] == "expression":
            source["expression"] = objective["expression"]
        else:
            source.update(
                function_id=objective["function_id"], function_version=objective["function_version"]
            )
        problem = {
            "schema_version": "1.0",
            "variables": variables,
            "objective": {"sense": objective["sense"], "unit": objective["unit"], "source": source},
            "parameters": {entry["name"]: entry["value"] for entry in parameters},
            "constraints": [],
            "deterministic": True,
        }

    return {
        "problem": problem,
        "missing_fields": missing,
        "ambiguities": ambiguities,
        "extracted_fields": extracted,
        "source_map": source_map,
        "normalizations": [item.model_dump(mode="json") for item in dto.normalizations],
        "unsupported_features": unsupported,
    }
