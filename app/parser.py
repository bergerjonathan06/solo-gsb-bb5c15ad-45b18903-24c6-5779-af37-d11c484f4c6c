"""Markdown parsing: headings, anchors, links and code-block awareness.

Scope (documented in README):
- ATX headings only (``# Title``), with optional explicit anchors ``{#id}``.
- Fenced (``` / ~~~) and 4-space indented code blocks are tracked, so
  headings and links inside them are ignored.
- Inline links, images and reference-style link definitions are extracted.
  Inline code spans are masked before link matching.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$")
HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*$")
CLOSING_HASHES_RE = re.compile(r"[ \t]+#+$")
EXPLICIT_ANCHOR_RE = re.compile(r"\{#([A-Za-z0-9_.:-]+)\}[ \t]*$")
INLINE_CODE_RE = re.compile(r"(`+).+?\1")
INLINE_LINK_RE = re.compile(r"(!?)\[[^\]\n]*\]\([ \t]*(<[^>\n]*>|[^\s)]+)")
REF_DEF_RE = re.compile(r"^[ \t]{0,3}\[([^\]\n]+)\]:[ \t]*(<[^>\n]*>|[^\s]+)")
REF_USAGE_RE = re.compile(r"!?\[([^\]\n]+)\]\[([^\]\n]*)\]")
_PUNCT_RE = re.compile(r"[^\w\s\-]", re.UNICODE)
_MD_LINK_IN_TEXT_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")


@dataclass
class Heading:
    line: int                      # 1-based line number
    level: int
    text: str                      # heading text without explicit anchor marker
    base_slug: str                 # GitHub-style slug of the text
    explicit_id: str | None        # explicit {#id} if present
    explicit_span: tuple[int, int] | None  # span of "{#id}" within the line
    anchor: str = ""               # final assigned anchor (filled by analyzer)


@dataclass
class Link:
    line: int                      # 1-based line number
    start: int                     # destination span within the line (no <>)
    end: int
    raw_target: str                # destination as written
    kind: str                      # inline | image | definition
    label: str | None = None       # normalized label for definitions


@dataclass
class ParsedDoc:
    path: str
    lines: list[str]
    newline: str
    headings: list[Heading] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    code_lines: set[int] = field(default_factory=set)


def slugify(text: str) -> str:
    """GitHub-style heading slug: lowercase, punctuation stripped,
    whitespace turned into hyphens. Word characters (incl. CJK) are kept."""
    text = text.strip().lower()
    text = _PUNCT_RE.sub("", text)
    text = re.sub(r"\s", "-", text)
    return text


def plain_text(md: str) -> str:
    """Strip inline markdown (links, emphasis, code) from heading text."""
    md = _MD_LINK_IN_TEXT_RE.sub(r"\1", md)
    md = re.sub(r"[`*_~]", "", md)
    return md


def _mask_inline_code(line: str) -> str:
    """Blank out inline code spans (positions preserved)."""
    return INLINE_CODE_RE.sub(lambda m: " " * len(m.group(0)), line)


def _norm_label(label: str) -> str:
    return " ".join(label.split()).lower()


def _strip_angles(token: str, start: int) -> tuple[int, int, str]:
    """Return (inner_start, inner_end, raw) for a destination token."""
    if token.startswith("<") and token.endswith(">"):
        return start + 1, start + len(token) - 1, token[1:-1]
    return start, start + len(token), token


def parse_markdown(path: str, content: str) -> ParsedDoc:
    newline = "\r\n" if "\r\n" in content else "\n"
    lines = content.split(newline)
    doc = ParsedDoc(path=path, lines=lines, newline=newline)

    in_fence = False
    fence_char = ""
    fence_len = 0
    prev_blank = True
    prev_indented = False
    ref_defs: dict[str, Link] = {}
    ref_usages: list[tuple[int, str]] = []

    for idx, line in enumerate(lines):
        lineno = idx + 1
        fence = FENCE_RE.match(line)

        if in_fence:
            doc.code_lines.add(lineno)
            if (
                fence
                and fence.group(1)[0] == fence_char
                and len(fence.group(1)) >= fence_len
                and fence.group(2).strip() == ""
            ):
                in_fence = False
            continue

        if fence and not (fence.group(1)[0] == "`" and "`" in fence.group(2)):
            in_fence = True
            fence_char = fence.group(1)[0]
            fence_len = len(fence.group(1))
            doc.code_lines.add(lineno)
            prev_blank = False
            prev_indented = False
            continue

        stripped = line.strip()
        if (
            (line.startswith("    ") or line.startswith("\t"))
            and stripped
            and (prev_blank or prev_indented)
        ):
            # indented code block (top-level simplification)
            doc.code_lines.add(lineno)
            prev_indented = True
            prev_blank = False
            continue

        prev_indented = False
        if not stripped:
            prev_blank = True
            continue
        prev_blank = False

        heading = HEADING_RE.match(line)
        if heading:
            text = CLOSING_HASHES_RE.sub("", heading.group(2))
            explicit_id = None
            explicit_span = None
            em = EXPLICIT_ANCHOR_RE.search(text)
            if em:
                explicit_id = em.group(1)
                explicit_span = (
                    heading.start(2) + em.start(),
                    heading.start(2) + em.end(),
                )
                text = (text[: em.start()] + text[em.end():]).rstrip()
            base = slugify(plain_text(text))
            if base or explicit_id:
                doc.headings.append(
                    Heading(
                        line=lineno,
                        level=len(heading.group(1)),
                        text=text,
                        base_slug=base,
                        explicit_id=explicit_id,
                        explicit_span=explicit_span,
                    )
                )
            # fall through: a heading may itself contain links

        masked = _mask_inline_code(line)

        dm = REF_DEF_RE.match(masked)
        if dm:
            s, e, raw = _strip_angles(dm.group(2), dm.start(2))
            label = _norm_label(dm.group(1))
            ref_defs[label] = Link(lineno, s, e, raw, "definition", label)

        for lm in INLINE_LINK_RE.finditer(masked):
            s, e, raw = _strip_angles(lm.group(2), lm.start(2))
            kind = "image" if lm.group(1) == "!" else "inline"
            doc.links.append(Link(lineno, s, e, raw, kind))

        for um in REF_USAGE_RE.finditer(masked):
            label = um.group(2) or um.group(1)
            ref_usages.append((lineno, _norm_label(label)))

    # only reference definitions that are actually used create edges
    used_labels = {label for _, label in ref_usages}
    for label, link in ref_defs.items():
        if label in used_labels:
            doc.links.append(link)

    return doc
