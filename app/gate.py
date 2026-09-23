"""Quality-gate evaluation for finished validations.

A gate request carries, per diagnostic type, maximum allowed ``error`` and
``warning`` counts (``thresholds``) plus a list of *ignore rules*. Each rule
may match on diagnostic type, document path (a POSIX glob) and message
(regex). Every diagnostic is matched against the rules in a stable priority
order:

1. the rule with the more specific path glob wins
   (more literal path segments, then more literal characters, then fewer
   wildcards in the pattern);
2. otherwise a rule constrained to one diagnostic type beats a rule without
   a type;
3. otherwise the rule submitted earlier (lower list index) wins.

Only the single highest-priority rule that matches a diagnostic is applied,
so the first matching rule in priority order is stable even when rules
overlap. Thresholds are checked against the post-ignore counts; a
diagnostic whose bucket (type + severity) has no configured threshold never
fails the gate.
"""
from __future__ import annotations

import re
from typing import Any

from .analyzer import DIAGNOSTIC_TYPES

PUBLIC_FIELDS = ("type", "severity", "path", "line", "message", "details")
SEVERITIES = ("error", "warning")


# ------------------------------------------------------------- validation ---

class GateRequestError(ValueError):
    """All request-shape problems that must surface as HTTP 422."""


def _compile_glob(pattern: str) -> re.Pattern[str]:
    """Translate a shell-style path glob into an anchored regex.

    Supports ``*`` (any non-segment sequence), ``?`` (one non-segment
    character), ``**`` (any number of path segments) and ``[...]``
    character classes; characters that are significant in regular
    expressions are escaped.
    """
    out: list[str] = ["^"]
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                # '**' spans any number of path segments; consume an
                # adjacent '/' so 'a/**/b' also matches 'a/b'.
                i += 2
                if i < n and pattern[i] == "/":
                    i += 1
                    out.append("(?:.*/)?")
                else:
                    out.append(".*")
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = i + 1
            if j < n and pattern[j] in ("!", "^"):
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 1
            if j >= n:  # unterminated class: treat '[' literally
                out.append(re.escape("["))
                i += 1
            else:
                body = pattern[i + 1 : j]
                if body[:1] in ("!", "^"):
                    body = "^" + body[1:]
                out.append(f"[{body}]")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    out.append("$")
    try:
        return re.compile("".join(out))
    except re.error:
        raise GateRequestError(f"invalid path glob: {pattern!r}")


def _glob_specificity(pattern: str) -> tuple:
    """Higher tuple = more specific path glob.

    Segments without wildcards rank above segments containing them; within
    that ordering, more literal characters and fewer wildcard metacharacters
    win. Patterns that do not name a concrete path at all (empty) rank
    lowest.
    """
    segments = pattern.split("/") if pattern else []
    literal_segments = sum(1 for s in segments if s and not any(ch in s for ch in "*?["))
    literal_chars = sum(1 for ch in pattern if ch not in "*?[]")
    wildcards = sum(1 for ch in pattern if ch in "*?[")
    return (len(segments), literal_segments, literal_chars, -wildcards)


def _validate_scalar_int(value: Any, where: str) -> int:
    # bool is an int subclass in Python; silently accepting true/false as a
    # threshold would be surprising, so reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, int):
        raise GateRequestError(f"{where} must be a non-negative integer")
    if value < 0:
        raise GateRequestError(f"{where} must be a non-negative integer")
    return value


def compile_gate_request(payload: dict) -> tuple[dict[str, dict[str, int]], list[dict]]:
    """Validate a decoded gate request and return (thresholds, rules).

    Each compiled rule is a dict with the raw criteria plus a compiled
    path regex (``path_re``) and message regex (``message_re``), and the
    request ordinal (``index``). Raises GateRequestError (-> 422) on any
    unknown diagnostic type, negative/non-integer threshold or invalid
    regex/glob.
    """
    if not isinstance(payload, dict):
        raise GateRequestError("request body must be a JSON object")

    thresholds_in = payload.get("thresholds", {})
    if thresholds_in is None:
        thresholds_in = {}
    if not isinstance(thresholds_in, dict):
        raise GateRequestError("thresholds must be an object keyed by diagnostic type")
    thresholds: dict[str, dict[str, int]] = {}
    for type_, limit in thresholds_in.items():
        if type_ not in DIAGNOSTIC_TYPES:
            raise GateRequestError(f"unknown diagnostic type: {type_!r}")
        if not isinstance(limit, dict):
            raise GateRequestError(
                f"threshold for {type_!r} must be an object with 'error'/'warning' limits"
            )
        entry: dict[str, int] = {}
        for severity in SEVERITIES:
            if severity in limit and limit[severity] is not None:
                entry[severity] = _validate_scalar_int(
                    limit[severity], f"thresholds.{type_}.{severity}"
                )
        thresholds[type_] = entry

    rules_in = payload.get("ignore_rules", [])
    if rules_in is None:
        rules_in = []
    if not isinstance(rules_in, list):
        raise GateRequestError("ignore_rules must be a list")

    rules: list[dict] = []
    for idx, raw in enumerate(rules_in):
        if not isinstance(raw, dict):
            raise GateRequestError(f"ignore_rules[{idx}] must be an object")
        type_ = raw.get("type")
        path_glob = raw.get("path_glob")
        message_regex = raw.get("message_regex")
        if type_ is None and path_glob is None and message_regex is None:
            raise GateRequestError(
                f"ignore_rules[{idx}] must set at least one of type, path_glob, message_regex"
            )
        if type_ is not None:
            if not isinstance(type_, str) or type_ not in DIAGNOSTIC_TYPES:
                raise GateRequestError(
                    f"ignore_rules[{idx}].type must be one of {sorted(DIAGNOSTIC_TYPES)}"
                )
        if path_glob is not None and (not isinstance(path_glob, str) or not path_glob):
            raise GateRequestError(f"ignore_rules[{idx}].path_glob must be a non-empty string")
        if message_regex is not None and (
            not isinstance(message_regex, str) or not message_regex
        ):
            raise GateRequestError(
                f"ignore_rules[{idx}].message_regex must be a non-empty string"
            )
        path_re = _compile_glob(path_glob) if path_glob else None
        try:
            message_re = re.compile(message_regex) if message_regex else None
        except re.error as exc:
            raise GateRequestError(
                f"ignore_rules[{idx}].message_regex is not a valid regex: {exc.msg}"
            )
        rules.append(
            {
                "index": idx,
                "type": type_,
                "path_glob": path_glob,
                "message_regex": message_regex,
                "path_re": path_re,
                "message_re": message_re,
                "path_specificity": _glob_specificity(path_glob or ""),
            }
        )
    return thresholds, rules


# ------------------------------------------------------------- evaluation ---

def _matches(rule: dict, diag: dict) -> bool:
    if rule["type"] is not None and rule["type"] != diag["type"]:
        return False
    if rule["path_re"] is not None and not rule["path_re"].match(diag["path"]):
        return False
    if rule["message_re"] is not None and not rule["message_re"].search(diag["message"]):
        return False
    return True


def _rule_priority(rule: dict) -> tuple:
    # Empty path glob -> specificity (0, 0, 0, 0); a present glob always
    # ranks above it via its segment count. Type specificity (exact type
    # constraint present or not) is the second axis, request order last.
    return (rule["path_specificity"], 1 if rule["type"] is not None else 0, -rule["index"])


def _public_diagnostic(diag: dict) -> dict:
    return {field: diag[field] for field in PUBLIC_FIELDS}


def _diag_sort_key(diag: dict) -> tuple:
    line = diag["line"] if diag["line"] is not None else -1
    return (diag["path"], line, diag["type"], diag["message"])


def _empty_type_counts() -> dict[str, int]:
    return {type_: 0 for type_ in sorted(DIAGNOSTIC_TYPES)}


def evaluate_gate(
    validation_id: str,
    diagnostics: list[dict],
    thresholds: dict[str, dict[str, int]],
    rules: list[dict],
) -> dict:
    """Apply ignore rules and thresholds; return the full gate response."""
    raw = {sev: _empty_type_counts() for sev in SEVERITIES}
    for diag in diagnostics:
        bucket = raw[diag["severity"]]
        bucket[diag["type"]] = bucket.get(diag["type"], 0) + 1

    matched_by_index: dict[int, dict] = {}
    ignored_diags: list[dict] = []
    remaining: list[dict] = []
    ignored = {sev: _empty_type_counts() for sev in SEVERITIES}

    for diag in diagnostics:
        best: dict | None = None
        for rule in rules:
            if _matches(rule, diag):
                if best is None or _rule_priority(rule) > _rule_priority(best):
                    best = rule
        if best is None:
            remaining.append(diag)
            continue
        bucket = ignored[diag["severity"]]
        bucket[diag["type"]] = bucket.get(diag["type"], 0) + 1
        ignored_out = _public_diagnostic(diag)
        ignored_out["ignored_by"] = best["index"]
        ignored_diags.append(ignored_out)
        hit = matched_by_index.get(best["index"])
        if hit is None:
            hit = {
                "rule_index": best["index"],
                "type": best["type"],
                "path_glob": best["path_glob"],
                "message_regex": best["message_regex"],
                "matched_count": 0,
            }
            matched_by_index[best["index"]] = hit
        hit["matched_count"] += 1

    matched = [matched_by_index[i] for i in sorted(matched_by_index)]
    ignored_diags.sort(key=lambda d: (*_diag_sort_key(d), d["ignored_by"]))
    remaining.sort(key=_diag_sort_key)

    def counts_block(table: dict[str, dict[str, int]]) -> dict:
        return {
            sev: {type_: table[sev].get(type_, 0) for type_ in sorted(DIAGNOSTIC_TYPES)}
            for sev in SEVERITIES
        }

    raw_counts = counts_block(raw)
    ignored_counts = counts_block(ignored)
    effective = {
        sev: {
            type_: raw_counts[sev][type_] - ignored_counts[sev][type_]
            for type_ in sorted(DIAGNOSTIC_TYPES)
        }
        for sev in SEVERITIES
    }

    threshold_checks: list[dict] = []
    failing: list[dict] = []
    passed = True
    for type_ in sorted(thresholds):
        for sev in SEVERITIES:
            if sev not in thresholds[type_]:
                continue
            limit = thresholds[type_][sev]
            count = effective[sev][type_]
            ok = count <= limit
            threshold_checks.append(
                {
                    "type": type_,
                    "severity": sev,
                    "threshold": limit,
                    "count": count,
                    "pass": ok,
                }
            )
            if not ok:
                passed = False
                failing.append(
                    {
                        "type": type_,
                        "severity": sev,
                        "threshold": limit,
                        "count": count,
                        "diagnostics": [
                            _public_diagnostic(d)
                            for d in remaining
                            if d["type"] == type_ and d["severity"] == sev
                        ],
                    }
                )

    return {
        "validation_id": validation_id,
        "status": "pass" if passed else "fail",
        "passed": passed,
        "raw_counts": raw_counts,
        "ignored_counts": ignored_counts,
        "counts": {
            "raw": sum(sum(sev.values()) for sev in raw_counts.values()),
            "ignored": sum(sum(sev.values()) for sev in ignored_counts.values()),
            "effective": len(remaining),
        },
        "matched_rules": matched,
        "ignored_diagnostics": ignored_diags,
        "threshold_checks": threshold_checks,
        "failing": failing,
    }
