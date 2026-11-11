"""Discover only installed MEALPY classes and resolve safe constructor arguments."""

from __future__ import annotations

import inspect
import math
import platform
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version

import numpy as np

from p_mc_optimizer.catalog.models import ADAPTER_VERSION, ImplementationRecord, Parameter, SolverConfiguration
from p_mc_optimizer.domain.models import ConstructorValue, DomainError, TrialRequestV2
from p_mc_optimizer.execution.evaluator import BudgetedEvaluator
from p_mc_optimizer.solvers.registry import run_mealpy, solver_cards


def _canonical(class_path: str) -> str:
    module, class_name = class_path.removeprefix("mealpy.").rsplit(".", 1)
    return f"mealpy:{module}:{class_name}"


_LEGACY = {_canonical(card["class_path"]): card for card in solver_cards()}
_SUGGESTED = set(_LEGACY) | {
    "mealpy:swarm_based.GWO:OriginalGWO",
    "mealpy:swarm_based.WOA:OriginalWOA",
}


@lru_cache(maxsize=1)
def _installed_classes(library_version: str) -> dict[str, type]:
    from mealpy import Optimizer, get_all_optimizers

    # The public helper keys by bare class name. Include its module scan to
    # retain distinct canonical IDs if a future package ships a name collision.
    import mealpy

    classes = set(get_all_optimizers(verbose=False).values())
    for _, module in inspect.getmembers(mealpy, inspect.ismodule):
        if module.__name__.startswith("mealpy."):
            classes.update(
                cls for _, cls in inspect.getmembers(module, inspect.isclass)
                if cls.__module__ == module.__name__ and cls is not Optimizer and issubclass(cls, Optimizer)
            )
    return dict(sorted(
        (_canonical(f"{cls.__module__}.{cls.__name__}"), cls)
        for cls in classes if cls.__module__.startswith("mealpy.") and not inspect.isabstract(cls)
    ))


def environment_id() -> str:
    python = ".".join(platform.python_version_tuple()[:2])
    return f"{ADAPTER_VERSION}:{platform.system()}:{platform.machine()}:{python}:numpy{np.__version__}"


def current_library_version() -> str:
    try:
        return version("mealpy")
    except PackageNotFoundError:
        raise DomainError("SOLVER_CONFIGURATION_MISMATCH", "نسخهٔ لازم کتابخانهٔ MEALPY نصب نشده است.") from None


def seed_identity(implementation_id: str) -> str:
    return _LEGACY.get(implementation_id, {}).get("algorithm_id", implementation_id)


def _repair_rule(cls: type) -> str:
    implementation_id = _canonical(f"{cls.__module__}.{cls.__name__}")
    if implementation_id in _LEGACY:
        return _LEGACY[implementation_id]["boundary_repair"]
    amend = cls.amend_solution
    return f"{amend.__module__}.{amend.__qualname__} then MEALPY FloatVar clipping"


def _scalar(value: object) -> bool:
    return type(value) in (str, bool, int, type(None)) or (type(value) is float and math.isfinite(value))


def _constructor_parameters(cls: type, overrides: dict[str, ConstructorValue]) -> tuple[Parameter, ...] | None:
    try:
        signature = inspect.signature(cls)
    except (ValueError, TypeError):
        return None
    result = []
    for name, field in signature.parameters.items():
        # The adapter never passes unbounded keyword overrides. The concrete
        # signature is authoritative; opaque positional constructors need review.
        if field.kind == inspect.Parameter.VAR_KEYWORD:
            continue
        if field.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.VAR_POSITIONAL):
            return None
        value = overrides.get(name, field.default)
        if not _scalar(value):
            return None
        result.append(Parameter(name=name, value=value))
    names = {parameter.name for parameter in result}
    # A subclass can forward opaque **kwargs to a more specific parent.
    # Leave it unresolved if its public signature hides parent parameters.
    for base in cls.__mro__[1:-1]:
        try:
            parent_signature = inspect.signature(base)
        except (ValueError, TypeError):
            return None
        parent_names = {
            parameter.name for parameter in parent_signature.parameters.values()
            if parameter.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        }
        if not parent_names <= names:
            return None
    if not {"epoch", "pop_size"} <= names or not overrides.keys() <= names:
        return None
    return tuple(sorted(result, key=lambda parameter: parameter.name))


def _resolve(cls: type, library_version: str, overrides: dict[str, ConstructorValue]) -> SolverConfiguration | None:
    parameters = _constructor_parameters(cls, overrides)
    if parameters is None:
        return None
    return SolverConfiguration(
        implementation_id=_canonical(f"{cls.__module__}.{cls.__name__}"),
        library_version=library_version,
        adapter_version=ADAPTER_VERSION,
        parameters=parameters,
        boundary_repair=_repair_rule(cls),
    )


def resolve_configuration(
    implementation_id: str, overrides: dict[str, ConstructorValue] | None = None,
) -> SolverConfiguration | None:
    library_version = current_library_version()
    cls = _installed_classes(library_version).get(implementation_id)
    if cls is None:
        raise DomainError("IMPLEMENTATION_MAPPING_INVALID", "روش در کتابخانهٔ نصب‌شده پیدا نشد.")
    defaults = _LEGACY.get(implementation_id, {}).get("configuration_snapshot", {})
    return _resolve(cls, library_version, defaults | (overrides or {}))


def discover() -> tuple[ImplementationRecord, ...]:
    library_version = current_library_version()
    records = []
    for implementation_id, cls in _installed_classes(library_version).items():
        defaults = _LEGACY.get(implementation_id, {}).get("configuration_snapshot", {})
        config = _resolve(cls, library_version, defaults)
        records.append(ImplementationRecord(
            implementation_id=implementation_id,
            library_version=library_version,
            module=cls.__module__,
            class_name=cls.__name__,
            family_id=cls.__module__.rsplit(".", 1)[-1].casefold(),
            seed_identity=seed_identity(implementation_id),
            suggested=implementation_id in _SUGGESTED,
            configuration=config,
            reason=None if config else "CONSTRUCTOR_CONFIGURATION_REQUIRED",
        ))
    return tuple(records)


def run_catalog_solver(evaluator: BudgetedEvaluator, trial: TrialRequestV2) -> None:
    library_version = current_library_version()
    cls = _installed_classes(library_version).get(trial.implementation_id)
    config = None if cls is None else _resolve(cls, library_version, trial.configuration_snapshot)
    if (
        config is None
        or trial.library_version != library_version
        or trial.adapter_version != ADAPTER_VERSION
        or trial.config_hash != config.config_id
        or trial.seed_identity != seed_identity(trial.implementation_id)
        or trial.boundary_repair != config.boundary_repair
        or trial.mode != config.mode
    ):
        raise DomainError("SOLVER_CONFIGURATION_MISMATCH", "نسخه یا پیکربندی روش با snapshot تأییدشده تطابق ندارد.")
    from p_mc_optimizer.domain.models import TrialRequestBudgetV1
    parameters = (trial.numeric_execution.effective_configuration.kwargs()
                  if isinstance(trial, TrialRequestBudgetV1) else config.kwargs())
    run_mealpy(evaluator, cls, parameters, seed=trial.seed)
