"""SARIF 2.1.0 export for validation diagnostics.

Maps stored diagnostics (broken links, broken anchors, anchor collisions
and cycles) onto a single SARIF run. See the OASIS SARIF v2.1.0 spec:
https://docs.oasis-open.org/sarif/sarif/v2.1.0/sarif-v2.1.0.html
"""
from __future__ import annotations

from urllib.parse import quote

SARIF_VERSION = "2.1.0"
SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"

TOOL_NAME = "Markdown Reference Closure Validator"
TOOL_VERSION = "1.0.0"

# Rule order is fixed so ruleIndex values stay stable across exports.
RULES: list[dict] = [
    {
        "id": "broken_link",
        "name": "BrokenLink",
        "shortDescription": {"text": "Link target does not match any imported document"},
        "defaultConfiguration": {"level": "error"},
        "help": {
            "text": "The relative link target cannot be resolved to a document in "
            "the imported set."
        },
    },
    {
        "id": "broken_anchor",
        "name": "BrokenAnchor",
        "shortDescription": {"text": "Link fragment does not match any anchor in the target document"},
        "defaultConfiguration": {"level": "error"},
        "help": {
            "text": "The target document exists, but its '#fragment' does not match "
            "a heading anchor (including GitHub-style auto slugs and {#id} anchors)."
        },
    },
    {
        "id": "anchor_collision",
        "name": "AnchorCollision",
        "shortDescription": {"text": "Multiple headings share the same anchor, or an anchor is redefined"},
        "defaultConfiguration": {"level": "warning"},
        "help": {
            "text": "Headings produce a duplicate anchor (error) or share an anchor "
            "base and rely on auto-disambiguation (warning)."
        },
    },
    {
        "id": "cycle",
        "name": "Cycle",
        "shortDescription": {"text": "Circular reference between documents"},
        "defaultConfiguration": {"level": "warning"},
        "help": {
            "text": "The document participates in a strongly connected component of "
            "the cross-document reference graph (self-references included)."
        },
    },
]
RULE_INDEX = {rule["id"]: i for i, rule in enumerate(RULES)}

_SEVERITY_TO_LEVEL = {"error": "error", "warning": "warning"}


def _artifact_uri(path: str) -> str:
    """Percent-encode a document path into a valid relative SARIF artifact URI."""
    return quote(path, safe="/")


def _result(diag: dict) -> dict:
    """Map one stored diagnostic row to a SARIF result object."""
    result: dict = {
        "ruleId": diag["type"],
        "level": _SEVERITY_TO_LEVEL.get(diag["severity"], "warning"),
        "message": {"text": diag["message"]},
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": _artifact_uri(diag["path"])},
                }
            }
        ],
    }
    if diag["type"] in RULE_INDEX:
        result["ruleIndex"] = RULE_INDEX[diag["type"]]
    # 1-based source line; doc-level diagnostics (cycles) carry no region.
    if diag["line"] is not None:
        result["locations"][0]["physicalLocation"]["region"] = {"startLine": diag["line"]}
    properties: dict = {"severity": diag["severity"]}
    if diag.get("details"):
        properties["details"] = diag["details"]
    result["properties"] = properties
    return result


def _sort_key(result: dict) -> tuple:
    loc = result["locations"][0]["physicalLocation"]
    line = loc.get("region", {}).get("startLine", -1)
    return (
        loc["artifactLocation"]["uri"],
        line,
        result["ruleId"],
        result["message"]["text"],
    )


def build_sarif(validation: dict, diagnostics: list[dict]) -> dict:
    """Build a SARIF 2.1.0 log with one run for a finished validation.

    Results are ordered deterministically by document, line, rule and message;
    the database row id serves only as a final tiebreaker for equal keys.
    """
    results = [
        (_result(diag), diag.get("id", 0))
        for diag in diagnostics
    ]
    results.sort(key=lambda pair: (*_sort_key(pair[0]), pair[1]))

    return {
        "$schema": SARIF_SCHEMA,
        "version": SARIF_VERSION,
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": TOOL_NAME,
                        "version": TOOL_VERSION,
                        "rules": RULES,
                    }
                },
                "invocations": [{"executionSuccessful": True}],
                "automationDetails": {"id": f"validation/{validation['id']}"},
                "results": [result for result, _ in results],
            }
        ],
    }
