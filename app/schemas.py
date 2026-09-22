"""Request/response schemas."""
from __future__ import annotations

import posixpath
import re

from pydantic import BaseModel, Field, field_validator

_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class DocumentIn(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    content: str = Field(max_length=2_000_000)


class ImportRequest(BaseModel):
    documents: list[DocumentIn] = Field(min_length=1, max_length=5000)


class ValidationRequest(BaseModel):
    import_id: str


class ValidationCompareRequest(BaseModel):
    base_validation_id: str = Field(min_length=1, max_length=64)
    target_validation_id: str = Field(min_length=1, max_length=64)


class GateThresholds(BaseModel):
    """Maximum tolerated diagnostics of each severity for one diagnostic type."""

    model_config = {"extra": "forbid"}

    error: int | None = Field(default=None, ge=0)
    warning: int | None = Field(default=None, ge=0)


class IgnoreRuleIn(BaseModel):
    """A single suppression rule; every criterion is optional.

    A rule matches a diagnostic when *all* supplied criteria match. A rule
    without any criterion suppresses every diagnostic.
    """

    model_config = {"extra": "forbid"}

    name: str | None = Field(default=None, max_length=256)
    type: str | None = Field(default=None, max_length=64)
    path_glob: str | None = Field(default=None, max_length=1024)
    message_regex: str | None = Field(default=None, max_length=2048)

    @field_validator("path_glob")
    @classmethod
    def _validate_path_glob(cls, value: str | None) -> str | None:
        if value is None:
            return value
        pattern = value.strip()
        if not pattern:
            raise ValueError("path_glob must not be empty")
        # Fails on unbalanced bracket expressions such as "docs/[a-*.md".
        try:
            re.compile(_translate_path_glob(pattern))
        except re.error as exc:
            raise ValueError(f"invalid path glob {value!r}: {exc}") from None
        return pattern

    @field_validator("message_regex")
    @classmethod
    def _validate_message_regex(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not value:
            raise ValueError("message_regex must not be empty")
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"invalid message_regex {value!r}: {exc}") from None
        return value

    @field_validator("type", "name")
    @classmethod
    def _reject_empty(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must not be empty")
        return value


class GateRequest(BaseModel):
    model_config = {"extra": "forbid"}

    thresholds: dict[str, GateThresholds] = Field(default_factory=dict, max_length=64)
    ignore_rules: list[IgnoreRuleIn] = Field(default_factory=list, max_length=500)


def _translate_path_glob(pattern: str) -> str:
    """Translate a path glob (``*``, ``?``, ``**`` supported) into a regex.

    Imported lazily to keep validation concerns next to the glob engine.
    """
    from .gate import path_glob_to_regex

    return path_glob_to_regex(pattern)


def normalize_doc_path(raw: str) -> str:
    """Normalize and validate a client-supplied document path.

    Raises ValueError on paths that are absolute, empty, or escape the
    document root.
    """
    p = raw.replace("\\", "/").strip()
    if not p:
        raise ValueError("empty path")
    if p.startswith("/") or _DRIVE_RE.match(p):
        raise ValueError(f"absolute path not allowed: {raw!r}")
    if "\x00" in p:
        raise ValueError(f"invalid path: {raw!r}")
    norm = posixpath.normpath(p)
    if norm == "." or norm.startswith(".."):
        raise ValueError(f"path escapes document root: {raw!r}")
    return norm
