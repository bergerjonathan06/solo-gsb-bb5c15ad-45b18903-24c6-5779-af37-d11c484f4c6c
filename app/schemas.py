"""Request/response schemas."""
from __future__ import annotations

import posixpath
import re

from pydantic import BaseModel, Field

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
