"""Local pack scaffold — 2.0-C3 T3 (AT-838).

Generates a working pack project on disk: a manifest, fixtures that exercise it,
and a README describing the authoring loop.

The property that makes a scaffold worth having
------------------------------------------------
**What it writes passes the whole toolkit immediately** — schema validation, lint,
and the fixture harness — and a test asserts exactly that. A scaffold whose output
fails its own tooling teaches an author that the errors are noise, and from then
on they read past every real one.

It also scaffolds a NEGATIVE case, not just a firing one. A detector that fires on
everything passes a positive-only suite forever; the quiet case is the one that
catches it, and an author who is shown the pattern once tends to keep writing it.

Deterministic and offline: fixed fixture timestamps (no clock reads, so a
scaffolded suite does not start failing on a date nobody chose), no network, and
nothing outside the target directory is touched. Existing files are never
overwritten unless the caller explicitly asks.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..platform_capabilities import get_concept
from .harness import FIXTURES_DIRNAME, PACK_MANIFEST_FILENAME
from .manifest import MANIFEST_VERSION
from .primitives import PRIMITIVE_LIBRARY_VERSION

#: The concept a scaffolded pack reads by default. Any platform concept works;
#: this one exists at the earliest platform version, so the generated floor is
#: permissive and the author can narrow it.
DEFAULT_CONCEPT = "incident_workflow"

#: Fixture timeline. Fixed, never derived from the clock — see the module
#: docstring. ``as_of`` for each case is the latest record in that case.
_RECURRENCE_DATES = (
    "2026-01-05T09:00:00Z",
    "2026-01-10T09:00:00Z",
    "2026-01-15T09:00:00Z",
    "2026-01-20T09:00:00Z",
    "2026-01-25T09:00:00Z",
)
_AGEING_OPENED = "2025-12-01T09:00:00Z"
_AGEING_OBSERVED = "2026-01-10T09:00:00Z"


class ScaffoldError(ValueError):
    """The scaffold cannot be written where it was asked to go."""


@dataclass(frozen=True)
class ScaffoldResult:
    """What was written, so a CLI can print it and a test can assert on it."""

    directory: Path
    files: Sequence[Path]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "directory": str(self.directory),
            "files": [str(path) for path in self.files],
        }


def _minimum_platform_version(concepts: Sequence[str]) -> str:
    """The lowest platform version providing every required concept.

    Derived, not hardcoded: a scaffold that declared a floor its own concepts do
    not support would fail validation the moment the author ran it — and the
    self-contradiction check (AT-836) exists precisely to catch that.
    """
    floors = ["1.0.0"]
    for concept in concepts:
        spec = get_concept(concept)
        if spec is not None:
            floors.append(spec.since)
    return max(floors, key=lambda value: tuple(int(part) for part in value.split(".")))


def build_manifest_document(
    *,
    pack_id: str,
    pack_name: str,
    author_name: str,
    author_contact: str,
    concept: str = DEFAULT_CONCEPT,
    description: str = "",
) -> Dict[str, Any]:
    """The manifest a scaffold writes — valid, lint-clean, and runnable."""
    return {
        "manifestVersion": MANIFEST_VERSION,
        "primitiveLibraryVersion": PRIMITIVE_LIBRARY_VERSION,
        "pack": {
            "packId": pack_id,
            "packName": pack_name,
            "packVersion": "0.1.0",
            "domain": pack_id,
            "description": description
            or (
                f"{pack_name}: operational friction composed from platform "
                f"primitives over normalised concepts. Ships no code."
            ),
            "author": {"name": author_name, "contact": author_contact},
        },
        "compatibility": {
            "minPlatformVersion": _minimum_platform_version([concept]),
            "maxPlatformVersion": None,
            "requiredConcepts": [concept],
            "optionalConcepts": [],
        },
        "certification": {
            "requestedLevel": "community",
            "notes": "Request Partner review once the pack is exercised on real signal.",
        },
        "detectors": [
            {
                "detectorId": "repeated_work_item",
                "title": "The same work item recurs",
                "primitive": "recurrence",
                "concepts": [concept],
                "parameters": {
                    "min_occurrences": 4,
                    "window_days": 30,
                    "group_by": "signature",
                },
                "labels": {
                    "summary": "The same normalised work item recurs within the window.",
                    "whyItMatters": (
                        "Repeated identical work is the clearest candidate for an "
                        "assisting agent."
                    ),
                    "recommendation": (
                        "An agent handles the recurring cases; the residual requires "
                        "judgment."
                    ),
                },
            },
            {
                "detectorId": "queue_ageing",
                "title": "Work ages in a queue",
                "primitive": "ageing",
                "concepts": [concept],
                "parameters": {
                    "min_age_days": 14,
                    "min_items": 3,
                    "age_from": "opened_at",
                    "state_scope": "open",
                },
                "labels": {
                    "summary": "Open work items have been sitting past the ageing threshold.",
                    "whyItMatters": (
                        "A queue holding aged work signals a step with no owner or no "
                        "capacity."
                    ),
                },
            },
        ],
        "scorerCalibration": {
            "impactWeights": {
                "effort_concentration": 0.4,
                "breadth": 0.25,
                "recurrence_stability": 0.2,
                "automation_shape": 0.15,
            },
            "confidence": {
                "singleSourceCap": "MEDIUM",
                "corroboratedMax": "HIGH",
                "conversationSourceCap": "MEDIUM",
            },
        },
        "terminology": {
            "glossary": {
                "work_item": "One tracked unit of work in the source system.",
                "queue": "A named work list a group draws from.",
            },
            "languageMap": {"opportunity": "finding"},
            "llmContext": (
                "Operational friction analysis. Reference groups, queues, services, "
                "and entities only. Describe recurrence, ageing, and concentration as "
                "observed; never assert causation. Humans remain responsible for every "
                "action."
            ),
        },
    }


def _record(
    record_id: str,
    concept: str,
    *,
    observed_at: str,
    opened_at: Optional[str] = None,
    signature: str = "",
    state: str = "closed",
    entity_reference: str = "svc-example",
) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "concept": concept,
        "record_id": record_id,
        "source_system": "servicenow",
        "observed_at": observed_at,
        "entity_reference": entity_reference,
        "actor_group": "service-desk",
        "state": state,
    }
    if opened_at:
        record["opened_at"] = opened_at
    if signature:
        record["signature"] = signature
    return record


def build_case_documents(concept: str = DEFAULT_CONCEPT) -> Dict[str, Dict[str, Any]]:
    """The starter case suite: one firing case per detector, plus a quiet case."""
    recurring = {
        "name": "recurring work items are detected",
        "description": (
            "Five occurrences of one signature inside the window clear the "
            "recurrence floor of four."
        ),
        "signal": {
            "records": [
                _record(
                    f"WI-{index + 1:03d}",
                    concept,
                    observed_at=stamp,
                    signature="repeated_manual_step",
                )
                for index, stamp in enumerate(_RECURRENCE_DATES)
            ]
        },
        "expect": {
            "detectors": {
                "repeated_work_item": {
                    "fires": True,
                    "findingCount": 1,
                    "subjects": ["repeated_manual_step"],
                    "minMetric": 4,
                    "confidence": "MEDIUM",
                    "corroboration": "single_source",
                },
                "queue_ageing": {"fires": False},
            }
        },
    }

    ageing = {
        "name": "aged open work is detected",
        "description": (
            "Three open items opened forty days before the evaluation instant "
            "clear the ageing floor of three."
        ),
        "signal": {
            "records": [
                _record(
                    f"AGE-{index + 1:03d}",
                    concept,
                    observed_at=_AGEING_OBSERVED,
                    opened_at=_AGEING_OPENED,
                    state="open",
                )
                for index in range(3)
            ]
        },
        "expect": {
            "detectors": {
                "queue_ageing": {
                    "fires": True,
                    "findingCount": 1,
                    "subjects": ["svc-example"],
                    "confidence": "MEDIUM",
                },
                "repeated_work_item": {"fires": False},
            }
        },
    }

    quiet = {
        "name": "thin signal stays quiet",
        "description": (
            "The negative case. A detector that fires on everything passes a "
            "positive-only suite forever — keep a quiet case for every detector."
        ),
        "signal": {
            "records": [
                _record(
                    "WI-901",
                    concept,
                    observed_at=_RECURRENCE_DATES[0],
                    signature="repeated_manual_step",
                ),
                _record(
                    "WI-902",
                    concept,
                    observed_at=_RECURRENCE_DATES[1],
                    signature="repeated_manual_step",
                ),
            ]
        },
        "expect": {
            "findingCount": 0,
            "detectors": {
                "repeated_work_item": {"fires": False},
                "queue_ageing": {"fires": False},
            },
        },
    }

    return {
        "01_recurring_work_fires.json": recurring,
        "02_aged_work_fires.json": ageing,
        "03_thin_signal_is_quiet.json": quiet,
    }


def build_readme(pack_id: str, pack_name: str) -> str:
    """The authoring loop, written where the author will actually look."""
    return f"""# {pack_name}

An AgentIQ pack: **declarative configuration, no code**. Detectors are composed
from platform primitives over normalised concepts, so every finding inherits the
four-part criterion — evidence, confidence, corroboration status, source trace.

## Layout

    {PACK_MANIFEST_FILENAME}        the pack manifest — identity, compatibility, detectors, calibration
    {FIXTURES_DIRNAME}/            test cases: seeded signal + what you expect from it

## The loop

    python scripts/pack_sdk.py validate .     # is the manifest well-formed and closed?
    python scripts/pack_sdk.py lint .         # does it hold the platform's non-negotiables?
    python scripts/pack_sdk.py test .         # do the fixtures produce what you expect?
    python scripts/pack_sdk.py check .        # all three, the way installation runs them

## The rules you inherit

* **Groups, not people.** Findings reference groups, queues, services, and
  entities. Signal naming an individual is refused when it is admitted, not
  quietly dropped later.
* **Observation, not causation.** State recurrence, ageing, and concentration.
  Causation is the causal engine's to assert.
* **Aggregation floors.** A detector must need more than one record to fire.
* **Confidence is derived, never declared.** One source is capped and labelled;
  independent agreement raises it. Your calibration can lower a ceiling, never
  raise one.

## Next steps

1. Point `compatibility.requiredConcepts` at the concepts your detectors read.
2. Replace the starter detectors with the primitives that match your domain
   (`python scripts/pack_sdk.py primitives` lists them with their parameters).
3. Keep a quiet case per detector alongside every firing case.
4. Request certification review when the pack runs clean — the level is issued by
   CloudFulcrum, never self-applied in the manifest.

Pack id: `{pack_id}`
"""


def scaffold_pack(
    directory: Any,
    *,
    pack_id: str,
    pack_name: str = "",
    author_name: str = "",
    author_contact: str = "",
    concept: str = DEFAULT_CONCEPT,
    force: bool = False,
) -> ScaffoldResult:
    """Write a working pack project into ``directory``.

    Refuses to overwrite an existing file unless ``force`` — a scaffold that
    silently replaced an author's manifest would be the single worst bug this
    toolkit could have.
    """
    target = Path(directory)
    identifier = str(pack_id or "").strip()
    if not identifier:
        raise ScaffoldError("a pack id is required")

    document = build_manifest_document(
        pack_id=identifier,
        pack_name=pack_name or identifier.replace("_", " ").title(),
        author_name=author_name or "Your organisation",
        author_contact=author_contact or "packs@example.test",
        concept=concept,
    )
    cases = build_case_documents(concept)

    planned: Dict[Path, str] = {
        target / PACK_MANIFEST_FILENAME: json.dumps(document, indent=2) + "\n",
        target / "README.md": build_readme(
            identifier, document["pack"]["packName"]
        ),
    }
    for filename, case in cases.items():
        planned[target / FIXTURES_DIRNAME / filename] = (
            json.dumps(case, indent=2) + "\n"
        )

    existing = [path for path in planned if path.exists()]
    if existing and not force:
        raise ScaffoldError(
            "refusing to overwrite existing file(s): "
            + ", ".join(sorted(str(path) for path in existing))
            + " (pass force to replace them)"
        )

    written: List[Path] = []
    for path, content in planned.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return ScaffoldResult(directory=target, files=tuple(sorted(written)))


__all__ = [
    "DEFAULT_CONCEPT",
    "ScaffoldError",
    "ScaffoldResult",
    "build_case_documents",
    "build_manifest_document",
    "build_readme",
    "scaffold_pack",
]
