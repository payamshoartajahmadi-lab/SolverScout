"""Pure budget derivation and process-frozen numerical execution identity."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
from importlib.metadata import version
import os
from pathlib import Path
import platform

from p_mc_optimizer.catalog.budget_models import NumericExecutionV1, NumericRuntimeFingerprintV1
from p_mc_optimizer.catalog.configuration import Parameter, SolverConfiguration, canonical_json, digest

ALLOWLIST = (
    "mealpy:swarm_based.GWO:OriginalGWO",
    "mealpy:swarm_based.WOA:OriginalWOA",
)
POLICY_DEFINITION = {
    "schema_version":"1.0", "policy_id":"budget_horizon.v1", "rule_id":"linear_population_horizon.v1",
    "library":"mealpy", "library_version":"3.0.3", "mode":"single", "implementations":list(ALLOWLIST),
    "setup_evaluations":1, "initialization_rule":"pop_size", "generation_evaluations_rule":"pop_size",
    "epoch_rule":"min_base_epoch_ceil_remaining_over_population", "insufficient_initialization_budget":"reject",
    "non_allowlisted":"passthrough", "pop_size_min":5, "pop_size_max":10000, "base_epoch_min":1, "base_epoch_max":100000,
}
POLICY_HASH = digest(POLICY_DEFINITION)
_NUMERIC_FILES = (
    "domain/models.py", "domain/base.py", "execution/evaluator.py", "execution/supervisor.py",
    "solvers/registry.py", "catalog/adapter.py", "catalog/trials.py", "catalog/budget_models.py",
    "catalog/budget_policy.py", "catalog/configuration.py",
)
_FINGERPRINTS = {}


def _error(code, message):
    from p_mc_optimizer.domain.models import DomainError
    return DomainError(code, message)


def _manifest(root, paths):
    return [{"path":name, "sha256":hashlib.sha256((root/name).read_bytes()).hexdigest()}
            for name in sorted(paths)]


def scan_numeric_runtime_fingerprint():
    from p_mc_optimizer.catalog.adapter import environment_id
    import mealpy
    mealpy_root = Path(mealpy.__file__).resolve().parent
    package = Path(__file__).resolve().parents[1]
    library_paths = [p.relative_to(mealpy_root).as_posix() for p in mealpy_root.rglob("*.py")]
    result = {
        "schema_version":"1.0", "environment_id":environment_id(),
        "python_major_minor":".".join(platform.python_version_tuple()[:2]),
        "platform_system":platform.system(), "architecture":platform.machine(),
        "numpy_version":version("numpy"), "mealpy_version":version("mealpy"),
        "dtype":"float64", "rng":"PCG64", "mode":"single",
        "mealpy_source_manifest_sha256":digest(_manifest(mealpy_root,library_paths)),
        "numeric_source_manifest_sha256":digest(_manifest(package,_NUMERIC_FILES)),
    }
    return NumericRuntimeFingerprintV1.model_validate(result).model_dump(mode="json")


def numeric_runtime_fingerprint():
    pid = os.getpid()
    if pid not in _FINGERPRINTS:
        try:
            _FINGERPRINTS[pid] = scan_numeric_runtime_fingerprint()
        except (OSError, ValueError, ImportError) as exc:
            raise _error("NUMERIC_POLICY_ENVIRONMENT_MISMATCH", "اثر انگشت محیط عددی قابل تعیین نیست.") from exc
    return deepcopy(_FINGERPRINTS[pid])


def numeric_runtime_fingerprint_hash():
    return digest(numeric_runtime_fingerprint())


def derive_effective_configuration(
    base_configuration, evaluation_quota, policy_definition=POLICY_DEFINITION,
    policy_hash=POLICY_HASH, numeric_runtime_fingerprint=None,
):
    config = (base_configuration if isinstance(base_configuration,SolverConfiguration) else
              SolverConfiguration.model_validate_json(canonical_json(base_configuration)))
    if digest(policy_definition) != policy_hash or policy_definition != POLICY_DEFINITION or policy_hash != POLICY_HASH:
        raise _error("NUMERIC_POLICY_INVALID", "تعریف سیاست عددی با قرارداد ثبت‌شده سازگار نیست.")
    if type(evaluation_quota) is not int or evaluation_quota <= 0:
        raise _error("NUMERIC_POLICY_INVALID", "سهم ارزیابی عدد صحیح مثبت لازم دارد.")
    if numeric_runtime_fingerprint is None:
        raise _error("NUMERIC_POLICY_INVALID", "مشتق‌سازی سیاست به اثر انگشت صریح محیط نیاز دارد.")
    fingerprint = NumericRuntimeFingerprintV1.model_validate(numeric_runtime_fingerprint)
    parameters = config.kwargs()
    if config.implementation_id not in ALLOWLIST:
        return {"application":"passthrough","reason":"not_allowlisted",
                "effective_configuration":config,"effective_config_hash":config.config_id,"derivation":None}
    if config.library_version != "3.0.3" or fingerprint.mealpy_version != "3.0.3" or config.mode != "single":
        raise _error("NUMERIC_POLICY_ENVIRONMENT_MISMATCH", "نسخه یا شیوهٔ اجرای سیاست عددی ناسازگار است.")
    population, epoch = parameters.get("pop_size"), parameters.get("epoch")
    if (type(population) is not int or type(epoch) is not int or not 5 <= population <= 10000 or not 1 <= epoch <= 100000):
        raise _error("NUMERIC_POLICY_INVALID", "جمعیت و تعداد نسل پایه باید اعداد صحیح در محدودهٔ سیاست باشند.")
    if evaluation_quota <= 1 + population:
        raise _error("NUMERIC_POLICY_BUDGET_TOO_SMALL", "بودجهٔ این اجرا برای جمعیت اولیه و شروع یک نسل کافی نیست.")
    budget_epoch = (evaluation_quota - 1 - population + population - 1) // population
    effective_epoch = min(epoch, budget_epoch)
    effective = config.model_copy(update={"parameters":tuple(
        Parameter(name=key,value=effective_epoch if key == "epoch" else value)
        for key,value in sorted(parameters.items())
    )})
    natural = 1 + population + effective_epoch*population
    reason = "budget_epoch" if budget_epoch < epoch else "base_epoch_cap" if budget_epoch > epoch else "epoch_already_matches"
    return {
        "application":"adapted", "reason":reason,"effective_configuration":effective,
        "effective_config_hash":effective.config_id,
        "derivation":{"setup_evaluations":1,"initialization_evaluations":population,"population_size":population,
          "evaluations_per_generation":population,"base_epoch":epoch,"budget_epoch":budget_epoch,"effective_epoch":effective_epoch,
          "natural_evaluations":natural,"expected_started_without_error":min(evaluation_quota,natural),
          "expected_unused_without_error":max(0,evaluation_quota-natural)},
    }


def make_numeric_execution(base_configuration, evaluation_quota, authorization, snapshot=None):
    snapshot = snapshot or {
        "policy_definition":POLICY_DEFINITION,"policy_hash":POLICY_HASH,
        "numeric_runtime_fingerprint":numeric_runtime_fingerprint(),
        "numeric_runtime_fingerprint_sha256":numeric_runtime_fingerprint_hash(),
    }
    fingerprint = snapshot["numeric_runtime_fingerprint"]
    if digest(fingerprint) != snapshot["numeric_runtime_fingerprint_sha256"]:
        raise _error("NUMERIC_POLICY_INVALID", "اثر انگشت تثبیت‌شدهٔ سیاست معتبر نیست.")
    config = (base_configuration if isinstance(base_configuration,SolverConfiguration) else
              SolverConfiguration.model_validate_json(canonical_json(base_configuration)))
    projected = derive_effective_configuration(config,evaluation_quota,snapshot["policy_definition"],snapshot["policy_hash"],fingerprint)
    return NumericExecutionV1(
        schema_version="1.0",policy_id="budget_horizon.v1",policy_hash=snapshot["policy_hash"],
        numeric_runtime_fingerprint_sha256=snapshot["numeric_runtime_fingerprint_sha256"],
        base_config_id=config.config_id,evaluation_quota=evaluation_quota,authorization=authorization,**projected,
    )


@dataclass(frozen=True)
class _NumericQualificationAuthority:
    qualification_id: str
    check_id: str
    execution_id: str
    base_config_id: str
    policy_hash: str
    numeric_runtime_fingerprint_sha256: str
    evaluation_quota: int
    phase: str
    request_hash: str


def validate_numeric_request(request, authority=None):
    from p_mc_optimizer.domain.models import TrialRequestBudgetV1
    trial = request.trial
    if not isinstance(trial,TrialRequestBudgetV1):
        if authority is not None:
            raise _error("NUMERIC_POLICY_AUTHORIZATION_INVALID", "مجوز تأیید سیاست برای این اجرا معتبر نیست.")
        return
    numeric = trial.numeric_execution
    base = SolverConfiguration(
        implementation_id=trial.implementation_id,library_version=trial.library_version,
        adapter_version=trial.adapter_version,mode=trial.mode,boundary_repair=trial.boundary_repair,
        parameters=tuple(Parameter(name=k,value=v) for k,v in sorted(trial.configuration_snapshot.items())),
    )
    if base.config_id != trial.config_id or numeric.base_config_id != trial.config_id or numeric.evaluation_quota != request.evaluation_quota:
        raise _error("NUMERIC_POLICY_INVALID", "پیکربندی یا سهم سیاست عددی با Trial یکسان نیست.")
    if numeric.numeric_runtime_fingerprint_sha256 != numeric_runtime_fingerprint_hash():
        raise _error("NUMERIC_POLICY_ENVIRONMENT_MISMATCH", "محیط فرایند عددی با اثر انگشت تثبیت‌شده متفاوت است.")
    expected = make_numeric_execution(base,request.evaluation_quota,numeric.authorization)
    if expected != numeric:
        raise _error("NUMERIC_POLICY_INVALID", "پارامترهای مؤثر با تبدیل بودجهٔ ثبت‌شده یکسان نیستند.")
    auth = numeric.authorization
    if auth.kind == "qualification_run":
        if type(authority) is not _NumericQualificationAuthority:
            raise _error("NUMERIC_POLICY_AUTHORIZATION_INVALID", "اجرای تأیید سیاست به مجوز داخلی مقید نیاز دارد.")
        bound = (auth.qualification_id,auth.check_id,request.execution_id,base.config_id,numeric.policy_hash,
                 numeric.numeric_runtime_fingerprint_sha256,request.evaluation_quota,request.phase,digest(request.model_dump(mode="json")))
        if bound != tuple(getattr(authority,key) for key in authority.__dataclass_fields__):
            raise _error("NUMERIC_POLICY_AUTHORIZATION_INVALID", "مجوز داخلی به همین آزمون عددی تعلق ندارد.")
        from p_mc_optimizer.catalog.qualification import numeric_check_matrix
        from p_mc_optimizer.catalog.adapter import seed_identity
        from p_mc_optimizer.domain.models import ProblemInput
        from p_mc_optimizer.problems.registry import problem_hashes
        from p_mc_optimizer.selection.policies import derive_seed
        entry = next((row for row in numeric_check_matrix() if row["check_id"] == auth.check_id), None)
        expected_hashes = None if entry is None else problem_hashes(ProblemInput.model_validate(entry["problem_specification"]))
        if (entry is None or (request.revision.problem_math_hash, request.revision.evaluation_contract_hash) != expected_hashes
                or request.phase != entry["phase"] or request.evaluation_quota != entry["evaluation_quota"]
                or trial.replicate_index != entry["replicate_index"]
                or request.seed != derive_seed(1405, request.revision.problem_math_hash,
                    "budget_policy_qualification", seed_identity(trial.implementation_id), 0)):
            raise _error("NUMERIC_POLICY_AUTHORIZATION_INVALID", "درخواست با ماتریس ثابت تأیید سیاست برابر نیست.")
    elif authority is not None:
        raise _error("NUMERIC_POLICY_AUTHORIZATION_INVALID", "مجوز qualification برای Job عادی قابل استفاده نیست.")
    elif auth.kind == "job_certificate":
        certificate = auth.certificate
        if (
            certificate.get("base_config_id") != base.config_id or certificate.get("implementation_id") != base.implementation_id
            or certificate.get("policy_hash") != numeric.policy_hash
            or certificate.get("numeric_runtime_fingerprint_sha256") != numeric.numeric_runtime_fingerprint_sha256
            or digest(certificate.get("numeric_runtime_fingerprint")) != numeric.numeric_runtime_fingerprint_sha256
        ):
            raise _error("NUMERIC_POLICY_AUTHORIZATION_INVALID", "گواهی سیاست به پیکربندی و محیط این اجرا تعلق ندارد.")
    elif base.implementation_id in ALLOWLIST or auth.catalog_hash != trial.catalog_hash:
        raise _error("NUMERIC_POLICY_AUTHORIZATION_INVALID", "تأیید پایه مجوز تغییر این پیکربندی نیست.")


def validate_job_numeric_trial(value, snapshot, catalog):
    """Bind a proposed Trial to the immutable numeric and catalog snapshots of its Job."""
    from types import SimpleNamespace
    from p_mc_optimizer.domain.models import TrialRequestBudgetV1, parse_catalog_trial
    trial = parse_catalog_trial(value)
    if snapshot is None:
        if isinstance(trial,TrialRequestBudgetV1):
            raise _error("NUMERIC_POLICY_AUTHORIZATION_INVALID", "قرارداد عددی تازه در snapshot این Job ثبت نشده است.")
        return
    if not isinstance(trial,TrialRequestBudgetV1):
        raise _error("NUMERIC_POLICY_INVALID", "این Job به قرارداد عددی تثبیت‌شدهٔ تازه نیاز دارد.")
    numeric = trial.numeric_execution
    auth = numeric.authorization
    if auth.kind == "qualification_run":
        raise _error("NUMERIC_POLICY_AUTHORIZATION_INVALID", "مجوز qualification برای رزرو Trial یک Job معتبر نیست.")
    if snapshot["policy_definition"] != POLICY_DEFINITION or snapshot["policy_hash"] != numeric.policy_hash:
        raise _error("NUMERIC_POLICY_INVALID", "سیاست Trial با snapshot این Job یکسان نیست.")
    if snapshot["numeric_runtime_fingerprint_sha256"] != numeric.numeric_runtime_fingerprint_sha256:
        raise _error("NUMERIC_POLICY_ENVIRONMENT_MISMATCH", "اثر انگشت عددی Trial با Job یکسان نیست.")
    cards = catalog["cards"]
    card = next((row for row in cards if row["config_id"] == trial.config_id
                 and row["implementation_id"] == trial.implementation_id), None)
    if card is None:
        raise _error("NUMERIC_POLICY_AUTHORIZATION_INVALID", "پیکربندی در snapshot کاتالوگ همین Job وجود ندارد.")
    config = SolverConfiguration.model_validate_json(canonical_json(card["configuration"]))
    if config.kwargs() != trial.configuration_snapshot or config.config_id != trial.config_id:
        raise _error("NUMERIC_POLICY_INVALID", "پیکربندی پایهٔ Trial با snapshot کاتالوگ یکسان نیست.")
    if auth.kind == "job_certificate":
        expected = snapshot.get("certificates",{}).get(trial.config_id)
        if expected != {"certificate":auth.certificate,"certificate_hash":auth.certificate_hash}:
            raise _error("NUMERIC_POLICY_AUTHORIZATION_INVALID", "گواهی Trial با گواهی فریز‌شدهٔ Job یکسان نیست.")
    else:
        if (auth.catalog_hash != catalog["catalog_hash"] or auth.qualified_report != card["qualified_report"]
                or auth.qualified_report_hash != digest(card["qualified_report"])):
            raise _error("NUMERIC_POLICY_AUTHORIZATION_INVALID", "مجوز پایه با snapshot کاتالوگ همین Job یکسان نیست.")
    validate_numeric_request(SimpleNamespace(trial=trial,evaluation_quota=trial.evaluation_quota,
                                            execution_id=trial.attempt_id,phase=trial.phase))
