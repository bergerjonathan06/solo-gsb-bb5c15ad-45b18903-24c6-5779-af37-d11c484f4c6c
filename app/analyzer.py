"""Reference-graph construction and validation.

Builds the cross-document reference graph from parsed Markdown and emits
diagnostics: broken links, broken anchors, anchor collisions and cycles.
"""
from __future__ import annotations

import posixpath
import re
from collections import defaultdict
from dataclasses import dataclass, field
from urllib.parse import unquote

from .parser import Heading, Link, ParsedDoc, parse_markdown

SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")

BROKEN_LINK = "broken_link"
BROKEN_ANCHOR = "broken_anchor"
ANCHOR_COLLISION = "anchor_collision"
CYCLE = "cycle"


@dataclass
class Diagnostic:
    type: str
    severity: str                  # error | warning
    path: str                      # document the diagnostic belongs to
    line: int | None               # 1-based line, None for doc-level issues
    message: str
    details: dict = field(default_factory=dict)
    # internal references used by the fixer; never serialized
    link: Link | None = None
    heading: Heading | None = None

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "severity": self.severity,
            "path": self.path,
            "line": self.line,
            "message": self.message,
            "details": self.details,
        }


@dataclass
class Analysis:
    parsed: dict[str, ParsedDoc]
    anchors: dict[str, dict[str, Heading]]   # path -> anchor -> heading
    graph: dict[str, set[str]]               # path -> referenced paths
    diagnostics: list[Diagnostic]
    summary: dict


def resolve_target(src_path: str, target_path: str) -> str:
    """Resolve a relative link target against the source document path."""
    if target_path.startswith("/"):
        return posixpath.normpath(target_path.lstrip("/"))
    return posixpath.normpath(posixpath.join(posixpath.dirname(src_path), target_path))


def split_fragment(raw: str) -> tuple[str, str | None, bool]:
    """Split a link target into (path, fragment, had_fragment)."""
    if "#" in raw:
        path, _, frag = raw.partition("#")
        return path, frag, True
    return raw, None, False


def is_external(raw: str) -> bool:
    return bool(SCHEME_RE.match(raw)) or raw.startswith("//")


def _assign_anchors(pdoc: ParsedDoc, diagnostics: list[Diagnostic]) -> dict[str, Heading]:
    """Assign GitHub-style anchors and detect collisions."""
    table: dict[str, Heading] = {}
    base_counts: dict[str, int] = defaultdict(int)
    base_members: dict[str, list[Heading]] = defaultdict(list)

    for h in pdoc.headings:
        if h.explicit_id:
            anchor = h.explicit_id
        else:
            n = base_counts[h.base_slug]
            anchor = h.base_slug if n == 0 else f"{h.base_slug}-{n}"
            base_members[h.base_slug].append(h)
        base_counts[h.base_slug] += 1
        h.anchor = anchor
        if anchor in table:
            diagnostics.append(
                Diagnostic(
                    type=ANCHOR_COLLISION,
                    severity="error",
                    path=pdoc.path,
                    line=h.line,
                    message=(
                        f"duplicate anchor '#{anchor}' "
                        f"(first defined on line {table[anchor].line})"
                    ),
                    details={"anchor": anchor, "first_line": table[anchor].line},
                    heading=h,
                )
            )
        else:
            table[anchor] = h

    for base, members in base_members.items():
        if len(members) > 1:
            for h in members[1:]:
                diagnostics.append(
                    Diagnostic(
                        type=ANCHOR_COLLISION,
                        severity="warning",
                        path=pdoc.path,
                        line=h.line,
                        message=(
                            f"anchor '#{base}' is shared by {len(members)} headings; "
                            f"this heading was auto-assigned '#{h.anchor}'"
                        ),
                        details={
                            "anchor": base,
                            "assigned": h.anchor,
                            "occurrences": len(members),
                        },
                        heading=h,
                    )
                )
    return table


def find_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Iterative Tarjan SCC; returns components that form cycles."""
    index_of: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    counter = 0
    cycles: list[list[str]] = []

    for root in graph:
        if root in index_of:
            continue
        index_of[root] = lowlink[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        work = [(root, iter(sorted(graph[root])))]
        while work:
            node, it = work[-1]
            descended = False
            for nxt in it:
                if nxt not in index_of:
                    index_of[nxt] = lowlink[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(sorted(graph[nxt]))))
                    descended = True
                    break
                elif nxt in on_stack:
                    lowlink[node] = min(lowlink[node], index_of[nxt])
            if descended:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
            if lowlink[node] == index_of[node]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == node:
                        break
                if len(comp) > 1 or comp[0] in graph[comp[0]]:
                    cycles.append(sorted(comp))
    return cycles


def analyze(documents: dict[str, str]) -> Analysis:
    parsed = {p: parse_markdown(p, c) for p, c in documents.items()}
    diagnostics: list[Diagnostic] = []
    anchors: dict[str, dict[str, Heading]] = {}

    for path, pdoc in parsed.items():
        anchors[path] = _assign_anchors(pdoc, diagnostics)

    graph: dict[str, set[str]] = {p: set() for p in parsed}
    links_checked = 0

    for path, pdoc in parsed.items():
        for link in pdoc.links:
            raw = link.raw_target.strip()
            if not raw or is_external(raw):
                continue
            links_checked += 1
            target_part, frag, had_frag = split_fragment(raw)
            resolved = (
                path if target_part == "" else resolve_target(path, unquote(target_part))
            )
            if resolved not in parsed:
                diagnostics.append(
                    Diagnostic(
                        type=BROKEN_LINK,
                        severity="error",
                        path=path,
                        line=link.line,
                        message=f"link target '{raw}' does not match any imported document",
                        details={"target": raw, "resolved": resolved},
                        link=link,
                    )
                )
                continue
            if target_part != "":
                # fragment-only links are in-page navigation, not graph edges
                graph[path].add(resolved)
            if had_frag:
                frag_dec = unquote(frag or "")
                if frag_dec == "" or frag_dec not in anchors[resolved]:
                    diagnostics.append(
                        Diagnostic(
                            type=BROKEN_ANCHOR,
                            severity="error",
                            path=path,
                            line=link.line,
                            message=f"anchor '#{frag_dec}' not found in '{resolved}'",
                            details={
                                "target": raw,
                                "resolved": resolved,
                                "fragment": frag_dec,
                                "available": sorted(anchors[resolved]),
                            },
                            link=link,
                        )
                    )

    for comp in find_cycles(graph):
        route = " -> ".join(comp + [comp[0]])
        for member in comp:
            diagnostics.append(
                Diagnostic(
                    type=CYCLE,
                    severity="warning",
                    path=member,
                    line=None,
                    message=f"circular reference detected: {route}",
                    details={"cycle": comp},
                )
            )

    diagnostics.sort(key=lambda d: (d.path, d.line if d.line is not None else -1, d.type, d.message))

    by_type: dict[str, int] = defaultdict(int)
    by_severity: dict[str, int] = defaultdict(int)
    for d in diagnostics:
        by_type[d.type] += 1
        by_severity[d.severity] += 1
    summary = {
        "documents": len(parsed),
        "links_checked": links_checked,
        "diagnostics": len(diagnostics),
        "errors": by_severity.get("error", 0),
        "warnings": by_severity.get("warning", 0),
        "by_type": dict(by_type),
    }
    return Analysis(parsed, anchors, graph, diagnostics, summary)
