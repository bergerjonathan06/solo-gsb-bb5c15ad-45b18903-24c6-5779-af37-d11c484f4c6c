"""Comparison of two finished validations.

Diagnostics are matched by a stable fingerprint built from their six public
fields (type, severity, path, line, message, details). Matching uses multiset
semantics, so repeated identical diagnostics (e.g. one per cycle member) are
counted individually. The imports behind the validations are also compared at
the document level: added/removed paths and paths whose content changed.
"""
from __future__ import annotations

import json
from collections import Counter
from typing import Any

# Public diagnostic fields, in fingerprint order.
FINGERPRINT_FIELDS = ("type", "severity", "path", "line", "message", "details")


def _canonical_details(details: Any) -> str:
    """Stable serialization of a diagnostic's details dict.

    Key order never matters: dicts that compare equal produce the same string.
    """
    return json.dumps(details, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(diag: dict) -> tuple:
    """Stable identity of a diagnostic across validations."""
    return (
        diag["type"],
        diag["severity"],
        diag["path"],
        diag["line"],
        diag["message"],
        _canonical_details(diag.get("details")),
    )


def public_diagnostic(diag: dict) -> dict:
    """Project a stored diagnostic row onto its six public fields."""
    return {field: diag[field] for field in FINGERPRINT_FIELDS}


def _diag_sort_key(diag: dict) -> tuple:
    # Doc-level diagnostics (line is None) sort first, matching SARIF export.
    line = diag["line"] if diag["line"] is not None else -1
    return (diag["path"], line, diag["type"], diag["message"])


def diff_diagnostics(base: list[dict], target: list[dict]) -> dict:
    """Split target/base diagnostics into added, removed and unchanged sets.

    Added   : present in target but not (or less often) in base.
    Removed : present in base but not (or less often) in target.
    Unchanged: present in both, counted at the smaller multiplicity.
    """
    base_fps = [fingerprint(d) for d in base]
    target_fps = [fingerprint(d) for d in target]
    base_counts = Counter(base_fps)
    target_counts = Counter(target_fps)

    # Keep one representative public diagnostic per fingerprint.
    representative: dict[tuple, dict] = {}
    for diag in (*base, *target):
        representative.setdefault(fingerprint(diag), public_diagnostic(diag))

    added: list[dict] = []
    removed: list[dict] = []
    unchanged: list[dict] = []
    for fp, count in target_counts.items():
        added.extend([representative[fp]] * max(0, count - base_counts.get(fp, 0)))
        unchanged.extend([representative[fp]] * min(count, base_counts.get(fp, 0)))
    for fp, count in base_counts.items():
        removed.extend([representative[fp]] * max(0, count - target_counts.get(fp, 0)))

    for bucket in (added, removed, unchanged):
        bucket.sort(key=_diag_sort_key)

    return {
        "added": added,
        "removed": removed,
        "unchanged": unchanged,
        "counts": {
            "added": len(added),
            "removed": len(removed),
            "unchanged": len(unchanged),
        },
    }


def diff_documents(base_docs: dict[str, str], target_docs: dict[str, str]) -> dict:
    """Compare two imports' document sets at the path/content level."""
    base_paths = set(base_docs)
    target_paths = set(target_docs)
    shared = base_paths & target_paths
    return {
        "added": sorted(target_paths - base_paths),
        "removed": sorted(base_paths - target_paths),
        "changed": sorted(p for p in shared if base_docs[p] != target_docs[p]),
    }


def build_comparison(
    base_validation: dict,
    target_validation: dict,
    base_diagnostics: list[dict],
    target_diagnostics: list[dict],
    base_documents: dict[str, str],
    target_documents: dict[str, str],
) -> dict:
    """Assemble the full compare response from loaded validation data."""
    return {
        "base_validation_id": base_validation["id"],
        "target_validation_id": target_validation["id"],
        "base_import_id": base_validation["import_id"],
        "target_import_id": target_validation["import_id"],
        "diagnostics": diff_diagnostics(base_diagnostics, target_diagnostics),
        "documents": diff_documents(base_documents, target_documents),
    }
