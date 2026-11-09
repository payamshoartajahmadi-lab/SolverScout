"""Trusted objectives and immutable math/evaluation fingerprints."""

from __future__ import annotations

import hashlib
import inspect
import json
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from p_mc_optimizer.domain.models import (
    DSL_VERSION,
    EVALUATOR_VERSION,
    DomainError,
    ExpressionSource,
    ProblemInput,
    ProblemRevision,
    ProblemSpec,
    SourceEvidence,
)
from p_mc_optimizer.problems.dsl import RESERVED_NAMES, parse_expression


def sphere(x: Iterable[float]) -> float:
    return math.fsum(value ** 2 for value in x)


def ackley(x: Iterable[float]) -> float:
    values = tuple(x)
    n = len(values)
    return (
        -20 * math.exp(-0.2 * math.sqrt(math.fsum(value ** 2 for value in values) / n))
        - math.exp(math.fsum(math.cos(2 * math.pi * value) for value in values) / n)
        + 20 + math.e
    )


def rosenbrock(x: Iterable[float]) -> float:
    values = tuple(x)
    return math.fsum(100 * (right - left ** 2) ** 2 + (1 - left) ** 2 for left, right in zip(values, values[1:]))


def rastrigin(x: Iterable[float]) -> float:
    values = tuple(x)
    return 10 * len(values) + math.fsum(value ** 2 - 10 * math.cos(2 * math.pi * value) for value in values)


@dataclass(frozen=True)
class RegisteredObjective:
    function_id: str
    function: Callable[[Iterable[float]], float]
    default_bounds: tuple[float, float]
    function_version: str = "1.0"
    min_dimension: int = 2
    max_dimension: int = 30
    deterministic: bool = True

    @property
    def implementation_hash(self) -> str:
        return hashlib.sha256(inspect.getsource(self.function).encode("utf-8")).hexdigest()


REGISTERED_OBJECTIVES = {
    item.function_id: item
    for item in (
        RegisteredObjective("sphere", sphere, (-5.0, 5.0)),
        RegisteredObjective("ackley", ackley, (-32.768, 32.768)),
        RegisteredObjective("rosenbrock", rosenbrock, (-3.0, 3.0)),
        RegisteredObjective("rastrigin", rastrigin, (-5.12, 5.12)),
    )
}


def registered_objective(problem: ProblemInput) -> RegisteredObjective:
    source = problem.objective.source
    item = REGISTERED_OBJECTIVES.get(source.function_id)
    if item is None or item.function_version != source.function_version:
        raise DomainError("UNSUPPORTED_PROBLEM", "تابع یا نسخهٔ آن در Registry ثبت نشده است.")
    if not item.deterministic or not item.min_dimension <= len(problem.variables) <= item.max_dimension:
        raise DomainError("UNSUPPORTED_PROBLEM", "ابعاد یا قطعیت تابع با قرارداد ثبت‌شده سازگار نیست.")
    if problem.parameters:
        raise DomainError("UNSUPPORTED_PROBLEM", "تابع ثبت‌شده پارامتر ورودی نمی‌پذیرد.")
    return item


def validate_problem_math(problem: ProblemInput) -> None:
    names = [variable.name for variable in problem.variables]
    if len(names) != len(set(names)):
        raise DomainError("UNSUPPORTED_PROBLEM", "نام متغیرها باید یکتا باشد.")
    if (set(names) & problem.parameters.keys()) or ((set(names) | problem.parameters.keys()) & RESERVED_NAMES):
        raise DomainError("UNSUPPORTED_PROBLEM", "نام متغیر یا پارامتر با یک نام رزروشده یا تعریف‌شده تداخل دارد.")
    if isinstance(problem.objective.source, ExpressionSource):
        parse_expression(problem.objective.source.expression, set(names) | problem.parameters.keys())
    else:
        registered_objective(problem)


def _normalize_negative_zero(value):
    if isinstance(value, dict):
        return {key: _normalize_negative_zero(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_negative_zero(item) for item in value]
    if type(value) is float and value == 0:
        return 0.0
    return value


def canonical_json(value) -> str:
    return json.dumps(_normalize_negative_zero(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def content_hash(value) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def problem_hashes(problem: ProblemInput) -> tuple[str, str]:
    source = problem.objective.source
    names = {variable.name for variable in problem.variables}
    registered = None
    if isinstance(source, ExpressionSource):
        ast = parse_expression(source.expression, names | problem.parameters.keys()).canonical
        canonical_source = {"kind": "expression", "ast": ast}
    else:
        item = registered_objective(problem)
        canonical_source = source.model_dump(mode="json")
        registered = canonical_source | {"implementation_hash": item.implementation_hash}
    problem_math_hash = content_hash({
        "schema_version": problem.schema_version,
        "variables": [
            variable.model_dump(include={"name", "type", "lower", "upper"})
            for variable in problem.variables
        ],
        "objective": {"sense": problem.objective.sense, "source": canonical_source},
        "parameters": problem.parameters,
        "constraints": problem.constraints,
        "deterministic": problem.deterministic,
    })
    evaluation_contract_hash = content_hash({
        "problem_math_hash": problem_math_hash,
        "dsl_version": DSL_VERSION,
        "evaluator_version": EVALUATOR_VERSION,
        "dtype": "float64",
        "registered_function": registered,
    })
    return problem_math_hash, evaluation_contract_hash


def _pointer_component(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _field_paths(value, path=""):
    if isinstance(value, dict) and value:
        for key, item in value.items():
            yield from _field_paths(item, path + "/" + _pointer_component(key))
    elif isinstance(value, list) and value:
        for index, item in enumerate(value):
            yield from _field_paths(item, path + "/" + str(index))
    else:
        yield path


def freeze_problem(
    problem: ProblemInput,
    *,
    source_map: list[SourceEvidence | dict] | None = None,
    problem_id: str | None = None,
    revision_no: int = 1,
) -> ProblemRevision:
    # Rebuild from plain data to detach from potentially mutable caller-owned containers.
    problem_payload = problem.model_dump(mode="json")
    if source_map is None:
        source_hash = content_hash(problem_payload)
        source_map = [
            SourceEvidence(problem_field_path=path, source_ref="input:structured_problem", source_sha256=source_hash)
            for path in _field_paths(problem_payload)
        ]
    spec = ProblemSpec.model_validate(problem_payload | {"source_map": source_map})
    math_hash, evaluation_hash = problem_hashes(spec)
    return ProblemRevision(
        problem_revision_id=str(uuid4()),
        problem_id=problem_id or str(uuid4()),
        revision_no=revision_no,
        spec=spec,
        problem_math_hash=math_hash,
        evaluation_contract_hash=evaluation_hash,
        created_at=datetime.now(timezone.utc),
    )


def objective_for(spec: ProblemSpec) -> Callable[[Iterable[float]], float]:
    if not isinstance(spec.objective.source, ExpressionSource):
        function = registered_objective(spec).function
        return lambda x: function(float(value) for value in x)

    names = [variable.name for variable in spec.variables]
    parameters = spec.parameters.copy()
    expression = parse_expression(spec.objective.source.expression, set(names) | parameters.keys())

    def evaluate(x: Iterable[float]) -> float:
        return expression.evaluate(parameters | dict(zip(names, map(float, x), strict=True)))

    return evaluate
