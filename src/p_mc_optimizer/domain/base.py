"""Closed immutable contracts without dependencies on the numerical engine."""
from pydantic import BaseModel, ConfigDict


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)
