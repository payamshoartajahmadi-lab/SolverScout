"""Canonical configuration identity shared by legacy and budget-aware trials."""
from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import Field, model_validator

from p_mc_optimizer.domain.base import ContractModel

ImplementationId = Annotated[str, Field(max_length=240, pattern=r"^[a-z][a-z0-9_-]*:[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$")]
ConstructorValue = int | float | bool | str | None

def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class Parameter(ContractModel):
    name: str
    value: ConstructorValue


class SolverConfiguration(ContractModel):
    schema_version: Literal["2.0"] = "2.0"
    implementation_id: ImplementationId
    library_version: str
    adapter_version: str
    parameters: tuple[Parameter, ...]
    boundary_repair: str
    mode: Literal["single"] = "single"

    @model_validator(mode="after")
    def canonical_parameters(self):
        names = [parameter.name for parameter in self.parameters]
        if names != sorted(set(names)):
            raise ValueError("Constructor parameters must be sorted and unique")
        return self

    @property
    def config_id(self) -> str:
        return digest(self.model_dump(mode="json"))

    def kwargs(self) -> dict[str, ConstructorValue]:
        return {parameter.name: parameter.value for parameter in self.parameters}


