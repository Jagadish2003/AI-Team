"""Generated partner-documentation sections — 2.0-C3 T5 (AT-840).

Partner documentation names things a partner cannot see the source of: the
normalised concepts they may bind, the primitives they may compose, the lint
rules they will be held to. Every one of those lists lives in code, and a
hand-maintained copy of a list that moves is a copy that will be wrong — usually
without anybody noticing until a partner writes against it.

So the reference sections are **rendered from the same constants the platform
enforces**, spliced into the published markdown between generated markers, and a
CI test re-renders and compares. A drift is a build failure, not a support
ticket. This is the discipline ``manifest_schema_reference()`` already applies to
the schema reference, widened to the partner docs.

    python scripts/pack_sdk.py docs --check   # CI: are the docs current?
    python scripts/pack_sdk.py docs --write   # regenerate them in place

What is generated and what is not
---------------------------------
Only the **reference** blocks — concept vocabulary, primitive reference, manifest
shape, lint rules, aggregation floors. The prose around them (why a rule exists,
how to think about a primitive, the worked walkthrough) is written by hand and
must stay that way: generated prose reads like generated prose, and the parts of
partner documentation that actually teach are the parts a person wrote.

Dependency-free of ``app``; reads the declaration modules and nothing else.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from ..platform_capabilities import (
    NORMALISED_CONCEPTS,
    PLATFORM_VERSION,
    is_concept_available,
)
from .lint import LINT_RULES, FLOOR_PARAMETERS
from .manifest import MANIFEST_VERSION, manifest_schema_reference
from .primitives import PRIMITIVE_LIBRARY, PRIMITIVE_LIBRARY_VERSION, primitive_ids

#: Marker pair delimiting a generated block in a published document. The opening
#: marker carries the regeneration command, because the first thing an author who
#: edits generated text by hand needs is the way to stop doing that.
BEGIN_TEMPLATE = (
    "<!-- generated:{name} — regenerate with "
    "`python scripts/pack_sdk.py docs --write`; do not edit by hand -->"
)
END_TEMPLATE = "<!-- /generated:{name} -->"

_BLOCK_RE = re.compile(
    r"<!-- generated:(?P<name>[a-z_]+)[^>]*-->\n(?P<body>.*?)\n<!-- /generated:(?P=name) -->",
    re.DOTALL,
)

#: The partner documentation set, in reading order.
PARTNER_DOC_FILES: Tuple[str, ...] = (
    "README.md",
    "concept_vocabulary.md",
    "primitive_reference.md",
    "discipline_rules.md",
    "worked_example.md",
)


class ReferenceDocsError(ValueError):
    """A document names a generated section that does not exist."""


# ── Rendering helpers ─────────────────────────────────────────────────────────


def _cell(text: Any) -> str:
    """One markdown table cell: no pipes, no newlines, nothing that breaks a row."""
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> List[str]:
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    lines.extend("| " + " | ".join(_cell(cell) for cell in row) + " |" for row in rows)
    return lines


def _arity(minimum: int, maximum: Optional[int]) -> str:
    if maximum is None:
        return f"{minimum} or more"
    if maximum == minimum:
        return "exactly 1" if minimum == 1 else f"exactly {minimum}"
    return f"{minimum}–{maximum}"


def _bounds(parameter: Mapping[str, Any]) -> str:
    if parameter.get("choices"):
        return " \\| ".join(str(choice) for choice in parameter["choices"])
    low, high = parameter.get("minimum"), parameter.get("maximum")
    if low is None and high is None:
        return "true / false" if parameter.get("kind") == "boolean" else "—"
    if low is None:
        return f"up to {high}"
    if high is None:
        return f"from {low}"
    return f"{low} to {high}"


# ── Sections ──────────────────────────────────────────────────────────────────


def concept_vocabulary_section() -> str:
    """The normalised concepts a manifest may bind, and when each arrived."""
    rows = [
        (
            f"`{spec.concept_id}`",
            spec.since,
            "yes" if is_concept_available(spec.concept_id) else "no",
            spec.description,
        )
        for spec in sorted(
            NORMALISED_CONCEPTS.values(), key=lambda s: (s.since, s.concept_id)
        )
    ]
    lines = [
        f"Platform capability version **{PLATFORM_VERSION}** — "
        f"{len(NORMALISED_CONCEPTS)} normalised concepts.",
        "",
        *_table(("Concept", "Since", "Available here", "What it normalises"), rows),
    ]
    return "\n".join(lines)


def primitive_reference_section() -> str:
    """Every primitive, its concept arity, its parameters, and what it inherits."""
    lines = [
        f"Primitive library **{PRIMITIVE_LIBRARY_VERSION}** — "
        f"{len(PRIMITIVE_LIBRARY)} primitives. A detector names exactly one.",
    ]
    for primitive_id in primitive_ids():
        spec = PRIMITIVE_LIBRARY[primitive_id]
        entry = spec.to_dict()
        minimum, maximum = spec.concept_arity
        lines.extend(
            [
                "",
                f"#### `{primitive_id}` — {spec.label}",
                "",
                spec.description,
                "",
                f"**Concepts:** {_arity(minimum, maximum)}.",
                "",
                *_table(
                    ("Parameter", "Type", "Required", "Default", "Bounds / values", "Meaning"),
                    [
                        (
                            f"`{parameter['name']}`",
                            parameter["kind"],
                            "yes" if parameter["required"] else "no",
                            f"`{parameter['default']}`" if "default" in parameter else "—",
                            _bounds(parameter),
                            parameter["description"],
                        )
                        for parameter in entry["parameters"]
                    ],
                ),
                "",
                f"*Evidence:* {spec.evidence_semantics}",
                "",
                f"*Corroboration:* {spec.corroboration_semantics}",
            ]
        )
    return "\n".join(lines)


def manifest_shape_section() -> str:
    """The manifest's blocks, which are required, and the keys it refuses outright."""
    reference = manifest_schema_reference()
    blocks = reference["blocks"]
    required_top = set(reference["requiredTopLevelFields"])
    rows = []
    for name in reference["topLevelFields"]:
        block = blocks.get(name)
        fields = ", ".join(f"`{field}`" for field in block["fields"]) if block else "—"
        rows.append(
            (
                f"`{name}`",
                "required" if name in required_top else "optional",
                fields,
            )
        )
    lines = [
        f"Manifest version **{MANIFEST_VERSION}**. Every key below is the complete "
        f"set — the schema is closed, so an unrecognised key anywhere is an error, "
        f"not an ignored extra.",
        "",
        *_table(("Block", "Required", "Fields"), rows),
        "",
        "Keys refused anywhere in the document, at any depth, because they are how "
        "code gets smuggled into configuration:",
        "",
        "> "
        + ", ".join(f"`{key}`" for key in sorted(reference["forbiddenKeys"])),
    ]
    return "\n".join(lines)


def lint_rule_section() -> str:
    """The four non-negotiables, as the lint pass states them."""
    return "\n".join(
        _table(
            ("Rule", "The requirement"),
            [(f"`{rule}`", requirement) for rule, requirement in sorted(LINT_RULES.items())],
        )
    )


def aggregation_floor_section() -> str:
    """The minimum each primitive's floor parameter may be set to."""
    return "\n".join(
        _table(
            ("Primitive", "Floor parameter", "Minimum"),
            [
                (f"`{primitive}`", f"`{parameter}`", minimum)
                for primitive, (parameter, minimum) in sorted(FLOOR_PARAMETERS.items())
            ],
        )
    )


#: Every generated section, by the name a document's marker uses.
SECTIONS: Dict[str, Callable[[], str]] = {
    "concepts": concept_vocabulary_section,
    "primitives": primitive_reference_section,
    "manifest_shape": manifest_shape_section,
    "lint_rules": lint_rule_section,
    "aggregation_floors": aggregation_floor_section,
}


def render_section(name: str) -> str:
    """The current text of one generated section."""
    render = SECTIONS.get(name)
    if render is None:
        raise ReferenceDocsError(
            f"no generated section named {name!r} (known: {', '.join(sorted(SECTIONS))})"
        )
    return render()


def wrap_section(name: str) -> str:
    """A section with its markers — what a document actually carries."""
    return "\n".join(
        (BEGIN_TEMPLATE.format(name=name), render_section(name), END_TEMPLATE.format(name=name))
    )


# ── Document synchronisation ──────────────────────────────────────────────────


def section_names(text: str) -> List[str]:
    """Generated section names a document declares, in order of appearance."""
    return [match.group("name") for match in _BLOCK_RE.finditer(text)]


def apply_sections(text: str) -> str:
    """Return ``text`` with every generated block replaced by its current content."""
    unknown = [name for name in section_names(text) if name not in SECTIONS]
    if unknown:
        raise ReferenceDocsError(
            f"document references unknown generated section(s): {', '.join(unknown)}"
        )

    def _replace(match: "re.Match[str]") -> str:
        return wrap_section(match.group("name"))

    return _BLOCK_RE.sub(_replace, text)


def check_document(path: Any) -> List[str]:
    """Generated sections in one document that no longer match the code.

    An empty list means the document is current. A missing file is reported as a
    stale document rather than raising, so the CI check reports every problem in
    one pass instead of stopping at the first.
    """
    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError:
        return [f"{file_path.name}: cannot be read"]
    stale: List[str] = []
    for match in _BLOCK_RE.finditer(text):
        name = match.group("name")
        if name not in SECTIONS:
            stale.append(f"{file_path.name}: unknown generated section {name!r}")
            continue
        if match.group("body") != render_section(name):
            stale.append(f"{file_path.name}: generated section {name!r} is out of date")
    return stale


def default_docs_dir() -> Path:
    """The published partner documentation directory in this repository."""
    return Path(__file__).resolve().parents[4] / "docs" / "partner"


def sync_docs(directory: Any = None, *, write: bool = False) -> Dict[str, Any]:
    """Check (or rewrite) every partner document's generated sections.

    ``write=False`` is the CI mode: it changes nothing and reports what is stale.
    """
    docs_dir = Path(directory) if directory is not None else default_docs_dir()
    stale: List[str] = []
    updated: List[str] = []
    for name in PARTNER_DOC_FILES:
        path = docs_dir / name
        problems = check_document(path)
        if not problems:
            continue
        stale.extend(problems)
        if write and path.is_file():
            path.write_text(
                apply_sections(path.read_text(encoding="utf-8")), encoding="utf-8"
            )
            updated.append(name)
    # In write mode the verdict is what remains stale AFTER rewriting — a document
    # that could not be repaired (missing, or naming a section that does not
    # exist) must still fail rather than be reported as fixed.
    remaining = (
        [problem for name in PARTNER_DOC_FILES for problem in check_document(docs_dir / name)]
        if write
        else stale
    )
    return {
        "ok": not remaining,
        "docsDir": str(docs_dir),
        "stale": stale,
        "remaining": remaining,
        "updated": updated,
    }


__all__ = [
    "BEGIN_TEMPLATE",
    "END_TEMPLATE",
    "PARTNER_DOC_FILES",
    "SECTIONS",
    "ReferenceDocsError",
    "aggregation_floor_section",
    "apply_sections",
    "check_document",
    "concept_vocabulary_section",
    "default_docs_dir",
    "lint_rule_section",
    "manifest_shape_section",
    "primitive_reference_section",
    "render_section",
    "section_names",
    "sync_docs",
    "wrap_section",
]
