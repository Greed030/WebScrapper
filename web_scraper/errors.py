"""Исключение с типом ошибки; на границе фасада превращается в ErrorInfo."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .models import ErrorInfo, ErrorKind

_RETRYABLE_BY_DEFAULT: set[str] = {"timeout", "network"}


class ScraperError(Exception):
    def __init__(
        self,
        kind: ErrorKind,
        message: str,
        *,
        url: str | None = None,
        status_code: int | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.kind: ErrorKind = kind
        self.message = message
        self.url = url
        self.status_code = status_code
        self.retryable = kind in _RETRYABLE_BY_DEFAULT if retryable is None else retryable

    def to_info(self) -> ErrorInfo:
        return ErrorInfo(
            kind=self.kind,
            message=self.message,
            url=self.url,
            status_code=self.status_code,
            retryable=self.retryable,
        )

    def __str__(self) -> str:
        return f"{self.kind}: {self.message}"


def describe_validation_errors(errors: Sequence[Mapping[str, Any]], skip_prefix: str | None = None) -> str:
    """Ошибки Pydantic/FastAPI одной строкой: «url: Value error, expected ...; max_pages: ...»."""
    parts = []
    for item in list(errors)[:5]:
        loc = [str(p) for p in item.get("loc", ())]
        if skip_prefix and loc and loc[0] == skip_prefix:
            loc = loc[1:]
        parts.append(f"{'.'.join(loc) or '<input>'}: {item.get('msg', 'invalid value')}")
    return "; ".join(parts) or "invalid input"
