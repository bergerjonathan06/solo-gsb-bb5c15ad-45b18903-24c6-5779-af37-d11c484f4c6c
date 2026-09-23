"""End-to-end and unit tests for the reference-closure validator."""
from __future__ import annotations

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

def gate(client, vid, payload):
    return client.post(f"/api/validations/{vid}/gate", json=payload)


def test_gate_without_configuration_passes_and_reports_raw_counts(client):
    _, vid, body = import_and_validate(client, CORPUS)
    r = gate(client, vid, {})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["status"] == "pass"
    assert j["passed"] is True
    assert j["matched_rules"] == []
    assert j["ignored_diagnostics"] == []
    assert j["threshold_checks"] == []
    assert j["failing"] == []
    assert j["raw_counts"] == {
        "error": {
            "anchor_collision": 0,
            "broken_anchor": 2,
            "broken_link": 2,
            "cycle": 0,
        },
        "warning": {
            "anchor_collision": 1,
            "broken_anchor": 0,
            "broken_link": 0,
            "cycle": 3,
        },
    }
    assert j["ignored_counts"] == {
        sev: {t: 0 for t in body["summary"]["by_type"]}
        for sev in ("error", "warning")
    }
    assert j["counts"] == {"raw": 8, "ignored": 0, "effective": 8}


def test_gate_threshold_boundary_pass_and_fail(client):
    _, vid, _ = import_and_validate(client, CORPUS)

    # exactly at the threshold passes; one above fails
    r = gate(client, vid, {"thresholds": {"broken_link": {"error": 2}}})
    assert r.status_code == 200
    assert r.json()["status"] == "pass"

    r = gate(client, vid, {"thresholds": {"broken_link": {"error": 1}}})
    j = r.json()
    assert j["status"] == "fail"
    assert j["passed"] is False
    assert j["threshold_checks"] == [
        {"type": "broken_link", "severity": "error", "threshold": 1, "count": 2, "pass": False}
    ]
    assert len(j["failing"]) == 1
    bucket = j["failing"][0]
    assert bucket["type"] == "broken_link"
    assert bucket["severity"] == "error"
    assert bucket["threshold"] == 1
    assert bucket["count"] == 2
    failing_diags = bucket["diagnostics"]
    assert {(d["path"], d["line"]) for d in failing_diags} == {
        ("index.md", 3),
        ("docs/guide.md", 9),
    }
    for d in failing_diags:
        assert set(d) == {"type", "severity", "path", "line", "message", "details"}
    # failing diagnostics inherit the (path, line, type, message) ordering
    assert [d["path"] for d in failing_diags] == ["docs/guide.md", "index.md"]


def test_gate_warning_threshold_is_severity_scoped(client):
    _, vid, _ = import_and_validate(client, CORPUS)
    # cycle is warning-severity even though 3 exist; error threshold unaffected
    r = gate(client, vid, {"thresholds": {"cycle": {"error": 0, "warning": 3}}})
    assert r.json()["status"] == "pass"
    r = gate(client, vid, {"thresholds": {"cycle": {"warning": 2}}})
    j = r.json()
    assert j["status"] == "fail"
    assert j["failing"][0]["count"] == 3
    assert {d["path"] for d in j["failing"][0]["diagnostics"]} == {
        "index.md",
        "docs/guide.md",
        "docs/api.md",
    }
    # doc-level diagnostics (line=None) sort before line-based ones within a bucket
    assert all(d["line"] is None for d in j["failing"][0]["diagnostics"])


def test_gate_multiple_thresholds_aggregated(client):
    _, vid, _ = import_and_validate(client, CORPUS)
    r = gate(
        client,
        vid,
        {
            "thresholds": {
                "broken_link": {"error": 0},
                "broken_anchor": {"error": 0},
                "cycle": {"warning": 5},
            }
        },
    )
    j = r.json()
    assert j["status"] == "fail"
    assert {(f["type"], f["severity"]) for f in j["failing"]} == {
        ("broken_link", "error"),
        ("broken_anchor", "error"),
    }
    checks = {(c["type"], c["severity"], c["pass"]) for c in j["threshold_checks"]}
    assert checks == {
        ("broken_anchor", "error", False),
        ("broken_link", "error", False),
        ("cycle", "warning", True),
    }


def test_gate_ignore_by_type_glob_and_regex(client):
    _, vid, _ = import_and_validate(client, CORPUS)
    r = gate(
        client,
        vid,
        {
            "thresholds": {"broken_link": {"error": 0}, "cycle": {"warning": 0}},
            "ignore_rules": [
                {"type": "cycle"},                              # all cycle warnings
                {"path_glob": "docs/*.md", "type": "broken_link"},
                {"message_regex": r"does not match"},          # guide.md broken link
            ],
        },
    )
    j = r.json()
    assert j["status"] == "pass", j
    assert j["counts"] == {"raw": 8, "ignored": 5, "effective": 3}
    assert j["ignored_counts"]["error"]["broken_link"] == 2
    assert j["ignored_counts"]["warning"]["cycle"] == 3
    hits = {m["rule_index"]: m["matched_count"] for m in j["matched_rules"]}
    assert hits == {0: 3, 1: 1, 2: 1}
    ignored_by = {(d["path"], d["line"]): d["ignored_by"] for d in j["ignored_diagnostics"]}
    # guide.md broken link is claimed by the path-specific rule 1 even though
    # the broad message regex (rule 2) also matches
    assert ignored_by[("docs/guide.md", 9)] == 1
    assert ignored_by[("index.md", 3)] == 2
    assert {
        (d["path"], d["line"])
        for d in j["ignored_diagnostics"]
        if d["ignored_by"] == 0
    } == {("index.md", None), ("docs/guide.md", None), ("docs/api.md", None)}


def test_gate_ignored_diagnostics_are_excluded_from_threshold(client):
    _, vid, _ = import_and_validate(client, CORPUS)
    r = gate(
        client,
        vid,
        {
            "thresholds": {"broken_link": {"error": 0}},
            "ignore_rules": [
                {"type": "broken_link", "path_glob": "**"},  # both broken links
            ],
        },
    )
    j = r.json()
    assert j["status"] == "pass"
    assert j["threshold_checks"][0]["count"] == 0
    assert j["failing"] == []
    # raw counts remain the unfiltered truth
    assert j["raw_counts"]["error"]["broken_link"] == 2
    assert j["ignored_counts"]["error"]["broken_link"] == 2


def test_gate_priority_more_specific_path_wins_regardless_of_order(client):
    _, vid, _ = import_and_validate(client, CORPUS)
    # broad rule submitted first; the exact-path rule must claim the diagnosis
    r = gate(
        client,
        vid,
        {
            "ignore_rules": [
                {"path_glob": "docs/*"},            # 0: matches guide + api
                {"path_glob": "docs/guide.md"},    # 1: more specific
            ]
        },
    )
    j = r.json()
    hits = {m["rule_index"]: m["matched_count"] for m in j["matched_rules"]}
    assert hits[0] == 2  # both docs/api.md diagnostics (deep.md is beyond '*')
    assert hits[1] == 3  # all docs/guide.md diagnostics incl. its cycle member
    winners = {(d["path"], d["type"]): d["ignored_by"] for d in j["ignored_diagnostics"]}
    assert winners[("docs/guide.md", "broken_link")] == 1
    assert ("docs/old/deep.md", "broken_anchor") not in winners

    # and with the submission order flipped, priority must be identical
    r2 = gate(
        client,
        vid,
        {
            "ignore_rules": [
                {"path_glob": "docs/guide.md"},    # 0
                {"path_glob": "docs/*"},            # 1
            ]
        },
    )
    j2 = r2.json()
    assert {m["matched_count"] for m in j2["matched_rules"]} == {2, 3}
    winners2 = {(d["path"], d["type"]): d["ignored_by"] for d in j2["ignored_diagnostics"]}
    assert winners2[("docs/guide.md", "broken_link")] == 0


def test_gate_priority_type_constraint_is_second_axis(client):
    _, vid, _ = import_and_validate(client, CORPUS)
    # same path specificity; the typed rule claims matching diagnostics even
    # though it is submitted later
    r = gate(
        client,
        vid,
        {
            "ignore_rules": [
                {"path_glob": "docs/api.md"},                   # 0 untyped
                {"path_glob": "docs/api.md", "type": "cycle"},  # 1 typed
            ]
        },
    )
    j = r.json()
    hits = {m["rule_index"]: m["matched_count"] for m in j["matched_rules"]}
    assert hits[1] == 1  # the cycle member
    assert hits[0] == 1  # the broken anchor falls to the earlier untyped rule


def test_gate_priority_request_order_is_final_tiebreaker(client):
    _, vid, _ = import_and_validate(client, CORPUS)
    r = gate(
        client,
        vid,
        {
            "ignore_rules": [
                {"message_regex": "circular|anchor"},  # 0
                {"message_regex": "."},                # 1 matches everything else
            ]
        },
    )
    j = r.json()
    # equal specificity, neither typed: index 0 claims everything it matches
    cycle_diags = [d for d in j["ignored_diagnostics"] if d["type"] == "cycle"]
    assert cycle_diags and all(d["ignored_by"] == 0 for d in cycle_diags)


def test_gate_message_regex_uses_search_not_fullmatch(client):
    _, vid, _ = import_and_validate(client, CORPUS)
    r = gate(client, vid, {"ignore_rules": [{"message_regex": r"^anchor '#install'"}]})
    # the collision warning message starts with "anchor '#install' is shared..."
    j = r.json()
    assert j["matched_rules"][0]["matched_count"] == 1
    assert j["ignored_diagnostics"][0]["type"] == "anchor_collision"


def test_gate_glob_supports_double_star_segment(client):
    corpus = {
        "a.md": "# A\n\n[x](missing.md)\n",
        "x/b.md": "# B\n\n[y](gone.md)\n",
        "x/y/c.md": "# C\n\n[z](nope.md)\n",
    }
    _, vid, _ = import_and_validate(client, corpus)
    r = gate(client, vid, {"ignore_rules": [{"path_glob": "**/*.md"}]})
    # '**/*.md' spans zero or more segments: every document matches
    j = r.json()
    assert j["matched_rules"][0]["matched_count"] == 3
    r = gate(client, vid, {"ignore_rules": [{"path_glob": "*/*.md"}]})
    j = r.json()
    # single '*' cannot cross '/'
    assert {d["path"] for d in j["ignored_diagnostics"]} == {"x/b.md"}


def test_gate_rejects_invalid_requests(client):
    _, vid, _ = import_and_validate(client, CORPUS)

    def expect_422(payload):
        r = gate(client, vid, payload)
        assert r.status_code == 422, (payload, r.status_code, r.text)
        return r.json()["detail"]

    assert "regex" in expect_422({"ignore_rules": [{"message_regex": "("}]})
    assert "non-negative" in expect_422({"thresholds": {"broken_link": {"error": -1}}})
    assert "integer" in expect_422({"thresholds": {"broken_link": {"error": 1.5}}})
    assert "integer" in expect_422({"thresholds": {"broken_link": {"error": True}}})
    assert "unknown diagnostic type" in expect_422(
        {"thresholds": {"not_a_rule": {"error": 0}}}
    )
    assert "one of" in expect_422({"ignore_rules": [{"type": "not_a_rule"}]})
    assert "at least one" in expect_422({"ignore_rules": [{}]})
    # empty criteria strings are rejected by pydantic min_length
    assert gate(client, vid, {"ignore_rules": [{"path_glob": ""}]}).status_code == 422
    assert gate(client, vid, {"ignore_rules": [{"message_regex": ""}]}).status_code == 422
    # pydantic-level shape errors also return 422
    assert gate(client, vid, {"thresholds": []}).status_code == 422
    assert gate(client, vid, {"ignore_rules": "x"}).status_code == 422
    assert gate(client, vid, {"unknown_field": 1}).status_code == 422
    assert gate(client, vid, {"thresholds": {"broken_link": "nope"}}).status_code == 422


def test_gate_unknown_validation_returns_404(client):
    r = client.post("/api/validations/does-not-exist/gate", json={})
    assert r.status_code == 404
    assert r.json()["detail"] == "validation not found"


def test_gate_requires_done_status(client):
    from app import db

    _, done_id, _ = import_and_validate(client, CORPUS)
    import_id = db.create_import({"a.md": "# A\n"})
    pending_id = db.create_validation(import_id)  # stays 'pending'

    r = gate(client, pending_id, {"thresholds": {"broken_link": {"error": 0}}})
    assert r.status_code == 409
    assert "pending" in r.json()["detail"]

    db.set_validation_running(pending_id)
    assert gate(client, pending_id, {}).status_code == 409

    db.finish_validation(pending_id, "failed", error="boom")
    r = gate(client, pending_id, {})
    assert r.status_code == 409
    assert "failed" in r.json()["detail"]


def test_gate_glob_compiler_unit():
    from app.gate import _compile_glob, _glob_specificity

    def matches(pattern, path):
        return bool(_compile_glob(pattern).match(path))

    assert matches("docs/*.md", "docs/guide.md")
    assert not matches("docs/*.md", "docs/guide/notes.md")
    assert matches("docs/**/*.md", "docs/guide/notes.md")
    assert matches("docs/**", "docs/guide/notes.md")
    assert matches("**", "anything/at/all.md")
    assert matches("a/**/b.md", "a/b.md")
    assert matches("a/**/b.md", "a/x/y/b.md")
    assert matches("a?c.md", "abc.md")
    assert not matches("a?c.md", "a/c.md")
    assert matches("a[0-9].md", "a7.md")
    assert not matches("a[0-9].md", "ab.md")
    assert matches("literal+file.md", "literal+file.md")  # regex chars escaped

    assert _glob_specificity("docs/guide.md") > _glob_specificity("docs/*.md")
    assert _glob_specificity("docs/*.md") > _glob_specificity("*.md")
    assert _glob_specificity("*.md") > _glob_specificity("")

