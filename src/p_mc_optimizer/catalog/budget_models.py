"""Closed contracts for the frozen budget horizon and its execution permissions."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from p_mc_optimizer.catalog.configuration import SolverConfiguration, digest
from p_mc_optimizer.domain.base import ContractModel

Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Nonnegative = Annotated[int, Field(ge=0)]
Positive = Annotated[int, Field(gt=0)]


class NumericRuntimeFingerprintV1(ContractModel):
    schema_version: Literal["1.0"]
    environment_id: str
    python_major_minor: str
    platform_system: str
    architecture: str
    numpy_version: str
    mealpy_version: str
    dtype: Literal["float64"]
    rng: Literal["PCG64"]
    mode: Literal["single"]
    mealpy_source_manifest_sha256: Hash
    numeric_source_manifest_sha256: Hash


class BudgetDerivationV1(ContractModel):
    setup_evaluations: Positive
    initialization_evaluations: Positive
    population_size: Positive
    evaluations_per_generation: Positive
    base_epoch: Positive
    budget_epoch: Positive
    effective_epoch: Positive
    natural_evaluations: Positive
    expected_started_without_error: Positive
    expected_unused_without_error: Nonnegative


class BaseCatalogAuthorization(ContractModel):
    kind: Literal["base_catalog"]
    base_config_id: Hash
    catalog_hash: Hash
    qualified_report: dict
    qualified_report_hash: Hash

    @model_validator(mode="after")
    def report_matches(self):
        if digest(self.qualified_report) != self.qualified_report_hash:
            raise ValueError("The base qualification report hash does not match")
        if self.qualified_report.get("config_id") != self.base_config_id or self.qualified_report.get("status") != "qualified":
            raise ValueError("The base qualification does not authorize this configuration")
        return self


class JobCertificateAuthorization(ContractModel):
    kind: Literal["job_certificate"]
    qualification_id: str
    certificate_hash: Hash
    certificate: dict

    @model_validator(mode="after")
    def certificate_matches(self):
        if digest(self.certificate) != self.certificate_hash or self.certificate.get("qualification_id") != self.qualification_id:
            raise ValueError("The policy qualification certificate hash does not match")
        if self.certificate.get("status") != "qualified" or self.certificate.get("pilot_500_compatible") is not True:
            raise ValueError("The policy qualification certificate is not qualified")
        return self


class QualificationRunAuthorization(ContractModel):
    kind: Literal["qualification_run"]
    qualification_id: str
    check_id: str
    certificate_hash: None


NumericAuthorization = Annotated[
    BaseCatalogAuthorization | JobCertificateAuthorization | QualificationRunAuthorization,
    Field(discriminator="kind"),
]


class NumericExecutionV1(ContractModel):
    schema_version: Literal["1.0"]
    policy_id: Literal["budget_horizon.v1"]
    policy_hash: Hash
    numeric_runtime_fingerprint_sha256: Hash
    base_config_id: Hash
    application: Literal["adapted", "passthrough"]
    reason: Literal["budget_epoch", "base_epoch_cap", "epoch_already_matches", "not_allowlisted"]
    evaluation_quota: Positive
    effective_configuration: SolverConfiguration
    effective_config_hash: Hash
    derivation: BudgetDerivationV1 | None
    authorization: NumericAuthorization

    @model_validator(mode="after")
    def consistent_envelope(self):
        if self.effective_configuration.config_id != self.effective_config_hash:
            raise ValueError("The effective configuration hash does not match")
        if self.authorization.kind == "base_catalog":
            if (self.application != "passthrough" or self.reason != "not_allowlisted" or self.derivation is not None
                    or self.authorization.base_config_id != self.base_config_id):
                raise ValueError("Base qualification only permits an unchanged passthrough")
        elif self.application != "adapted" or self.reason == "not_allowlisted" or self.derivation is None:
            raise ValueError("Policy authorization requires an adapted configuration")
        return self
