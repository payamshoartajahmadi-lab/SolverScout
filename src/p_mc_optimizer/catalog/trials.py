"""Build server-authorized version 2 numeric envelopes."""

from __future__ import annotations

from p_mc_optimizer.catalog.adapter import current_library_version, environment_id, seed_identity
from p_mc_optimizer.catalog.models import ADAPTER_VERSION, CatalogSnapshot, SolverConfiguration
from p_mc_optimizer.domain.models import EVALUATOR_VERSION, DomainError, ProblemRevision, TrialRequestV2


def _request_for_configuration(
    revision: ProblemRevision, *, configuration: SolverConfiguration, catalog_hash: str,
    job_id: str, trial_id: str, attempt_id: str, idempotency_key: str, seed: int, phase: str,
    replicate_index: int, evaluation_quota: int, deadline_seconds: float,
) -> TrialRequestV2:
    return TrialRequestV2(
        schema_version="2.0", job_id=job_id, trial_id=trial_id, attempt_id=attempt_id,
        idempotency_key=idempotency_key, problem_revision_id=revision.problem_revision_id,
        problem_math_hash=revision.problem_math_hash, evaluation_contract_hash=revision.evaluation_contract_hash,
        evaluator_version=EVALUATOR_VERSION, algorithm_id=configuration.implementation_id,
        implementation_id=configuration.implementation_id, config_id=configuration.config_id,
        config_hash=configuration.config_id, catalog_hash=catalog_hash,
        seed_identity=seed_identity(configuration.implementation_id),
        adapter_version=configuration.adapter_version, library_version=configuration.library_version,
        configuration_snapshot=configuration.kwargs(), boundary_repair=configuration.boundary_repair,
        mode=configuration.mode, seed=seed, phase=phase, replicate_index=replicate_index,
        evaluation_quota=evaluation_quota, deadline_seconds=deadline_seconds,
    )


def catalog_trial_request(
    revision: ProblemRevision, *, snapshot: CatalogSnapshot, implementation_id: str, config_id: str,
    job_id: str, trial_id: str, attempt_id: str, idempotency_key: str, seed: int, phase: str,
    replicate_index: int, evaluation_quota: int, deadline_seconds: float,
) -> TrialRequestV2:
    card = snapshot.get(implementation_id, config_id)
    if (
        card.library_version != current_library_version()
        or card.adapter_version != ADAPTER_VERSION
        or card.qualified_report.environment != environment_id()
    ):
        raise DomainError("SOLVER_CONFIGURATION_MISMATCH", "محیط عددی با کاتالوگ تثبیت‌شده سازگار نیست.")
    dimension = len(revision.spec.variables)
    if not card.min_dimension <= dimension <= card.max_dimension or (phase == "pilot" and not card.pilot_500_compatible):
        raise DomainError("CATALOG_QUALIFICATION_REQUIRED", "پیکربندی برای دامنه یا بودجهٔ این اجرا تأیید نشده است.")
    return _request_for_configuration(
        revision, configuration=card.configuration, catalog_hash=snapshot.catalog_hash,
        job_id=job_id, trial_id=trial_id, attempt_id=attempt_id, idempotency_key=idempotency_key,
        seed=seed, phase=phase, replicate_index=replicate_index, evaluation_quota=evaluation_quota,
        deadline_seconds=deadline_seconds,
    )


def apply_budget_configuration(base_trial, snapshot, authorization):
    """Add a frozen policy envelope while preserving all base selection identifiers."""
    from p_mc_optimizer.catalog.budget_policy import make_numeric_execution
    from p_mc_optimizer.catalog.configuration import Parameter
    from p_mc_optimizer.domain.models import TrialRequestBudgetV1

    base = SolverConfiguration(
        implementation_id=base_trial.implementation_id, library_version=base_trial.library_version,
        adapter_version=base_trial.adapter_version, mode=base_trial.mode, boundary_repair=base_trial.boundary_repair,
        parameters=tuple(Parameter(name=k,value=v) for k,v in sorted(base_trial.configuration_snapshot.items())),
    )
    numeric = make_numeric_execution(base,base_trial.evaluation_quota,authorization,snapshot)
    return TrialRequestBudgetV1.model_validate({
        **base_trial.model_dump(mode="python"),"schema_version":"2.1",
        "numeric_execution":numeric,
    })
