"""Immutable solver configurations, catalog snapshots, and qualification evidence."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import Field, model_validator

from p_mc_optimizer.domain.models import (
    ConstructorValue,
    ContractModel,
    DomainError,
    ImplementationId,
    Sha256Hex,
)

ADAPTER_VERSION = "2.0"
CatalogStatus = Literal["discovered", "needs_configuration", "qualified", "rejected", "disabled"]


from p_mc_optimizer.catalog.configuration import Parameter, SolverConfiguration, canonical_json, digest


class ImplementationRecord(ContractModel):
    schema_version: Literal["2.0"] = "2.0"
    implementation_id: ImplementationId
    package: Literal["mealpy"] = "mealpy"
    library_version: str
    module: str
    class_name: str
    family_id: str
    seed_identity: str
    suggested: bool
    configuration: SolverConfiguration | None
    reason: str | None


class QualificationCheck(ContractModel):
    check: str
    status: Literal["passed", "failed", "cancelled"]
    started_evaluations: Annotated[int, Field(ge=0)]
    charged_unknown: Annotated[int, Field(ge=0)]
    history_sha256: Sha256Hex | None
    outcome: str
    elapsed_seconds: Annotated[float, Field(ge=0)]


class QualificationReport(ContractModel):
    schema_version: Literal["2.0"] = "2.0"
    config_id: Sha256Hex
    status: Literal["qualified", "rejected", "cancelled"]
    pilot_500_compatible: bool
    checks: tuple[QualificationCheck, ...]
    environment: str


class SolverCardV2(ContractModel):
    schema_version: Literal["2.0"] = "2.0"
    implementation_id: ImplementationId
    config_id: Sha256Hex
    family_id: str
    library_version: str
    adapter_version: str
    seed_identity: str
    configuration: SolverConfiguration
    qualified_report: QualificationReport
    min_dimension: Literal[2] = 2
    max_dimension: Literal[30] = 30
    deterministic_continuous_single_objective: Literal[True] = True
    pilot_500_compatible: bool

    @model_validator(mode="after")
    def matching_configuration(self):
        if (
            self.implementation_id != self.configuration.implementation_id
            or self.config_id != self.configuration.config_id
            or self.library_version != self.configuration.library_version
            or self.adapter_version != self.configuration.adapter_version
            or self.config_id != self.qualified_report.config_id
            or self.qualified_report.status != "qualified"
            or self.pilot_500_compatible != self.qualified_report.pilot_500_compatible
        ):
            raise ValueError("Configuration does not match the qualification report")
        return self


class CatalogSnapshot(ContractModel):
    schema_version: Literal["2.0"] = "2.0"
    catalog_hash: Sha256Hex
    cards: tuple[SolverCardV2, ...]

    @model_validator(mode="after")
    def matching_hash(self):
        keys = [(card.implementation_id, card.config_id) for card in self.cards]
        if keys != sorted(set(keys)) or self.catalog_hash != cards_hash(self.cards):
            raise ValueError("Catalog snapshot does not match its hash")
        return self

    def get(self, implementation_id: str, config_id: str) -> SolverCardV2:
        for card in self.cards:
            if (card.implementation_id, card.config_id) == (implementation_id, config_id):
                return card
        raise DomainError("CATALOG_QUALIFICATION_REQUIRED", "پیکربندی در کاتالوگ تأییدشدهٔ این اجرا وجود ندارد.")


def cards_hash(cards: tuple[SolverCardV2, ...]) -> str:
    return digest([card.model_dump(mode="json") for card in cards])
