"""Request/response schemas."""
from __future__ import annotations

import posixpath
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

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


class SeverityThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Limits are validated semantically (non-negative, int-not-bool) in
    # gate.compile_gate_request so all errors share the 422 list format.
    error: Any | None = Field(default=None)
    warning: Any | None = None


class IgnoreRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str | None = Field(default=None, min_length=1, max_length=64)
    path_glob: str | None = Field(default=None, min_length=1, max_length=1024)
    message_regex: str | None = Field(default=None, min_length=1, max_length=2048)


class GateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thresholds: dict[str, SeverityThresholds] | None = Field(default=None, max_length=32)
    ignore_rules: list[IgnoreRule] | None = Field(default=None, max_length=200)


def gate_payload(model: GateRequest) -> dict:
    """Project the parsed gate request onto the dict the gate engine validates."""
    return model.model_dump(exclude_none=True)


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
