"""Quality-gate evaluation for finished validations.

A gate configuration carries:

* ``thresholds``: per diagnostic type, the maximum tolerated count of
  ``error`` / ``warning`` severity diagnostics *after* suppression;
* ``ignore_rules``: suppression criteria (diagnostic ``type``, a ``path``
  glob, a ``message`` regex) where every supplied criterion must match.

Each diagnostic is matched against the rules with a stable priority, and the
single winning rule suppresses it. Conflicts are adjudicated by, in order:

1. more specific path glob (longer literal directory scope, then longer
   literal prefix, then fewer wildcard metacharacters); a path-scoped rule
   always beats an unscoped one;
2. a rule scoped to a diagnostic ``type`` beats a type-agnostic one;
3. earlier submission order.

Counts are kept both for the raw diagnostics and for those surviving
suppression; the gate passes when every surviving (type, severity) bucket is
within its configured threshold.
"""
from __future__ import annotations

import re
from typing import Any

# ------------------------------------------------------------------- globs ---


def path_glob_to_regex(pattern: str) -> str:
    """Translate a POSIX-style path glob into an anchored regex string.

    Supports ``?`` (one non-slash char), ``*`` (any run of non-slash chars),
    ``**`` (any number of path segments, including zero, so ``a/**/b``
    matches both ``a/b`` and ``a/x/y/b``) and ``[...]`` character classes.
    """
    i = 0
    out = ["^"]
    n = len(pattern)
    while i < n:
        c = pattern[i]
        nxt = pattern[i + 1] if i + 1 < n else ""
        if c == "*":
            if nxt == "*":
                # '**/' consumes any leading directory segments including none;
                # a trailing '/**' consumes any segments below its directory
                # (including none); a bare '**' spans everything incl. slashes.
                if i + 2 < n and pattern[i + 2] == "/":
                    out.append("(?:.*/)?")
                    i += 3
                    continue
                if i > 0 and pattern[i - 1] == "/":
                    # keep the emitted slash; 'dir/**' matches 'dir/child' and
                    # 'dir/a/b', but not the bare path 'dir'
                    out.append("(?:.*)?")
                    i += 2
                    continue
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
            i += 1
            continue
        if c == "?":
            out.append("[^/]")
        elif c == "[":
            j = i + 1
            if j < n and pattern[j] in ("!", "^"):
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 1
            if j >= n:
                raise re.error(f"unterminated character class in glob: {pattern!r}")
            body = pattern[i + 1 : j]
            if body[:1] in ("!", "^"):
                body = "^" + body[1:]
            out.append(f"[{body}]")
            i = j + 1
            continue
        else:
            out.append(re.escape(c))
        i += 1
    out.append("$")
    return "".join(out)


def _path_specificity(glob: str) -> tuple:
    """Rank how narrowly a path glob identifies documents.

    Larger tuples win: directory scope (literal slashes before the first
    wildcard), then literal prefix length, then fewer wildcard characters.
    An exact literal path is therefore the most specific possible glob.
    """
    wildcard_at = next(
        (i for i, c in enumerate(glob) if c in "*?["), len(glob)
    )
    literal_prefix = glob[:wildcard_at]
    dir_scope = literal_prefix.count("/")
    wildcards = glob.count("*") + glob.count("?") + glob.count("[")
    return (1, dir_scope, len(literal_prefix), -wildcards)


# ------------------------------------------------------------------- rules ---


class CompiledRule:
    """An ignore rule with its glob/regex compiled and priority precomputed."""

    __slots__ = ("index", "name", "type", "path_re", "message_re", "priority")

    def __init__(self, index: int, rule: Any):
        self.index = index
        self.name = rule.name
        self.type = rule.type
        self.path_re = (
            re.compile(path_glob_to_regex(rule.path_glob))
            if rule.path_glob
            else None
        )
        self.message_re = (
            re.compile(rule.message_regex) if rule.message_regex else None
        )
        path_key = _path_specificity(rule.path_glob) if rule.path_glob else (0, 0, 0, 0)
        # More specific first; type-scoped before type-agnostic; earlier first.
        # Flat tuple so the submission-order tiebreak engages even when path
        # and type criteria compare equal.
        self.priority = (*path_key, 1 if rule.type else 0, -index)

    def matches(self, diag: dict) -> bool:
        if self.type is not None and diag["type"] != self.type:
            return False
        if self.path_re is not None and not self.path_re.match(diag["path"]):
            return False
        if self.message_re is not None and not self.message_re.search(diag["message"]):
            return False
        return True


def _winning_rule(diag: dict, rules: list[CompiledRule]) -> CompiledRule | None:
    """Return the highest-priority rule matching the diagnostic, if any."""
    winner: CompiledRule | None = None
    for rule in rules:
        if rule.matches(diag) and (
            winner is None or rule.priority > winner.priority
        ):
            winner = rule
    return winner


def _public_diagnostic(diag: dict) -> dict:
    return {
        "type": diag["type"],
        "severity": diag["severity"],
        "path": diag["path"],
        "line": diag["line"],
        "message": diag["message"],
        "details": diag["details"],
    }


def _diag_sort_key(diag: dict) -> tuple:
    line = diag["line"] if diag["line"] is not None else -1
    return (diag["path"], line, diag["type"], diag["message"])


def _empty_counts(types: list[str]) -> dict:
    counts: dict = {
        "total": 0,
        "error": 0,
        "warning": 0,
        "by_type": {t: {"total": 0, "error": 0, "warning": 0} for t in types},
    }
    return counts


def _tally(counts: dict, diag: dict) -> None:
    counts["total"] += 1
    if diag["severity"] in ("error", "warning"):
        counts[diag["severity"]] += 1
    bucket = counts["by_type"].setdefault(
        diag["type"], {"total": 0, "error": 0, "warning": 0}
    )
    bucket["total"] += 1
    if diag["severity"] in ("error", "warning"):
        bucket[diag["severity"]] += 1


def evaluate_gate(
    validation_id: str,
    diagnostics: list[dict],
    thresholds: dict[str, Any],
    ignore_rules: list[Any],
) -> dict:
    """Apply suppression rules and thresholds, returning the gate verdict."""
    compiled = [CompiledRule(i, rule) for i, rule in enumerate(ignore_rules)]

    known_types = sorted({d["type"] for d in diagnostics} | set(thresholds))
    raw_counts = _empty_counts(known_types)
    surviving_counts = _empty_counts(known_types)

    # Indexed by submission order so the report lists each rule in request
    # order regardless of its priority.
    hit_by_index: dict[int, dict] = {}
    failing: list[dict] = []

    for diag in diagnostics:
        _tally(raw_counts, diag)
        winner = _winning_rule(diag, compiled)
        if winner is None:
            _tally(surviving_counts, diag)
            failing.append(diag)
        else:
            hit = hit_by_index.get(winner.index)
            if hit is None:
                hit = {
                    "rule_index": winner.index,
                    "name": winner.name,
                    "ignored_count": 0,
                }
                hit_by_index[winner.index] = hit
            hit["ignored_count"] += 1

    violations: list[dict] = []
    exceeded: set[tuple[str, str]] = set()
    passed = True
    for type_ in sorted(thresholds):
        bucket = surviving_counts["by_type"].get(
            type_, {"total": 0, "error": 0, "warning": 0}
        )
        for severity in ("error", "warning"):
            limit = getattr(thresholds[type_], severity)
            if limit is None:
                continue
            actual = bucket[severity]
            violations.append(
                {
                    "type": type_,
                    "severity": severity,
                    "threshold": limit,
                    "actual": actual,
                }
            )
            if actual > limit:
                passed = False
                exceeded.add((type_, severity))

    # Diagnostics that actually block the gate: unsuppressed diagnostics in a
    # (type, severity) bucket whose threshold was exceeded.
    failing = [
        diag
        for diag in failing
        if (diag["type"], diag["severity"]) in exceeded
    ]
    failing.sort(key=_diag_sort_key)

    return {
        "validation_id": validation_id,
        "status": "pass" if passed else "fail",
        "passed": passed,
        "counts": {
            "raw": raw_counts,
            "after_ignore": surviving_counts,
            "ignored": raw_counts["total"] - surviving_counts["total"],
        },
        "violations": violations,
        "matched_rules": [hit_by_index[i] for i in sorted(hit_by_index)],
        "failing_diagnostics": [_public_diagnostic(d) for d in failing],
    }
