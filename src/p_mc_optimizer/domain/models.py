"""Versioned contracts shared by the API and the numerical engine."""

from __future__ import annotations

import json
import math
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError


SCHEMA_VERSION = "1.0"
EVALUATOR_VERSION = "1.0"
DSL_VERSION = "1.0"
SOLVER_CONFIG_IDS = {
    "abc.original": "abc.default.v1",
    "cma_es.mealpy": "cma_es.mealpy.default.v1",
    "de.original": "de.default.v1",
    "pso.original": "pso.default.v1",
}
LLM_POLICIES = frozenset({"llm_only", "llm_shortlist_then_pilot"})
PILOT_COUNTS = {"pilot_only": 4, "llm_shortlist_then_pilot": 3}
PREFLIGHT_EVALUATIONS = 8
PROFILE_EVALUATIONS = 492
PILOT_REPEATS = 3
PILOT_EVALUATIONS = 500
VERIFICATION_EVALUATIONS = 2
VERIFICATION_TIME_RESERVE_SECONDS = 10
NUMERIC_ATOL = 1e-12
NUMERIC_RTOL = 1e-10


class DomainError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def finite_real(value: object) -> float:
    if type(value) not in (int, float):
        raise ValueError("عدد حقیقی متناهی لازم است.")
    try:
        result = float(value)
    except OverflowError:
        raise ValueError("عدد از بازهٔ float64 بیرون است.") from None
    if not math.isfinite(result):
        raise ValueError("عدد حقیقی متناهی لازم است.")
    return result


def uuid4_string(value: str) -> str:
    try:
        parsed = UUID(value)
    except ValueError:
        raise ValueError("شناسهٔ UUID نسخهٔ ۴ لازم است.") from None
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("شناسهٔ UUID نسخهٔ ۴ لازم است.")
    return value


def strict_json_loads(text: str | bytes, *, limit: int | None = None):
    """Read one UTF-8 JSON value without nonfinite numbers or duplicate keys."""
    if isinstance(text, bytes):
        if limit is not None and len(text) > limit:
            raise ValueError("اندازهٔ ورودی JSON بیش از حد مجاز است.")
        text = text.decode("utf-8")
    elif limit is not None and len(text.encode("utf-8")) > limit:
        raise ValueError("اندازهٔ ورودی JSON بیش از حد مجاز است.")

    def unique_object(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("کلید تکراری در JSON مجاز نیست.")
            result[key] = value
        return result

    def reject_constant(_value):
        raise ValueError("عدد غیرمتناهی در JSON مجاز نیست.")

    try:
        return json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
            parse_float=lambda number: finite_real(float(number)),
        )
    except RecursionError:
        raise ValueError("عمق ورودی JSON بیش از حد مجاز است.") from None


# JSON Schema must expose the type even though conversion happens before validation.
FiniteReal = Annotated[float, BeforeValidator(finite_real, json_schema_input_type=float)]
Uuid4String = Annotated[str, AfterValidator(uuid4_string)]
Name = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,31}$")]
PolicyId = Literal["fixed_solver", "random_solver", "rule_based", "llm_only", "pilot_only", "llm_shortlist_then_pilot"]
SolverId = Literal["abc.original", "cma_es.mealpy", "de.original", "pso.original"]
JobStatus = Literal["queued", "running", "needs_input", "needs_configuration", "completed", "failed", "cancelled"]
JobPhase = Literal["intake", "formulating", "validating_input", "preflight", "profiling", "planning", "piloting", "selecting", "solving", "verifying", "reporting"]
EvaluationPhase = Literal["preflight", "profile", "pilot", "main", "verification"]
ExecutionStatus = Literal["queued", "running", "completed", "failed", "timed_out", "cancelled", "unknown_outcome"]
TerminationReason = Literal["evaluation_budget", "time_budget", "solver_stop", "execution_error", "validation_error", "insufficient_evidence", "llm_budget", "cancelled"]
SolutionStatus = Literal["pending", "valid_best_found", "no_valid_solution", "validation_failed", "validation_incomplete"]
ExplanationStatus = Literal["pending", "generated", "schema_fallback", "provider_error", "not_requested"]
SelectionStatus = Literal["pending", "selected", "fixed_by_policy", "insufficient_evidence", "not_reached"]


from p_mc_optimizer.domain.base import ContractModel


class Variable(ContractModel):
    name: Name
    label_fa: str | None = None
    type: Literal["real"]
    lower: FiniteReal
    upper: FiniteReal
    unit: str | None = None

    @model_validator(mode="before")
    @classmethod
    def check_type(cls, data):
        if isinstance(data, dict) and "type" in data and data["type"] != "real":
            raise PydanticCustomError("UNSUPPORTED_PROBLEM", "نسخهٔ اول فقط متغیر حقیقی را می‌پذیرد.")
        return data

    @model_validator(mode="after")
    def check_bounds(self):
        if self.lower >= self.upper:
            raise PydanticCustomError("UNSUPPORTED_PROBLEM", "کران پایین باید کوچک‌تر از کران بالا باشد.")
        if not math.isfinite(self.upper - self.lower) or math.nextafter(self.lower, self.upper) == self.upper:
            raise PydanticCustomError("BOUNDS_RANGE_UNSUPPORTED", "عرض یا تفکیک عددی بازه پشتیبانی نمی‌شود.")
        return self


class ExpressionSource(ContractModel):
    kind: Literal["expression"]
    expression: str


class RegisteredFunctionSource(ContractModel):
    kind: Literal["registered_function"]
    function_id: Name
    function_version: str


class Objective(ContractModel):
    sense: Literal["minimize", "maximize"]
    unit: str | None = None
    source: Annotated[ExpressionSource | RegisteredFunctionSource, Field(discriminator="kind")]


class SourceMatch(ContractModel):
    start: Annotated[int, Field(ge=0)]
    end: Annotated[int, Field(gt=0)]


class SourceEvidence(ContractModel):
    draft_field_path: str | None = None
    problem_field_path: str | None
    source_ref: str
    quote: str | None = None
    matches: list[SourceMatch] = Field(default_factory=list)
    match_count: int = 0
    source_sha256: str | None = None


class ProblemInput(ContractModel):
    schema_version: Literal["1.0"]
    variables: Annotated[list[Variable], Field(min_length=2, max_length=30)]
    objective: Objective
    parameters: dict[Name, FiniteReal]
    constraints: Annotated[list[object], Field(max_length=0)]
    deterministic: Literal[True]

    @model_validator(mode="before")
    @classmethod
    def check_scope(cls, data):
        if not isinstance(data, dict):
            return data
        variables = data.get("variables")
        if isinstance(variables, list) and not 2 <= len(variables) <= 30:
            raise PydanticCustomError("UNSUPPORTED_PROBLEM", "تعداد متغیرها باید ۲ تا ۳۰ باشد.")
        if isinstance(data.get("objective"), list) or "objectives" in data:
            raise PydanticCustomError("UNSUPPORTED_PROBLEM", "نسخهٔ اول فقط مسئلهٔ تک‌هدفه را می‌پذیرد.")
        if isinstance(data.get("constraints"), list) and data["constraints"]:
            raise PydanticCustomError("UNSUPPORTED_PROBLEM", "قید عمومی در نسخهٔ اول پشتیبانی نمی‌شود.")
        if "deterministic" in data and data["deterministic"] is not True:
            raise PydanticCustomError("UNSUPPORTED_PROBLEM", "تابع هدف باید قطعی باشد.")
        return data

    @model_validator(mode="after")
    def check_math(self):
        from p_mc_optimizer.problems.registry import validate_problem_math

        try:
            validate_problem_math(self)
        except DomainError as error:
            raise PydanticCustomError(error.code, str(error)) from None
        return self


class ProblemSpec(ProblemInput):
    source_map: list[SourceEvidence]


class ProblemRevision(ContractModel):
    problem_revision_id: Uuid4String
    problem_id: Uuid4String
    revision_no: Annotated[int, Field(gt=0)]
    spec: ProblemSpec
    problem_math_hash: str
    evaluation_contract_hash: str
    created_at: datetime


class ExecutionPolicyInput(ContractModel):
    policy_version: Literal["1.0"]
    portfolio_id: Literal["continuous_v1"]
    selection_policy: PolicyId
    fixed_algorithm_id: SolverId | None
    max_objective_evaluations: Annotated[int, Field(ge=1002, le=1_000_000)]
    max_compute_seconds: Annotated[int, Field(ge=11, le=3600)]
    master_seed: Annotated[int, Field(ge=0, lt=2**32)]
    explain_via_llm: bool

    @model_validator(mode="after")
    def check_policy(self):
        if (self.selection_policy == "fixed_solver") != (self.fixed_algorithm_id is not None):
            raise ValueError("فقط سیاست fixed_solver به شناسهٔ روش ثابت نیاز دارد.")
        if self.explain_via_llm and self.selection_policy not in LLM_POLICIES:
            raise ValueError("سیاست پایه از مدل زبانی استفاده نمی‌کند.")
        if self.main_evaluations < 500:
            raise ValueError("بودجهٔ اجرای اصلی باید دست‌کم ۵۰۰ ارزیابی باشد.")
        return self

    @property
    def main_evaluations(self) -> int:
        pilot = PILOT_COUNTS.get(self.selection_policy, 0) * PILOT_REPEATS * PILOT_EVALUATIONS
        return self.max_objective_evaluations - PREFLIGHT_EVALUATIONS - PROFILE_EVALUATIONS - pilot - VERIFICATION_EVALUATIONS


class ExecutionPolicy(ExecutionPolicyInput):
    preflight_evaluations: Literal[8] = PREFLIGHT_EVALUATIONS
    profile_evaluations: Literal[492] = PROFILE_EVALUATIONS
    pilot_repeats: Literal[3] = PILOT_REPEATS
    pilot_evaluations: Literal[500] = PILOT_EVALUATIONS
    verification_evaluations: Literal[2] = VERIFICATION_EVALUATIONS
    verification_time_reserve_seconds: Literal[10] = VERIFICATION_TIME_RESERVE_SECONDS
    max_llm_calls: Literal[6] = 6
    max_llm_calls_per_role: Literal[2] = 2
    max_trial_attempts: Literal[1] = 1
    numeric_atol: Literal[1e-12] = NUMERIC_ATOL
    numeric_rtol: Literal[1e-10] = NUMERIC_RTOL
    use_history: Literal[False] = False
    pilot_candidate_count: Literal[0, 3, 4]

    @classmethod
    def from_input(cls, policy: ExecutionPolicyInput) -> ExecutionPolicy:
        data = policy.model_dump()
        data["pilot_candidate_count"] = PILOT_COUNTS.get(policy.selection_policy, 0)
        return cls.model_validate(data)

    @model_validator(mode="after")
    def check_pilot_count(self):
        if self.pilot_candidate_count != PILOT_COUNTS.get(self.selection_policy, 0):
            raise ValueError("تعداد کاندید پایلوت با سیاست سازگار نیست.")
        return self


class NaturalLanguageInput(ContractModel):
    kind: Literal["natural_language"]
    text: Annotated[str, Field(min_length=1, max_length=16000)]

    @model_validator(mode="after")
    def check_text(self):
        if not self.text.strip():
            raise ValueError("متن مسئله خالی است.")
        return self


class StructuredProblemInput(ContractModel):
    kind: Literal["structured_problem"]
    problem: ProblemInput


class ProblemRevisionInput(ContractModel):
    kind: Literal["problem_revision"]
    problem_revision_id: Uuid4String


class ProfileReference(ContractModel):
    profile_id: Uuid4String
    revision: Annotated[int, Field(gt=0)]


JobInput = Annotated[NaturalLanguageInput | StructuredProblemInput | ProblemRevisionInput, Field(discriminator="kind")]


class CreateJob(ContractModel):
    input: JobInput
    policy: ExecutionPolicyInput
    llm_profile: ProfileReference | None

    @model_validator(mode="after")
    def check_llm(self):
        if self.policy.selection_policy not in LLM_POLICIES:
            if isinstance(self.input, NaturalLanguageInput):
                raise ValueError("برای متن طبیعی سیاست دارای صورت‌بند را انتخاب کنید.")
            if self.llm_profile is not None:
                raise ValueError("سیاست پایه پروفایل مدل نمی‌پذیرد.")
        elif self.llm_profile is None:
            raise PydanticCustomError("PROVIDER_CONFIGURATION_REQUIRED", "برای سیاست دارای مدل زبانی پروفایل را مشخص کنید.")
        return self


class Candidate(ContractModel):
    algorithm_id: str
    config_id: str
    rationale_fa: str
    evidence_refs: list[str]


class CandidatePlan(ContractModel):
    candidates: list[Candidate]
    uncertainty_notes: str | None


AdvisorResponse = CandidatePlan


ImplementationId = Annotated[str, Field(
    max_length=240, pattern=r"^[a-z][a-z0-9_-]*:[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$",
)]
Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ConstructorValue = int | float | bool | str | None


class _TrialFields(ContractModel):
    job_id: Uuid4String
    trial_id: Uuid4String
    attempt_id: Uuid4String
    idempotency_key: str
    problem_revision_id: Uuid4String
    problem_math_hash: str
    evaluation_contract_hash: str
    evaluator_version: Literal["1.0"]
    algorithm_id: str
    config_id: str
    adapter_version: str
    library_version: str
    configuration_snapshot: dict[str, int | float]
    seed: Annotated[int, Field(ge=0, lt=2**32)]
    phase: Literal["pilot", "main"]
    replicate_index: Annotated[int, Field(ge=0, le=2)]
    evaluation_quota: Annotated[int, Field(gt=0)]
    deadline_seconds: Annotated[FiniteReal, Field(gt=0)]
    start_mode: Literal["fresh"] = "fresh"

    @model_validator(mode="after")
    def check_replicate(self):
        if self.phase == "main" and self.replicate_index != 0:
            raise ValueError("اجرای اصلی فقط یک تکرار دارد.")
        return self


class TrialRequest(_TrialFields):
    algorithm_id: SolverId

    @model_validator(mode="after")
    def check_config(self):
        if self.config_id != SOLVER_CONFIG_IDS[self.algorithm_id]:
            raise ValueError("شناسهٔ پیکربندی با روش سازگار نیست.")
        return self


class TrialRequestV2(_TrialFields):
    schema_version: Literal["2.0"]
    algorithm_id: ImplementationId
    implementation_id: ImplementationId
    config_id: Sha256Hex
    config_hash: Sha256Hex
    catalog_hash: Sha256Hex
    configuration_snapshot: dict[str, ConstructorValue]
    seed_identity: str
    boundary_repair: str
    mode: Literal["single"]

    @model_validator(mode="after")
    def check_snapshot(self):
        if self.algorithm_id != self.implementation_id or self.config_id != self.config_hash:
            raise ValueError("شناسهٔ روش یا پیکربندی با snapshot یکسان نیست.")
        if self.phase == "pilot" and self.evaluation_quota != PILOT_EVALUATIONS:
            raise ValueError("سهم هر پایلوت نسخهٔ دوم دقیقاً ۵۰۰ ارزیابی است.")
        return self


from p_mc_optimizer.catalog.budget_models import NumericExecutionV1


class TrialRequestBudgetV1(TrialRequestV2):
    schema_version: Literal["2.1"]
    numeric_execution: NumericExecutionV1

    @model_validator(mode="after")
    def matching_numeric_execution(self):
        if self.numeric_execution.base_config_id != self.config_id or self.numeric_execution.evaluation_quota != self.evaluation_quota:
            raise ValueError("Numeric policy base configuration and quota must match the Trial")
        return self


def parse_catalog_trial(value):
    if isinstance(value, TrialRequestV2):
        return value
    cls = TrialRequestBudgetV1 if value.get("schema_version") == "2.1" else TrialRequestV2
    return cls.model_validate_json(json.dumps(value,ensure_ascii=False,allow_nan=False))


class EvaluationRecord(ContractModel):
    job_id: Uuid4String
    execution_id: Uuid4String
    trial_id: Uuid4String | None
    attempt_id: Uuid4String | None
    stage_execution_id: Uuid4String | None
    evaluation_id: Uuid4String
    sequence: Annotated[int, Field(gt=0)]
    phase: EvaluationPhase
    evaluation_kind: Literal["objective", "library_setup"]
    x: list[FiniteReal]
    objective_raw: FiniteReal | None
    score_for_minimization: FiniteReal | None
    status: Literal["successful", "failed"]
    evaluation_seconds: Annotated[FiniteReal, Field(ge=0)]
    problem_math_hash: str
    evaluator_version: Literal["1.0"]
    error_code: str | None = None

    @model_validator(mode="after")
    def check_origin(self):
        if self.phase in {"pilot", "main"}:
            if self.trial_id is None or self.attempt_id != self.execution_id or self.stage_execution_id is not None:
                raise ValueError("شناسهٔ منشأ Trial نامعتبر است.")
        elif self.trial_id is not None or self.attempt_id is not None or self.stage_execution_id != self.execution_id:
            raise ValueError("شناسهٔ منشأ مرحله نامعتبر است.")
        if self.status == "successful":
            if self.objective_raw is None or self.score_for_minimization is None or self.error_code is not None:
                raise ValueError("ارزیابی موفق به مقدار هدف معتبر نیاز دارد.")
        elif self.objective_raw is not None or self.score_for_minimization is not None or self.error_code is None:
            raise ValueError("ارزیابی ناموفق باید کد خطا و مقادیر null داشته باشد.")
        return self


class ExecutionRequest(ContractModel):
    job_id: Uuid4String
    execution_id: Uuid4String
    phase: EvaluationPhase
    revision: ProblemRevision
    seed: Annotated[int, Field(ge=0, lt=2**32)]
    evaluation_quota: Annotated[int, Field(gt=0)]
    deadline_seconds: Annotated[FiniteReal, Field(gt=0)]
    trial: TrialRequestBudgetV1 | TrialRequest | TrialRequestV2 | None = None
    verification_reference: EvaluationRecord | None = None

    @model_validator(mode="after")
    def check_identity(self):
        if self.phase in {"pilot", "main"}:
            trial = self.trial
            if trial is None or (
                trial.job_id != self.job_id
                or trial.attempt_id != self.execution_id
                or trial.phase != self.phase
                or trial.problem_revision_id != self.revision.problem_revision_id
                or trial.problem_math_hash != self.revision.problem_math_hash
                or trial.evaluation_contract_hash != self.revision.evaluation_contract_hash
                or trial.seed != self.seed
                or trial.evaluation_quota != self.evaluation_quota
                or trial.deadline_seconds != self.deadline_seconds
            ):
                raise ValueError("اجرای عددی به snapshot و Trial همان درخواست تعلق ندارد.")
        elif self.trial is not None:
            raise ValueError("مرحلهٔ عمومی شناسهٔ Trial نمی‌پذیرد.")
        if (self.phase == "verification") != (self.verification_reference is not None):
            raise ValueError("فقط verification به ارزیابی مرجع نیاز دارد.")
        return self


class ProblemProfile(ContractModel):
    n_variables: int
    lower: list[FiniteReal]
    upper: list[FiniteReal]
    widths: list[FiniteReal]
    objective_sense: Literal["minimize", "maximize"]
    sample_count: int
    objective_scale: FiniteReal
    score_min: FiniteReal
    score_median: FiniteReal
    score_max: FiniteReal
    normalized_iqr: FiniteReal
    normalized_std: FiniteReal
    mean_evaluation_seconds: FiniteReal
    sample_artifact_id: Uuid4String | None = None


class ValidationResult(ContractModel):
    validation_status: Literal["pending", "passed", "failed", "incomplete", "not_applicable"]
    reference_evaluation_id: Uuid4String
    reference_objective_raw: FiniteReal
    repeat_objective_raw: list[FiniteReal]
    absolute_differences: list[FiniteReal | None]
    numeric_atol: FiniteReal = NUMERIC_ATOL
    numeric_rtol: FiniteReal = NUMERIC_RTOL


class ExecutionError(ContractModel):
    error_code: str
    message_fa: str
    technical_type: str | None = None
    technical_message: str | None = None


class ExecutionResult(ContractModel):
    job_id: Uuid4String
    execution_id: Uuid4String
    phase: EvaluationPhase
    execution_status: ExecutionStatus
    termination_reason: TerminationReason
    best: EvaluationRecord | None
    started_evaluations: Annotated[int, Field(ge=0)]
    successful_evaluations: Annotated[int, Field(ge=0)]
    failed_evaluations: Annotated[int, Field(ge=0)]
    rejected_proposals: Annotated[int, Field(ge=0)]
    elapsed_compute_seconds: Annotated[FiniteReal, Field(ge=0)]
    overshoot_seconds: Annotated[FiniteReal, Field(ge=0)]
    history_complete: bool
    history_sha256: str | None
    history_artifact_id: Uuid4String | None = None
    profile_features: ProblemProfile | None
    validation: ValidationResult | None
    error: ExecutionError | None
    fingerprint: dict[str, str]
    runtime_provenance: dict | None = None


class PilotMetric(ContractModel):
    algorithm_id: SolverId
    eligible: bool
    trial_ids: list[Uuid4String]
    median_score: FiniteReal | None
    normalized_iqr: FiniteReal | None
    reason: str | None


class SelectionDecision(ContractModel):
    decision_id: Uuid4String
    policy_version: Literal["1.0"] = "1.0"
    selection_status: SelectionStatus
    decision_source: str
    selected_algorithm_id: SolverId | None
    selected_config_id: str | None
    metrics: list[PilotMetric]
    scale: FiniteReal | None
    tie_break_order: list[str]
    uncertainty_notes: str | None


class PilotMetricV2(PilotMetric):
    algorithm_id: ImplementationId


class SelectionDecisionV2(SelectionDecision):
    policy_version: Literal["2.0"] = "2.0"
    selected_algorithm_id: ImplementationId | None
    metrics: list[PilotMetricV2]
