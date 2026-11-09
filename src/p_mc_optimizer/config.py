"""Non-secret, loopback-only local settings."""

from __future__ import annotations

import json
from pathlib import Path

from platformdirs import user_data_dir
from pydantic import BaseModel, ConfigDict, Field


class LocalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    api_port: int = Field(default=8765, ge=1024, le=65535)
    ui_port: int = Field(default=8501, ge=1024, le=65535)

    @property
    def api_url(self) -> str:
        return f"http://127.0.0.1:{self.api_port}"

    @property
    def ui_url(self) -> str:
        return f"http://127.0.0.1:{self.ui_port}"


def data_directory(override: Path | None) -> Path:
    return (override or Path(user_data_dir("P_MC_Optimizer", appauthor=False))).expanduser().resolve()


def load_config(path: Path | None) -> LocalConfig:
    if path is None:
        return LocalConfig()
    from p_mc_optimizer.domain.models import strict_json_loads
    config = LocalConfig.model_validate(strict_json_loads(path.read_bytes()))
    if config.api_port == config.ui_port:
        raise ValueError("پورت رابط و API باید متفاوت باشند.")
    return config


def config_json(config: LocalConfig) -> str:
    return json.dumps(config.model_dump(), sort_keys=True)
