"""End-to-end and unit tests for the reference-closure validator."""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.analyzer import analyze
from app.main import app
from app.parser import parse_markdown

CORPUS = {
    "index.md": (
        "# Home\n"
        "\n"
        "See the [guide](docs/guide.md#install) and [missing](docs/nope.md).\n"
        "\n"
        "```\n"
        "[fake](ghost.md)\n"
        "# not a heading\n"
        "```\n"
        "\n"
        "Ref [api](docs/api.md).\n"
    ),
    "docs/guide.md": (
        "# Guide\n"
        "\n"
        "## Install\n"
        "\n"
        "Back to [home](../index.md#home).\n"
        "\n"
        "## Install\n"
        "\n"
        "See [old](../deep.md).\n"
        "\n"
        "Jump to [top](#guide).\n"
    ),
    "docs/api.md": (
        "# API\n"
        "\n"
        "Related: [setup](guide.md#instal) and [home](../index.md#home).\n"
        "\n"
        "[ref]: guide.md\n"
        "Follow [the guide][ref].\n"
    ),
    "docs/old/deep.md": (
        "# Deep\n"
        "\n"
        "Up: [guide](../guide.md).\n"
        "\n"
        "Empty [e](#).\n"
    ),
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def import_and_validate(client, corpus):
    r = client.post(
        "/api/imports",
        json={"documents": [{"path": p, "content": c} for p, c in corpus.items()]},
    )
    assert r.status_code == 201, r.text
    import_id = r.json()["import_id"]
    r = client.post("/api/validations", json={"import_id": import_id})
    assert r.status_code == 202, r.text
    vid = r.json()["validation_id"]
    r = client.get(f"/api/validations/{vid}")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done", body
    return import_id, vid, body


# ------------------------------------------------------------------ parser ---

def test_parser_ignores_fenced_and_indented_code():
    doc = parse_markdown(
        "a.md",
        "# Real\n"
        "\n"
        "```\n"
        "# fake heading\n"
        "[x](ghost.md)\n"
        "```\n"
        "\n"
        "    [indented](nope.md)\n"
        "\n"
        "~~~text\n"
        "[tilde](nope2.md)\n"
        "~~~\n"
        "\n"
        "Inline `[not](a-link.md)` code and [real](ok.md).\n",
    )
    assert [h.text for h in doc.headings] == ["Real"]
    assert [l.raw_target for l in doc.links] == ["ok.md"]
    assert {3, 4, 5, 6, 8, 10, 11, 12} <= doc.code_lines  # both fences + indented


def test_parser_reference_links_and_explicit_anchors():
    doc = parse_markdown(
        "a.md",
        "# Title {#custom}\n"
        "\n"
        "[used][u] and [missing][m].\n"
        "\n"
        "[u]: target.md\n"
        "[unused]: nowhere.md\n",
    )
    assert doc.headings[0].explicit_id == "custom"
    targets = sorted(l.raw_target for l in doc.links)
    assert targets == ["target.md"]  # unused definition creates no edge


def test_parser_angle_bracket_and_title_destinations():
    doc = parse_markdown("a.md", "[x](<my file.md>) and [y](b.md \"title\").\n")
    assert [l.raw_target for l in doc.links] == ["my file.md", "b.md"]


# ------------------------------------------------------------------ analyze --

def test_analyze_detects_all_diagnostic_types():
    analysis = analyze(CORPUS)
    diags = analysis.diagnostics
    by_type = {}
    for d in diags:
        by_type.setdefault(d.type, []).append(d)

    broken_links = by_type["broken_link"]
    assert {(d.path, d.line) for d in broken_links} == {("index.md", 3), ("docs/guide.md", 9)}

    broken_anchors = by_type["broken_anchor"]
    assert {(d.path, d.line) for d in broken_anchors} == {
        ("docs/api.md", 3),
        ("docs/old/deep.md", 5),
    }

    collisions = by_type["anchor_collision"]
    assert len(collisions) == 1
    assert collisions[0].path == "docs/guide.md"
    assert collisions[0].line == 7
    assert collisions[0].details["assigned"] == "install-1"

    cycles = by_type["cycle"]
    assert {d.path for d in cycles} == {"index.md", "docs/guide.md", "docs/api.md"}

    # links inside the fenced code block were ignored
    assert all("ghost.md" not in str(d.details) for d in diags)
    assert analysis.summary["errors"] == 4
    assert analysis.summary["warnings"] == 4


# ---------------------------------------------------------------------- api --

def test_import_rejects_bad_paths(client):
    r = client.post("/api/imports", json={"documents": [{"path": "/abs.md", "content": "x"}]})
    assert r.status_code == 422
    r = client.post("/api/imports", json={"documents": [{"path": "../esc.md", "content": "x"}]})
    assert r.status_code == 422
    r = client.post(
        "/api/imports",
        json={"documents": [{"path": "a.md", "content": "x"}, {"path": "./a.md", "content": "y"}]},
    )
    assert r.status_code == 422  # duplicate after normalization


def test_import_and_get_documents(client):
    r = client.post(
        "/api/imports",
        json={"documents": [{"path": p, "content": c} for p, c in CORPUS.items()]},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["doc_count"] == 4
    import_id = body["import_id"]

    r = client.get(f"/api/imports/{import_id}")
    assert r.status_code == 200
    assert r.json()["paths"] == sorted(CORPUS)

    r = client.get(f"/api/imports/{import_id}/documents/docs/guide.md")
    assert r.status_code == 200
    assert r.json()["content"] == CORPUS["docs/guide.md"]

    assert client.get(f"/api/imports/{import_id}/documents/nope.md").status_code == 404
    assert client.get("/api/imports/does-not-exist").status_code == 404


def test_validation_flow_and_filters(client):
    _, vid, body = import_and_validate(client, CORPUS)
    assert body["summary"]["by_type"] == {
        "broken_link": 2,
        "broken_anchor": 2,
        "anchor_collision": 1,
        "cycle": 3,
    }

    r = client.get(f"/api/validations/{vid}", params={"type": "broken_link"})
    assert {d["path"] for d in r.json()["diagnostics"]} == {"index.md", "docs/guide.md"}

    r = client.get(f"/api/validations/{vid}", params={"path": "docs/api.md"})
    assert {d["type"] for d in r.json()["diagnostics"]} == {"broken_anchor", "cycle"}

    assert client.get("/api/validations/nope").status_code == 404
    assert client.post("/api/validations", json={"import_id": "nope"}).status_code == 404


def test_fix_preview_rewrites_only_anchors_and_references(client):
    _, vid, _ = import_and_validate(client, CORPUS)
    r = client.post(f"/api/validations/{vid}/fix-preview")
    assert r.status_code == 200, r.text
    preview = r.json()

    fixed = {d["path"]: d for d in preview["fixed_documents"]}
    assert set(fixed) == {"docs/guide.md", "docs/api.md"}

    # anchor collision: duplicate heading pinned with explicit anchor
    guide_lines = fixed["docs/guide.md"]["fixed_content"].split("\n")
    assert guide_lines[6] == "## Install  {#install-1}"
    # broken link: unique-basename rewrite, relative to the source document
    assert guide_lines[8] == "See [old](old/deep.md)."
    # code blocks and body text untouched
    assert fixed["docs/guide.md"]["fixed_content"].split("\n")[0] == "# Guide"

    # broken anchor: closest-match repair
    api_lines = fixed["docs/api.md"]["fixed_content"].split("\n")
    assert "guide.md#install" in api_lines[2]

    # unfixed: no-candidate link, empty anchor, and the cycles
    unfixed = preview["unfixed"]
    assert len(unfixed) == 5
    reasons = {(u["type"], u["path"]) for u in unfixed}
    assert ("broken_link", "index.md") in reasons
    assert ("broken_anchor", "docs/old/deep.md") in reasons
    assert sum(1 for u in unfixed if u["type"] == "cycle") == 3

    # every change is confined to a heading or a link destination
    for doc in preview["fixed_documents"]:
        for change in doc["changes"]:
            assert change["kind"] in {"heading-anchor", "link-target"}
            assert change["before"] != change["after"]


def test_fix_preview_result_revalidates_clean(client):
    import_id, vid, _ = import_and_validate(client, CORPUS)
    preview = client.post(f"/api/validations/{vid}/fix-preview").json()

    fixed_corpus = dict(CORPUS)
    for doc in preview["fixed_documents"]:
        fixed_corpus[doc["path"]] = doc["fixed_content"]

    reanalysis = analyze(fixed_corpus)
    remaining = {(d.type, d.path) for d in reanalysis.diagnostics}
    # fixable issues are gone; only unfixed ones remain
    assert ("anchor_collision", "docs/guide.md") not in remaining
    assert ("broken_anchor", "docs/api.md") not in remaining
    assert ("broken_link", "docs/guide.md") not in remaining
    assert ("broken_link", "index.md") in remaining
    assert ("cycle", "index.md") in remaining


def test_fix_preview_requires_done_status(client, tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    from app import db

    import_id = db.create_import({"a.md": "# A\n"})
    vid = db.create_validation(import_id)  # stays 'pending'
    r = client.post(f"/api/validations/{vid}/fix-preview")
    assert r.status_code == 409


def test_crlf_preserved_in_fix_preview(client):
    corpus = {"a.md": "# T\r\n\r\n## Dup\r\n\r\n## Dup\r\n"}
    _, vid, _ = import_and_validate(client, corpus)
    preview = client.post(f"/api/validations/{vid}/fix-preview").json()
    fixed = preview["fixed_documents"][0]["fixed_content"]
    assert "\r\n" in fixed
    assert fixed.split("\r\n")[4] == "## Dup  {#dup-1}"


# --------------------------------------------------------------- compare ----

BASE_CORPUS = {
    "index.md": "# Home\n\n[x](missing.md) and [y](gone.md).\n",
    "docs/a.md": "# A\n\nBack [home](../index.md).\n",
}

TARGET_CORPUS = {
    "index.md": "# Home\n\n[x](missing.md) and [z](new/missing.md).\n",
    "docs/a.md": "# A\n\n[anchor](../index.md#no-such-anchor).\n",
    "docs/b.md": "# B\n\nJust added.\n",
}


def test_compare_reports_diagnostic_and_document_changes(client):
    _, base_id, _ = import_and_validate(client, BASE_CORPUS)
    _, target_id, target_body = import_and_validate(client, TARGET_CORPUS)

    r = client.post(
        "/api/validations/compare",
        json={
            "base_validation_id": base_id,
            "target_validation_id": target_id,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["base_validation_id"] == base_id
    assert body["target_validation_id"] == target_id

    added = body["diagnostics"]["added"]
    removed = body["diagnostics"]["removed"]
    unchanged = body["diagnostics"]["unchanged"]

    # broken link to gone.md is gone; new/missing.md link and the broken
    # anchor in docs/a.md are newly reported
    assert {(d["type"], d["path"], d["line"]) for d in removed} == {
        ("broken_link", "index.md", 3),
    }
    assert "gone.md" in removed[0]["message"]
    assert {(d["type"], d["path"], d["line"]) for d in added} == {
        ("broken_link", "index.md", 3),
        ("broken_anchor", "docs/a.md", 3),
    }
    added_messages = [d["message"] for d in added]
    assert any("new/missing.md" in m for m in added_messages)

    # all returned diagnostics expose exactly the six fingerprint fields
    for d in added + removed + unchanged:
        assert set(d) == {"type", "severity", "path", "line", "message", "details"}

    assert body["diagnostics"]["counts"] == {
        "added": len(added),
        "removed": len(removed),
        "unchanged": len(unchanged),
    }
    assert target_body["summary"]["diagnostics"] == (
        len(added) + len(unchanged)
    )

    docs = body["documents"]
    assert docs["added"] == ["docs/b.md"]
    assert docs["removed"] == []
    assert docs["changed"] == ["docs/a.md", "index.md"]


def test_compare_detects_document_removal_and_swap(client):
    _, base_id, _ = import_and_validate(
        client, {"a.md": "# A\n", "b.md": "# B\n"}
    )
    _, target_id, _ = import_and_validate(
        client, {"a.md": "# A changed\n", "c.md": "# C\n"}
    )
    r = client.post(
        "/api/validations/compare",
        json={"base_validation_id": base_id, "target_validation_id": target_id},
    )
    assert r.status_code == 200, r.text
    docs = r.json()["documents"]
    assert docs == {"added": ["c.md"], "removed": ["b.md"], "changed": ["a.md"]}


def test_compare_reverse_direction_flips_added_and_removed(client):
    _, base_id, _ = import_and_validate(client, BASE_CORPUS)
    _, target_id, _ = import_and_validate(client, TARGET_CORPUS)

    forward = client.post(
        "/api/validations/compare",
        json={"base_validation_id": base_id, "target_validation_id": target_id},
    ).json()
    reverse = client.post(
        "/api/validations/compare",
        json={"base_validation_id": target_id, "target_validation_id": base_id},
    ).json()

    assert reverse["diagnostics"]["added"] == forward["diagnostics"]["removed"]
    assert reverse["diagnostics"]["removed"] == forward["diagnostics"]["added"]
    assert reverse["diagnostics"]["unchanged"] == forward["diagnostics"]["unchanged"]
    assert reverse["documents"]["added"] == forward["documents"]["removed"]
    assert reverse["documents"]["removed"] == forward["documents"]["added"]


def test_compare_identical_imports_shows_everything_unchanged(client):
    _, base_id, _ = import_and_validate(client, CORPUS)
    _, target_id, _ = import_and_validate(client, CORPUS)
    r = client.post(
        "/api/validations/compare",
        json={"base_validation_id": base_id, "target_validation_id": target_id},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["diagnostics"]["added"] == []
    assert body["diagnostics"]["removed"] == []
    assert body["diagnostics"]["counts"]["unchanged"] == 8
    assert body["documents"] == {"added": [], "removed": [], "changed": []}


def test_compare_self_reports_no_changes(client):
    _, vid, _ = import_and_validate(client, CORPUS)
    r = client.post(
        "/api/validations/compare",
        json={"base_validation_id": vid, "target_validation_id": vid},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["diagnostics"]["added"] == []
    assert body["diagnostics"]["removed"] == []
    assert body["diagnostics"]["counts"]["unchanged"] == 8
    assert body["documents"] == {"added": [], "removed": [], "changed": []}


def test_compare_results_are_sorted_by_path_line_type_message(client):
    base_corpus = {
        "z.md": "# Z\n\n[z1](x1.md)\n\n[z2](x2.md)\n",
        "a.md": "# A\n\n[a2](y2.md)\n\n[a1](y1.md)\n",
    }
    target_corpus = {
        "z.md": "# Z\n\n[z1](x1.md)\n\n[z2](x2.md)\n\n[z3](x3.md)\n",
        "a.md": "# A\n\n[a2](y2.md)\n\n[a1](y1.md)\n\n[a0](y0.md)\n",
    }
    _, base_id, _ = import_and_validate(client, base_corpus)
    _, target_id, _ = import_and_validate(client, target_corpus)
    body = client.post(
        "/api/validations/compare",
        json={"base_validation_id": base_id, "target_validation_id": target_id},
    ).json()

    keys = [
        (d["path"], d["line"] if d["line"] is not None else -1, d["type"], d["message"])
        for d in body["diagnostics"]["added"]
    ]
    assert keys == sorted(keys)
    assert [k[0] for k in keys] == ["a.md", "z.md"]
    assert [k[1] for k in keys] == [7, 7]


def test_compare_doc_level_diagnostics_sort_first(client):
    # two-doc mutual references form a cycle; removing one doc removes the
    # cycle members (doc-level, line=None) which must sort before line-based ones
    base_corpus = {
        "a.md": "# A\n\nto [b](b.md), [missing](nope.md) and [old](old.md).\n",
        "b.md": "# B\n\nto [a](a.md).\n",
    }
    target_corpus = {"a.md": "# A\n\n[missing](nope.md) and [other](other.md).\n"}
    _, base_id, _ = import_and_validate(client, base_corpus)
    _, target_id, _ = import_and_validate(client, target_corpus)
    body = client.post(
        "/api/validations/compare",
        json={"base_validation_id": base_id, "target_validation_id": target_id},
    ).json()

    removed = body["diagnostics"]["removed"]
    lines = [d["line"] for d in removed if d["path"] == "a.md"]
    assert lines[0] is None  # cycle member (doc-level) precedes line diagnostics
    keys = [
        (d["path"], d["line"] if d["line"] is not None else -1, d["type"], d["message"])
        for d in removed
    ]
    assert keys == sorted(keys)


def test_compare_unknown_validation_returns_404(client):
    _, vid, _ = import_and_validate(client, CORPUS)

    r = client.post(
        "/api/validations/compare",
        json={"base_validation_id": "does-not-exist", "target_validation_id": vid},
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "validation not found"

    r = client.post(
        "/api/validations/compare",
        json={"base_validation_id": vid, "target_validation_id": "does-not-exist"},
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "validation not found"


def test_compare_requires_done_status(client):
    from app import db

    _, done_id, _ = import_and_validate(client, CORPUS)
    import_id = db.create_import({"a.md": "# A\n"})
    pending_id = db.create_validation(import_id)  # stays 'pending'

    r = client.post(
        "/api/validations/compare",
        json={"base_validation_id": pending_id, "target_validation_id": done_id},
    )
    assert r.status_code == 409
    assert "pending" in r.json()["detail"]

    r = client.post(
        "/api/validations/compare",
        json={"base_validation_id": done_id, "target_validation_id": pending_id},
    )
    assert r.status_code == 409
    assert "pending" in r.json()["detail"]

    db.set_validation_running(pending_id)
    r = client.post(
        "/api/validations/compare",
        json={"base_validation_id": pending_id, "target_validation_id": done_id},
    )
    assert r.status_code == 409
    assert "running" in r.json()["detail"]

    db.finish_validation(pending_id, "failed", error="boom")
    r = client.post(
        "/api/validations/compare",
        json={"base_validation_id": pending_id, "target_validation_id": done_id},
    )
    assert r.status_code == 409
    assert "failed" in r.json()["detail"]


def test_compare_rejects_malformed_request(client):
    r = client.post("/api/validations/compare", json={"base_validation_id": "x"})
    assert r.status_code == 422
    r = client.post(
        "/api/validations/compare",
        json={"base_validation_id": "", "target_validation_id": "y"},
    )
    assert r.status_code == 422


def test_fingerprint_ignores_details_key_order():
    from app.comparison import diff_diagnostics

    d1 = {
        "type": "broken_anchor",
        "severity": "error",
        "path": "a.md",
        "line": 1,
        "message": "m",
        "details": {"target": "t", "resolved": "r"},
    }
    d2 = dict(d1, details={"resolved": "r", "target": "t"})
    result = diff_diagnostics([d1], [d2])
    assert result["added"] == []
    assert result["removed"] == []
    assert len(result["unchanged"]) == 1


def test_fingerprint_distinguishes_diagnostic_fields():
    from app.comparison import diff_diagnostics

    base = {
        "type": "broken_link",
        "severity": "error",
        "path": "a.md",
        "line": 1,
        "message": "m",
        "details": {"k": "v"},
    }
    for field, new_value in {
        "type": "broken_anchor",
        "severity": "warning",
        "path": "b.md",
        "line": 2,
        "message": "other",
        "details": {"k": "w"},
    }.items():
        target = dict(base)
        target[field] = new_value
        result = diff_diagnostics([base], [target])
        assert len(result["added"]) == 1, field
        assert len(result["removed"]) == 1, field
        assert result["unchanged"] == [], field


def test_diff_diagnostics_uses_multiset_semantics():
    # cycles emit one diagnostic per component member with identical message
    # structure but differing paths; here two truly identical fingerprints are
    # compared against one -> exactly one removed, one unchanged.
    from app.comparison import diff_diagnostics

    diag = {
        "type": "cycle",
        "severity": "warning",
        "path": "a.md",
        "line": None,
        "message": "circular reference detected: a.md -> b.md -> a.md",
        "details": {"cycle": ["a.md", "b.md"]},
    }
    result = diff_diagnostics([diag, diag], [diag])
    assert len(result["removed"]) == 1
    assert len(result["unchanged"]) == 1
    assert result["added"] == []


# --------------------------------------------------------------- sarif -----

def test_sarif_export_maps_all_diagnostics(client):
    _, vid, body = import_and_validate(client, CORPUS)
    r = client.get(f"/api/validations/{vid}/sarif")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/sarif+json")

    log = r.json()
    assert log["version"] == "2.1.0"
    assert log["$schema"].startswith("https://") and log["$schema"].endswith(".json")
    assert len(log["runs"]) == 1

    run = log["runs"][0]
    assert run["tool"]["driver"]["name"]
    assert run["tool"]["driver"]["version"]
    assert run["invocations"] == [{"executionSuccessful": True}]
    assert run["automationDetails"] == {"id": f"validation/{vid}"}

    rules = run["tool"]["driver"]["rules"]
    rule_ids = [rule["id"] for rule in rules]
    assert rule_ids == [
        "broken_link",
        "broken_anchor",
        "anchor_collision",
        "cycle",
    ]
    rule_index = {rule["id"]: i for i, rule in enumerate(rules)}

    results = run["results"]
    assert len(results) == body["summary"]["diagnostics"] == 8

    by_rule: dict[str, list] = {}
    for res in results:
        by_rule.setdefault(res["ruleId"], []).append(res)
        # ruleId/ruleIndex consistently reference the run-level rule table
        assert res["ruleIndex"] == rule_index[res["ruleId"]]
        loc = res["locations"][0]["physicalLocation"]
        assert "uri" in loc["artifactLocation"]

    # severity -> SARIF level mapping
    assert all(res["level"] == "error" for res in by_rule["broken_link"])
    assert all(res["level"] == "error" for res in by_rule["broken_anchor"])
    assert all(res["level"] == "warning" for res in by_rule["cycle"])
    # the CORPUS collision is the auto-disambiguation warning
    assert [res["level"] for res in by_rule["anchor_collision"]] == ["warning"]

    # paths, lines and messages are preserved and locatable
    broken = {(
        res["locations"][0]["physicalLocation"]["artifactLocation"]["uri"],
        res["locations"][0]["physicalLocation"]["region"]["startLine"],
    ): res for res in by_rule["broken_link"]}
    assert set(broken) == {("index.md", 3), ("docs/guide.md", 9)}
    res = broken[("index.md", 3)]
    assert "docs/nope.md" in res["message"]["text"]
    assert res["properties"]["severity"] == "error"
    assert res["properties"]["details"]["resolved"] == "docs/nope.md"

    # cycle diagnostics are document-level: no region, but still located
    for res in by_rule["cycle"]:
        loc = res["locations"][0]["physicalLocation"]
        assert "region" not in loc
        assert loc["artifactLocation"]["uri"] in {
            "index.md",
            "docs/guide.md",
            "docs/api.md",
        }
        assert "circular reference detected" in res["message"]["text"]
        assert res["properties"]["details"]["cycle"]


def test_sarif_error_level_collision_overrides_rule_default(client):
    # duplicate explicit anchors are errors even though the rule defaults to warning
    corpus = {"a.md": "# H {#dup}\n\n# J {#dup}\n"}
    _, vid, _ = import_and_validate(client, corpus)
    log = client.get(f"/api/validations/{vid}/sarif").json()
    results = log["runs"][0]["results"]
    assert len(results) == 1
    assert results[0]["ruleId"] == "anchor_collision"
    assert results[0]["level"] == "error"
    assert results[0]["locations"][0]["physicalLocation"]["region"] == {
        "startLine": 3
    }


def test_sarif_results_are_stable_and_sorted(client):
    _, vid, _ = import_and_validate(client, CORPUS)
    first = client.get(f"/api/validations/{vid}/sarif").json()
    second = client.get(f"/api/validations/{vid}/sarif").json()
    assert first == second  # byte-identical re-export

    keys = [
        (
            res["locations"][0]["physicalLocation"]["artifactLocation"]["uri"],
            res["locations"][0]["physicalLocation"].get("region", {}).get(
                "startLine", -1
            ),
            res["ruleId"],
            res["message"]["text"],
        )
        for res in first["runs"][0]["results"]
    ]
    assert keys == sorted(keys)


def test_sarif_percent_encodes_artifact_uris(client):
    corpus = {"my docs/a.md": "# A\n\n[x](missing.md).\n"}
    _, vid, _ = import_and_validate(client, corpus)
    log = client.get(f"/api/validations/{vid}/sarif").json()
    result = log["runs"][0]["results"][0]
    assert result["ruleId"] == "broken_link"
    uri = result["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
    assert uri == "my%20docs/a.md"


def test_sarif_empty_when_validation_is_clean(client):
    _, vid, _ = import_and_validate(client, {"a.md": "# A\n\nText only.\n"})
    log = client.get(f"/api/validations/{vid}/sarif").json()
    assert log["version"] == "2.1.0"
    assert log["runs"][0]["results"] == []
    assert [r["id"] for r in log["runs"][0]["tool"]["driver"]["rules"]]


def test_sarif_unknown_validation_returns_404(client):
    r = client.get("/api/validations/does-not-exist/sarif")
    assert r.status_code == 404
    assert r.json()["detail"] == "validation not found"


def test_sarif_requires_done_status(client):
    from app import db

    import_id = db.create_import({"a.md": "# A\n"})
    vid = db.create_validation(import_id)  # stays 'pending'
    r = client.get(f"/api/validations/{vid}/sarif")
    assert r.status_code == 409
    assert "pending" in r.json()["detail"]

    db.set_validation_running(vid)
    assert client.get(f"/api/validations/{vid}/sarif").status_code == 409

    db.finish_validation(vid, "failed", error="boom")
    r = client.get(f"/api/validations/{vid}/sarif")
    assert r.status_code == 409
    assert "failed" in r.json()["detail"]


# ------------------------------------------------------------------ gate ---

def test_gate_without_configuration_counts_raw_diagnostics_and_passes(client):
    _, vid, body = import_and_validate(client, CORPUS)
    r = client.post(f"/api/validations/{vid}/gate", json={})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "pass"
    assert out["passed"] is True
    assert out["counts"]["raw"]["total"] == body["summary"]["diagnostics"] == 8
    assert out["counts"]["raw"] == {
        "total": 8,
        "error": 4,
        "warning": 4,
        "by_type": {
            "broken_link": {"total": 2, "error": 2, "warning": 0},
            "broken_anchor": {"total": 2, "error": 2, "warning": 0},
            "anchor_collision": {"total": 1, "error": 0, "warning": 1},
            "cycle": {"total": 3, "error": 0, "warning": 3},
        },
    }
    assert out["counts"]["after_ignore"] == out["counts"]["raw"]
    assert out["counts"]["ignored"] == 0
    assert out["matched_rules"] == []
    assert out["violations"] == []
    assert out["failing_diagnostics"] == []


def test_gate_threshold_failure_reports_violations_and_failing_diagnostics(client):
    _, vid, _ = import_and_validate(client, CORPUS)
    r = client.post(
        f"/api/validations/{vid}/gate",
        json={"thresholds": {"broken_link": {"error": 1}, "cycle": {"warning": 0}}},
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "fail"
    assert out["passed"] is False

    violations = {(v["type"], v["severity"]): v for v in out["violations"]}
    assert violations["broken_link", "error"] == {
        "type": "broken_link",
        "severity": "error",
        "threshold": 1,
        "actual": 2,
    }
    assert violations["cycle", "warning"]["actual"] == 3

    # only diagnostics of the exceeded buckets block the gate
    failing = out["failing_diagnostics"]
    assert {d["type"] for d in failing} == {"broken_link", "cycle"}
    assert len(failing) == 5
    # the non-exceeded broken_anchor bucket is not part of the failure list
    assert all(d["type"] != "broken_anchor" for d in failing)
    # failing diagnostics carry the full six-field projection and are sorted
    for d in failing:
        assert set(d) == {"type", "severity", "path", "line", "message", "details"}
    keys = [
        (d["path"], d["line"] if d["line"] is not None else -1, d["type"], d["message"])
        for d in failing
    ]
    assert keys == sorted(keys)


def test_gate_threshold_equal_to_count_passes(client):
    _, vid, _ = import_and_validate(client, CORPUS)
    r = client.post(
        f"/api/validations/{vid}/gate",
        json={"thresholds": {"broken_link": {"error": 2}}},
    )
    assert r.status_code == 200
    out = r.json()
    assert out["status"] == "pass"
    assert out["violations"][0]["actual"] == 2
    assert out["failing_diagnostics"] == []


def test_gate_unknown_type_threshold_is_evaluated_against_zero(client):
    _, vid, _ = import_and_validate(client, CORPUS)
    # thresholds are upper bounds: zero allowance on an absent type passes
    r = client.post(
        f"/api/validations/{vid}/gate",
        json={"thresholds": {"no_such_type": {"error": 0, "warning": 0}}},
    )
    assert r.status_code == 200
    out = r.json()
    assert out["status"] == "pass"
    assert out["violations"] == [
        {"type": "no_such_type", "severity": "error", "threshold": 0, "actual": 0},
        {"type": "no_such_type", "severity": "warning", "threshold": 0, "actual": 0},
    ]
    assert "no_such_type" in out["counts"]["after_ignore"]["by_type"]


def test_gate_ignore_rule_by_type_reduces_counts(client):
    _, vid, _ = import_and_validate(client, CORPUS)
    r = client.post(
        f"/api/validations/{vid}/gate",
        json={
            "thresholds": {"broken_link": {"error": 0}},
            "ignore_rules": [{"type": "broken_link"}],
        },
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "pass"
    assert out["counts"]["raw"]["error"] == 4
    assert out["counts"]["after_ignore"]["error"] == 2
    assert out["counts"]["ignored"] == 2
    assert out["counts"]["after_ignore"]["by_type"]["broken_link"] == {
        "total": 0,
        "error": 0,
        "warning": 0,
    }
    assert out["matched_rules"] == [
        {"rule_index": 0, "name": None, "ignored_count": 2}
    ]
    assert out["failing_diagnostics"] == []


def test_gate_ignore_rule_by_path_glob_and_message_regex(client):
    _, vid, _ = import_and_validate(client, CORPUS)
    r = client.post(
        f"/api/validations/{vid}/gate",
        json={
            "thresholds": {"broken_link": {"error": 0}},
            "ignore_rules": [
                {"path_glob": "**/guide.md"},
                {"message_regex": r"nope\.md"},
            ],
        },
    )
    out = r.json()
    assert out["status"] == "pass"
    # rule 0 suppresses every diagnostic under docs/ (incl. the guide broken
    # link, the anchor collision and two docs-level cycle entries); rule 1
    # additionally suppresses the index.md broken link via its message
    assert out["counts"]["ignored"] == 4
    hits = {(h["rule_index"]): h["ignored_count"] for h in out["matched_rules"]}
    assert hits[0] >= 3
    assert hits[1] == 1
    assert out["counts"]["after_ignore"]["by_type"]["broken_link"] == {
        "total": 0,
        "error": 0,
        "warning": 0,
    }


def test_gate_rule_without_criteria_matches_everything(client):
    _, vid, _ = import_and_validate(client, CORPUS)
    r = client.post(
        f"/api/validations/{vid}/gate",
        json={
            "thresholds": {
                "broken_link": {"error": 0},
                "broken_anchor": {"error": 0},
                "anchor_collision": {"warning": 0},
                "cycle": {"warning": 0},
            },
            "ignore_rules": [{"name": "suppress-all"}],
        },
    )
    out = r.json()
    assert out["status"] == "pass"
    assert out["counts"]["ignored"] == 8
    assert out["matched_rules"] == [
        {"rule_index": 0, "name": "suppress-all", "ignored_count": 8}
    ]
    assert out["counts"]["after_ignore"]["total"] == 0


def test_gate_more_specific_path_glob_wins_conflict(client):
    _, vid, _ = import_and_validate(client, CORPUS)
    # both rules match the diagnostics on docs/guide.md; only the more
    # specific (literal, deeper) glob gets credit for suppressing them
    r = client.post(
        f"/api/validations/{vid}/gate",
        json={
            "ignore_rules": [
                {"name": "broad", "path_glob": "**/*.md"},
                {"name": "narrow", "path_glob": "docs/guide.md"},
            ]
        },
    )
    out = r.json()
    assert out["status"] == "pass"
    hits = {h["name"]: h for h in out["matched_rules"]}
    # docs/guide.md carries three diagnostics: broken_link, anchor_collision,
    # and one cycle entry — all credited to the narrow rule despite order
    assert hits["narrow"] == {"rule_index": 1, "name": "narrow", "ignored_count": 3}
    # the broad rule claims the remaining five diagnostics elsewhere
    assert hits["broad"]["ignored_count"] == 5
    assert out["counts"]["ignored"] == 8


def test_gate_path_specificity_beats_submission_order(client):
    # an earlier broad rule must not shadow a later, narrower one
    from app.gate import CompiledRule
    from app.schemas import IgnoreRuleIn

    broad = CompiledRule(0, IgnoreRuleIn(path_glob="**/*.md"))
    narrow = CompiledRule(1, IgnoreRuleIn(path_glob="docs/guide.md"))
    diag = {"type": "broken_link", "severity": "error", "path": "docs/guide.md",
            "line": 9, "message": "x", "details": {}}
    from app.gate import _winning_rule
    assert _winning_rule(diag, [broad, narrow]) is narrow
    assert _winning_rule(diag, [narrow, broad]) is narrow


def test_gate_type_scoped_rule_beats_type_agnostic_on_equal_glob(client):
    from app.gate import CompiledRule, _winning_rule
    from app.schemas import IgnoreRuleIn

    agnostic = CompiledRule(0, IgnoreRuleIn(path_glob="docs/*.md"))
    typed = CompiledRule(1, IgnoreRuleIn(path_glob="docs/*.md", type="broken_link"))
    diag = {"type": "broken_link", "severity": "error", "path": "docs/guide.md",
            "line": 9, "message": "x", "details": {}}
    assert _winning_rule(diag, [agnostic, typed]) is typed
    # a different type is only eligible for the agnostic rule
    other = dict(diag, type="cycle", line=None)
    assert _winning_rule(other, [agnostic, typed]) is agnostic


def test_gate_equal_specificity_falls_back_to_submission_order(client):
    from app.gate import CompiledRule, _winning_rule
    from app.schemas import IgnoreRuleIn

    diag = {"type": "broken_link", "severity": "error", "path": "a.md",
            "line": 1, "message": "link target does not match", "details": {}}

    earlier = CompiledRule(0, IgnoreRuleIn(message_regex="link"))
    later = CompiledRule(1, IgnoreRuleIn(message_regex=r"does not"))
    # index encodes submission order, so the earlier rule wins however the
    # compiled list happens to be iterated
    assert _winning_rule(diag, [earlier, later]) is earlier
    assert _winning_rule(diag, [later, earlier]) is earlier


def test_gate_message_regex_uses_search_semantics(client):
    _, vid, _ = import_and_validate(client, CORPUS)
    r = client.post(
        f"/api/validations/{vid}/gate",
        json={"ignore_rules": [{"message_regex": r"nope\.md$"}]},
    )
    assert r.status_code == 200
    assert r.json()["counts"]["ignored"] == 0  # message ends with a closing quote
    r = client.post(
        f"/api/validations/{vid}/gate",
        json={"ignore_rules": [{"message_regex": r"docs/nope\.md"}]},
    )
    assert r.status_code == 200
    assert r.json()["counts"]["ignored"] == 1


def test_gate_all_criteria_must_match(client):
    _, vid, _ = import_and_validate(client, CORPUS)
    # path matches but type does not -> nothing suppressed
    r = client.post(
        f"/api/validations/{vid}/gate",
        json={"ignore_rules": [{"path_glob": "docs/guide.md", "type": "broken_anchor"}]},
    )
    assert r.json()["counts"]["ignored"] == 0
    assert r.json()["matched_rules"] == []
    r = client.post(
        f"/api/validations/{vid}/gate",
        json={"ignore_rules": [{"path_glob": "docs/guide.md", "type": "broken_link"}]},
    )
    assert r.json()["counts"]["ignored"] == 1


def test_gate_response_is_deterministic(client):
    _, vid, _ = import_and_validate(client, CORPUS)
    payload = {
        "thresholds": {"broken_link": {"error": 1}, "cycle": {"warning": 0}},
        "ignore_rules": [{"path_glob": "docs/**"}],
    }
    first = client.post(f"/api/validations/{vid}/gate", json=payload).json()
    second = client.post(f"/api/validations/{vid}/gate", json=payload).json()
    assert first == second


def test_gate_rejects_invalid_message_regex(client):
    _, vid, _ = import_and_validate(client, CORPUS)
    r = client.post(
        f"/api/validations/{vid}/gate",
        json={"ignore_rules": [{"message_regex": "(unclosed"}]},
    )
    assert r.status_code == 422
    assert "message_regex" in r.text


def test_gate_rejects_invalid_path_glob(client):
    _, vid, _ = import_and_validate(client, CORPUS)
    r = client.post(
        f"/api/validations/{vid}/gate",
        json={"ignore_rules": [{"path_glob": "docs/[a-*.md"}]},
    )
    assert r.status_code == 422
    assert "path_glob" in r.text


def test_gate_rejects_negative_thresholds(client):
    _, vid, _ = import_and_validate(client, CORPUS)
    r = client.post(
        f"/api/validations/{vid}/gate",
        json={"thresholds": {"broken_link": {"error": -1}}},
    )
    assert r.status_code == 422
    r = client.post(
        f"/api/validations/{vid}/gate",
        json={"thresholds": {"broken_link": {"warning": -2}}},
    )
    assert r.status_code == 422


def test_gate_rejects_empty_regex_and_glob(client):
    _, vid, _ = import_and_validate(client, CORPUS)
    assert client.post(
        f"/api/validations/{vid}/gate",
        json={"ignore_rules": [{"message_regex": ""}]},
    ).status_code == 422
    assert client.post(
        f"/api/validations/{vid}/gate",
        json={"ignore_rules": [{"path_glob": "   "}]},
    ).status_code == 422


def test_gate_rejects_unknown_fields(client):
    _, vid, _ = import_and_validate(client, CORPUS)
    r = client.post(
        f"/api/validations/{vid}/gate",
        json={"thresholds": {"broken_link": {"errors": 0}}},
    )
    assert r.status_code == 422
    r = client.post(
        f"/api/validations/{vid}/gate",
        json={"ignore_rules": [{"path": "a.md"}]},
    )
    assert r.status_code == 422


def test_gate_unknown_validation_returns_404(client):
    r = client.post("/api/validations/does-not-exist/gate", json={})
    assert r.status_code == 404
    assert r.json()["detail"] == "validation not found"


def test_gate_requires_done_status(client):
    from app import db

    _, done_id, _ = import_and_validate(client, CORPUS)
    import_id = db.create_import({"a.md": "# A\n"})
    vid = db.create_validation(import_id)  # stays 'pending'
    r = client.post(f"/api/validations/{vid}/gate", json={})
    assert r.status_code == 409
    assert "pending" in r.json()["detail"]

    db.set_validation_running(vid)
    assert client.post(f"/api/validations/{vid}/gate", json={}).status_code == 409

    db.finish_validation(vid, "failed", error="boom")
    r = client.post(f"/api/validations/{vid}/gate", json={})
    assert r.status_code == 409
    assert "failed" in r.json()["detail"]


def test_path_glob_translation():
    from app.gate import path_glob_to_regex

    def matches(glob, path):
        return re.fullmatch(path_glob_to_regex(glob)[1:-1], path) is not None

    assert matches("docs/*.md", "docs/guide.md")
    assert not matches("docs/*.md", "docs/api/guide.md")
    assert matches("**/*.md", "index.md")
    assert matches("**/*.md", "docs/guide.md")
    assert matches("docs/**/guide.md", "docs/guide.md")
    assert matches("docs/**/guide.md", "docs/a/b/guide.md")
    assert matches("docs/**", "docs/guide.md")
    assert matches("docs/**", "docs/a/b/guide.md")
    assert not matches("docs/**", "docs")
    assert not matches("docs/**", "docsx/guide.md")
    assert matches("docs/guide.md", "docs/guide.md")
    assert not matches("docs/guide.md", "docs/other.md")
    assert matches("docs/a?.md", "docs/a1.md")
    assert not matches("docs/a?.md", "docs/a/b.md")
    assert matches("docs/[gx].md", "docs/g.md")
    assert not matches("docs/[gx].md", "docs/a.md")

