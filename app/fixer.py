"""Fix preview: compute minimal rewrites for fixable diagnostics.

Only heading-anchor markers and link destinations are rewritten, and only
on lines that are not inside code blocks (the parser never records links
or headings there). Body text and code blocks are left byte-identical.
"""
from __future__ import annotations

import difflib
import posixpath
import re
from collections import defaultdict
from dataclasses import dataclass

from .analyzer import (
    ANCHOR_COLLISION,
    BROKEN_ANCHOR,
    BROKEN_LINK,
    CYCLE,
    Analysis,
    analyze,
    split_fragment,
)
from .parser import Heading

_HEADING_INSERT_RE = re.compile(r"^(.*?)(\s+#+)?\s*$")


@dataclass
class SpanEdit:
    line: int          # 1-based
    start: int
    end: int
    text: str
    kind: str          # link-target | heading-anchor


def _encode_target_part(part: str) -> str:
    # keep unicode readable; only spaces must be escaped in Markdown destinations
    return part.replace(" ", "%20")


def _rebuild_target(raw: str, new_path: str | None, new_frag: str | None) -> str:
    path_part, frag, had_frag = split_fragment(raw)
    if new_path is not None:
        path_part = new_path
    if new_frag is not None:
        frag, had_frag = new_frag, True
    out = _encode_target_part(path_part)
    if had_frag:
        out += "#" + _encode_target_part(frag or "")
    return out


def _allocate(used: set[str], stem: str) -> str:
    candidate = stem
    n = 2
    while candidate in used:
        candidate = f"{stem}-{n}"
        n += 1
    used.add(candidate)
    return candidate


def _heading_edit(h: Heading, new_id: str) -> SpanEdit:
    if h.explicit_span:
        s, e = h.explicit_span
        return SpanEdit(h.line, s, e, "{#" + new_id + "}", "heading-anchor")
    return SpanEdit(h.line, -1, -1, new_id, "heading-anchor")  # insert, resolved later


def build_fix_preview(documents: dict[str, str]) -> dict:
    analysis: Analysis = analyze(documents)
    parsed, anchors = analysis.parsed, analysis.anchors

    edits: dict[str, list[SpanEdit]] = defaultdict(list)
    unfixed: list[dict] = []
    used = {p: set(table) for p, table in anchors.items()}

    # --- 1. anchor collisions -------------------------------------------------
    warnings = [d for d in analysis.diagnostics if d.type == ANCHOR_COLLISION and d.severity == "warning"]
    errors = [d for d in analysis.diagnostics if d.type == ANCHOR_COLLISION and d.severity == "error"]

    for d in warnings:
        # shared base slug: pin the auto-assigned anchor explicitly so the
        # heading no longer depends on fragile auto-numbering. Anchor value
        # is unchanged, so no reference needs rewriting.
        h = d.heading
        edits[d.path].append(_heading_edit(h, h.anchor))

    for d in errors:
        # duplicate final anchor: rename this heading to the next free id.
        # References to the anchor keep pointing at the first heading.
        h = d.heading
        new_id = _allocate(used[d.path], h.anchor)
        anchors[d.path][new_id] = h
        h.anchor = new_id
        edits[d.path].append(_heading_edit(h, new_id))

    # --- 2. broken anchors ----------------------------------------------------
    for d in analysis.diagnostics:
        if d.type != BROKEN_ANCHOR or d.link is None:
            continue
        target_doc = d.details["resolved"]
        frag = d.details["fragment"]
        matches = difflib.get_close_matches(
            frag, sorted(anchors[target_doc]), n=1, cutoff=0.6
        )
        if matches:
            new_target = _rebuild_target(d.link.raw_target, None, matches[0])
            edits[d.path].append(
                SpanEdit(d.link.line, d.link.start, d.link.end, new_target, "link-target")
            )
        else:
            unfixed.append({**d.to_dict(), "reason": "no similar anchor found in target document"})

    # --- 3. broken links ------------------------------------------------------
    for d in analysis.diagnostics:
        if d.type != BROKEN_LINK or d.link is None:
            continue
        resolved = d.details["resolved"]
        base = posixpath.basename(resolved)
        candidates = [p for p in parsed if posixpath.basename(p) == base]
        if len(candidates) == 1:
            start_dir = posixpath.dirname(d.path) or "."
            new_path = posixpath.relpath(candidates[0], start_dir)
            new_target = _rebuild_target(d.link.raw_target, new_path, None)
            edits[d.path].append(
                SpanEdit(d.link.line, d.link.start, d.link.end, new_target, "link-target")
            )
        else:
            reason = (
                "no imported document with that file name"
                if not candidates
                else f"ambiguous: multiple candidates {sorted(candidates)}"
            )
            unfixed.append({**d.to_dict(), "reason": reason})

    # --- 4. cycles are structural, not auto-fixable ---------------------------
    for d in analysis.diagnostics:
        if d.type == CYCLE:
            unfixed.append({**d.to_dict(), "reason": "circular references require manual restructuring"})

    # --- apply edits ----------------------------------------------------------
    fixed_documents = []
    edit_count = 0
    for path in sorted(edits):
        pdoc = parsed[path]
        lines = list(pdoc.lines)
        by_line: dict[int, list[SpanEdit]] = defaultdict(list)
        for e in edits[path]:
            by_line[e.line].append(e)

        changes = []
        for line_no in sorted(by_line):
            original = lines[line_no - 1]
            line_edits = by_line[line_no]
            resolved_edits = []
            for e in line_edits:
                if e.start < 0:  # heading-anchor insert: position from original line
                    m = _HEADING_INSERT_RE.match(original)
                    insert_at = len(m.group(1))  # before any closing hashes
                    resolved_edits.append((insert_at, insert_at, "  {#" + e.text + "}", e.kind))
                else:
                    resolved_edits.append((e.start, e.end, e.text, e.kind))
            # apply right-to-left so original spans stay valid
            cur = original
            kinds = set()
            for start, end, text, kind in sorted(
                resolved_edits, key=lambda x: (x[0], x[1]), reverse=True
            ):
                cur = cur[:start] + text + cur[end:]
                kinds.add(kind)
            lines[line_no - 1] = cur
            edit_count += len(line_edits)
            changes.append(
                {
                    "line": line_no,
                    "kind": "+".join(sorted(kinds)),
                    "before": original,
                    "after": cur,
                }
            )
        fixed_documents.append(
            {
                "path": path,
                "changes": changes,
                "fixed_content": pdoc.newline.join(lines),
            }
        )

    return {
        "fixed_documents": fixed_documents,
        "unfixed": unfixed,
        "stats": {
            "documents_rewritten": len(fixed_documents),
            "edits": edit_count,
            "unfixed": len(unfixed),
        },
    }
