"""Shared, user-safe errors for the local application boundary."""
from __future__ import annotations

from typing import Any


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        error_code: str,
        message_fa: str,
        *,
        field_errors: list[dict[str, Any]] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(error_code)
        self.status_code = status_code
        self.error_code = error_code
        self.message_fa = message_fa
        self.field_errors = field_errors or []
        self.retryable = retryable
