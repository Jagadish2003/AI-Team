"""HP-2 T7 (AC6) — the on-prem configuration templates and the anti-drift gate.

HP-2 T7 ships two partner-facing templates under ``deployment/`` for configuring
the ``in_boundary`` provider against a customer-operated model server:

* ``deployment/ON_PREM_OLLAMA.md``
* ``deployment/ON_PREM_VLLM.md``

This module is the machine gate on them. It exists because of the story's own
sub-AC 5: *the supported-embedding-model table must be the single source HP-2.4's
dimension check reads from — the two cannot drift.* A table copied into prose is
exactly the artifact that goes stale silently, and a WRONG dimension in a
customer-facing template is worse than none: an operator sizing their model
server against it has no way to discover it is wrong until vectors fail to store.

So the relationship is enforced BIDIRECTIONALLY against
``app/retrieval/embedding_dimensions.MODEL_DIMENSIONS``:

* every row in either template must match the declared entry exactly — the model
  name, the dimension, AND the basis; and the dimension is re-checked through
  ``declared_dimension()``, the same function HP-2.4's guard calls, so the gate
  tests the real read path rather than a parallel one;
* every declared model must appear in both templates. Adding a model to the code
  and forgetting to document it fails the build.

Three further classes of drift are pinned for the same reason — a template that
names a variable, a limit, or a batch size the code does not actually use is a
template that sends an operator to configure something that will not take effect:

* every ``IN_BOUNDARY_*`` variable the templates name must be a real config key
  resolved from ``in_boundary_config`` (never a hardcoded list here);
* the chunk-size arithmetic both templates reason about must equal
  ``chunking.MAX_CHUNK_CHARS``;
* the documented embedding batch size must equal the code's default.

Finally, the templates' HONESTY structure is pinned. Neither Ollama nor vLLM was
installed when they were written — the configuration path was verified end to end
through the real gateway adapter against a local stub, and the parts that need a
real model server are listed as unverified. That split is the whole point of the
HP-2 story, so this module fails the build if the "not verified" section or the
blank version-record table is ever quietly deleted, and if any template starts
claiming a specific model-server version was validated when the reviewer sign-off
row is still empty.

Every gate here is proven to REJECT, not merely to pass: the parser/comparison is
driven over deliberately-corrupted synthetic tables in
``TestTheGateGoesRedWhenItShould``. A gate never observed failing is not known to
be a gate.

Note on vocabulary: this file deliberately names no model provider, endpoint, or
SDK. The R16-D1 no-bypass scanners sweep every ``.py`` under ``backend/``,
including tests, and the one permitted home for those names is
``app/model_gateway/``.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from app.model_gateway import in_boundary_config
from app.retrieval import chunking
from app.retrieval.embedding_dimensions import (
    BASIS_MEASURED,
    BASIS_PUBLISHED,
    MODEL_DIMENSIONS,
    declared_dimension,
)

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

BACKEND_ROOT: Path = Path(__file__).resolve().parents[2]
REPO_ROOT: Path = BACKEND_ROOT.parent
DEPLOYMENT_DIR: Path = REPO_ROOT / "deployment"

OLLAMA_TEMPLATE: Path = DEPLOYMENT_DIR / "ON_PREM_OLLAMA.md"
VLLM_TEMPLATE: Path = DEPLOYMENT_DIR / "ON_PREM_VLLM.md"

#: The two templates AC6 requires, by the server each configures.
TEMPLATES: Dict[str, Path] = {"ollama": OLLAMA_TEMPLATE, "vllm": VLLM_TEMPLATE}

#: Every document that RESTATES the dimension table, which is a strictly wider set
#: than the templates: ``deployment/README.md`` carries a third copy. It is not a
#: template (it configures nothing, so the template-content checks above would be
#: nonsense against it) but it is a copy of these numbers, and an operator sizes a
#: model server against whichever copy they happen to read. An unpinned restatement
#: is precisely the artifact that goes stale silently — a wrong number there has no
#: way of being discovered — so the drift gate below covers all three.
DIMENSION_DOCUMENTS: Dict[str, Path] = {
    **TEMPLATES,
    "deployment_readme": DEPLOYMENT_DIR / "README.md",
}

#: The four models sub-AC 4 names explicitly, with the dimension it names.
#: Written out rather than derived, because sub-AC 4 is a claim about THESE
#: numbers: deriving them from the table under test would make the check vacuous.
STORY_REQUIRED_MODELS: Dict[str, int] = {
    "nomic-embed-text": 768,
    "mxbai-embed-large": 1024,
    "all-MiniLM-L6-v2": 384,
    "bge-large-en-v1.5": 1024,
}

#: Sub-AC 4 requires this stated explicitly, not implied.
NO_MIGRATION_CLAIM = "no schema migration and no re-embed"

#: A dimension-table row: | `model` | <int> | published|measured |
#: Anchored to exactly three cells with an integer dimension and a basis drawn
#: from the code's own closed vocabulary, so the other three-column tables in
#: these documents (context windows, request shapes, troubleshooting) cannot be
#: mistaken for dimension rows.
_ROW_RE = re.compile(
    r"^\|\s*`(?P<model>[^`]+)`\s*\|\s*(?P<dims>\d+)\s*\|\s*"
    r"(?P<basis>" + BASIS_PUBLISHED + "|" + BASIS_MEASURED + r")\s*\|\s*$"
)

#: An ``IN_BOUNDARY_*`` token as it appears in the templates' shell blocks.
_IN_BOUNDARY_TOKEN_RE = re.compile(r"\bIN_BOUND" + r"ARY_[A-Z0-9_]+\b")

#: ``VAR=http://host[:port]/path`` — used to prove the different-hosts case is
#: documented with genuinely DIFFERENT hosts, not merely mentioned in prose.
_ENDPOINT_ASSIGNMENT_RE = re.compile(
    r"^(?P<var>IN_BOUND" + r"ARY_(?:GENERATION|EMBEDDING)_ENDPOINT)="
    r"https?://(?P<host>[^/:\s]+)"
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def parse_dimension_rows(text: str) -> Dict[str, Tuple[int, str]]:
    """Extract ``{model: (dimensions, basis)}`` from a document's tables.

    Shared by the real gates and by the negative controls, so the controls
    exercise the same parser the gates do rather than a lookalike.
    """
    rows: Dict[str, Tuple[int, str]] = {}
    for line in text.splitlines():
        match = _ROW_RE.match(line.strip())
        if match is None:
            continue
        rows[match.group("model")] = (int(match.group("dims")), match.group("basis"))
    return rows


def compare_against_declared(rows: Dict[str, Tuple[int, str]]) -> List[str]:
    """Return one human-readable problem per row that disagrees with the code.

    The comparison every drift gate below runs. Checks the model name resolves at
    all, that the documented dimension equals what ``declared_dimension()``
    returns (the function HP-2.4's guard actually calls), and that the recorded
    basis matches — a row that quietly upgrades ``provisional`` reasoning to
    ``measured`` is a drift too.
    """
    declared_by_model = {entry.model: entry for entry in MODEL_DIMENSIONS.values()}
    problems: List[str] = []
    for model, (dims, basis) in sorted(rows.items()):
        entry = declared_by_model.get(model)
        if entry is None:
            problems.append(
                f"{model!r}: documented but NOT declared in MODEL_DIMENSIONS "
                f"(a template must never offer a model the platform does not declare)"
            )
            continue
        if dims != entry.dimensions:
            problems.append(
                f"{model!r}: document says {dims}, MODEL_DIMENSIONS declares "
                f"{entry.dimensions}"
            )
        if declared_dimension(model) != dims:
            problems.append(
                f"{model!r}: document says {dims}, declared_dimension() — the "
                f"function HP-2.4 reads — returns {declared_dimension(model)}"
            )
        if basis != entry.basis:
            problems.append(
                f"{model!r}: document records basis {basis!r}, MODEL_DIMENSIONS "
                f"records {entry.basis!r}"
            )
    return problems


def declared_model_names() -> set:
    """Every distinct model name declared in the code table."""
    return {entry.model for entry in MODEL_DIMENSIONS.values()}


def in_boundary_config_keys() -> set:
    """The real ``IN_BOUNDARY_*`` variable names, read from the config module.

    Resolved from the module rather than restated here: a hardcoded list would be
    a second source of truth for the very thing this file exists to stop.
    """
    return {
        value
        for name, value in vars(in_boundary_config).items()
        if name.startswith("CONFIG_KEY_") and isinstance(value, str)
    }


# ==========================================================================
# AC6 — both templates exist and are substantive
# ==========================================================================


class TestBothTemplatesExist:
    """AC6: ``deployment/`` contains an Ollama template and a vLLM template."""

    @pytest.mark.parametrize("server", sorted(TEMPLATES))
    def test_template_file_exists(self, server: str) -> None:
        path = TEMPLATES[server]
        assert path.is_file(), (
            f"HP-2 T7 AC6 requires a {server} on-prem configuration template at "
            f"{path.relative_to(REPO_ROOT)}"
        )

    @pytest.mark.parametrize("server", sorted(TEMPLATES))
    def test_template_is_not_a_stub(self, server: str) -> None:
        """Guard against a vacuous pass: a placeholder file satisfies nothing."""
        text = _read(TEMPLATES[server])
        assert len(text) > 4000, (
            f"{TEMPLATES[server].name} is {len(text)} chars — too short to be a "
            "usable configuration template"
        )

    def test_the_two_templates_reference_each_other(self) -> None:
        """Each names the other, so an operator on the wrong page finds the right
        one instead of concluding their server is unsupported."""
        assert VLLM_TEMPLATE.name in _read(OLLAMA_TEMPLATE)
        assert OLLAMA_TEMPLATE.name in _read(VLLM_TEMPLATE)


# ==========================================================================
# Sub-AC 1 — each template covers generation AND embeddings
# ==========================================================================


class TestEachTemplateCoversBothRoles:
    """Sub-AC 1. The two roles resolve independently, so a template covering one
    describes a deployment that is half-configured."""

    @pytest.mark.parametrize("server", sorted(TEMPLATES))
    def test_both_provider_roles_are_selected(self, server: str) -> None:
        text = _read(TEMPLATES[server])
        for assignment in (
            "MODEL_GENERATION_PROVIDER=in_boundary",
            "MODEL_EMBEDDING_PROVIDER=in_boundary",
        ):
            assert assignment in text, (
                f"{TEMPLATES[server].name} must show {assignment} — the two roles "
                "resolve independently and both must be set explicitly"
            )

    @pytest.mark.parametrize("server", sorted(TEMPLATES))
    def test_both_role_model_names_are_configured(self, server: str) -> None:
        text = _read(TEMPLATES[server])
        for key in (
            in_boundary_config.CONFIG_KEY_GENERATION_MODEL,
            in_boundary_config.CONFIG_KEY_EMBEDDING_MODEL,
        ):
            assert key in text, f"{TEMPLATES[server].name} must configure {key}"

    @pytest.mark.parametrize("server", sorted(TEMPLATES))
    def test_the_template_sets_the_profile_that_makes_the_rules_apply(
        self, server: str
    ) -> None:
        """HP-2.1/2.2: under ``customer_hosted`` there is no provider default, so a
        template that omits the profile or either provider variable documents a
        deployment that refuses to start.

        This is a DOCUMENT assertion — it checks the template says so, not that a
        process booted. That the configuration genuinely boots is verified out of
        band against a stub server and tabulated in each template's validation
        status section; naming this test after a boot it never performs would be
        the same overstatement the templates are careful to avoid.
        """
        text = _read(TEMPLATES[server])
        assert "DEPLOYMENT_PROFILE=customer_hosted" in text, (
            f"{TEMPLATES[server].name} must set DEPLOYMENT_PROFILE=customer_hosted "
            "— that is what makes the boundary rules apply"
        )


# ==========================================================================
# Sub-AC 2 — the different-hosts case, with genuinely different hosts
# ==========================================================================


class TestDifferentHostsIsCovered:
    """Sub-AC 2. Generation and embeddings served from DIFFERENT hosts is the
    real on-prem topology (a GPU box for generation, a cheap box next to the
    database for embeddings), and it is the case the per-role endpoint overrides
    exist for."""

    @pytest.mark.parametrize("server", sorted(TEMPLATES))
    def test_both_per_role_endpoint_overrides_are_documented(self, server: str) -> None:
        text = _read(TEMPLATES[server])
        for key in (
            in_boundary_config.CONFIG_KEY_GENERATION_ENDPOINT,
            in_boundary_config.CONFIG_KEY_EMBEDDING_ENDPOINT,
        ):
            assert key in text, (
                f"{TEMPLATES[server].name} must document {key} — the per-role "
                "override is the only way to serve the two roles from different hosts"
            )

    @pytest.mark.parametrize("server", sorted(TEMPLATES))
    def test_a_worked_example_uses_two_distinct_hosts(self, server: str) -> None:
        """Mentioning the variables is not documenting the case. At least one
        worked example must point the two roles at DIFFERENT hosts."""
        generation_hosts: set = set()
        embedding_hosts: set = set()
        for line in _read(TEMPLATES[server]).splitlines():
            match = _ENDPOINT_ASSIGNMENT_RE.match(line.strip())
            if match is None:
                continue
            if match.group("var").endswith("GENERATION_ENDPOINT"):
                generation_hosts.add(match.group("host"))
            else:
                embedding_hosts.add(match.group("host"))

        assert generation_hosts and embedding_hosts, (
            f"{TEMPLATES[server].name} has no worked per-role endpoint assignments "
            f"(generation={sorted(generation_hosts)}, embedding={sorted(embedding_hosts)})"
        )
        distinct = {
            (gen, emb)
            for gen in generation_hosts
            for emb in embedding_hosts
            if gen != emb
        }
        assert distinct, (
            f"{TEMPLATES[server].name} assigns both role endpoints but never to two "
            f"different hosts (generation={sorted(generation_hosts)}, "
            f"embedding={sorted(embedding_hosts)}) — sub-AC 2 requires the "
            "different-hosts case be covered with a worked example"
        )


# ==========================================================================
# Sub-AC 4 / 5 — the dimension table cannot drift from the code
# ==========================================================================


class TestDimensionTableCannotDrift:
    """Sub-AC 5, the load-bearing gate. ``MODEL_DIMENSIONS`` is the single source
    HP-2.4's check reads; these templates restate it for a human, and the two are
    pinned together in both directions."""

    @pytest.mark.parametrize("server", sorted(DIMENSION_DOCUMENTS))
    def test_the_parser_finds_a_table_at_all(self, server: str) -> None:
        """Non-vacuity: a comparison over zero rows would pass silently."""
        rows = parse_dimension_rows(_read(DIMENSION_DOCUMENTS[server]))
        assert len(rows) >= len(STORY_REQUIRED_MODELS), (
            f"{DIMENSION_DOCUMENTS[server].name}: parsed only {len(rows)} dimension rows "
            f"({sorted(rows)}) — either the table is missing or its formatting no "
            "longer matches the parser, which would make this gate vacuous"
        )

    @pytest.mark.parametrize("server", sorted(DIMENSION_DOCUMENTS))
    def test_every_documented_row_matches_the_code(self, server: str) -> None:
        problems = compare_against_declared(parse_dimension_rows(_read(DIMENSION_DOCUMENTS[server])))
        assert not problems, (
            f"{DIMENSION_DOCUMENTS[server].name} has drifted from "
            "app/retrieval/embedding_dimensions.MODEL_DIMENSIONS, the single source "
            "HP-2.4's startup check reads. Fix the document (or the table, if the "
            "document is right):\n  " + "\n  ".join(problems)
        )

    @pytest.mark.parametrize("server", sorted(DIMENSION_DOCUMENTS))
    def test_every_declared_model_is_documented(self, server: str) -> None:
        """The other direction: adding a model to the code without documenting it
        fails the build. Without this, the templates could silently fall behind."""
        documented = set(parse_dimension_rows(_read(DIMENSION_DOCUMENTS[server])))
        missing = declared_model_names() - documented
        assert not missing, (
            f"{DIMENSION_DOCUMENTS[server].name} does not document these declared models: "
            f"{sorted(missing)}. A model the platform declares but no template "
            "mentions is a model an operator cannot discover."
        )

    @pytest.mark.parametrize("server", sorted(DIMENSION_DOCUMENTS))
    def test_no_extra_models_are_invented(self, server: str) -> None:
        documented = set(parse_dimension_rows(_read(DIMENSION_DOCUMENTS[server])))
        extra = documented - declared_model_names()
        assert not extra, (
            f"{DIMENSION_DOCUMENTS[server].name} documents models the code does not declare: "
            f"{sorted(extra)}"
        )

    @pytest.mark.parametrize("server", sorted(DIMENSION_DOCUMENTS))
    def test_the_four_story_named_models_are_present_with_the_right_dimensions(
        self, server: str
    ) -> None:
        """Sub-AC 4 names four models and their dimensions explicitly."""
        rows = parse_dimension_rows(_read(DIMENSION_DOCUMENTS[server]))
        for model, dims in sorted(STORY_REQUIRED_MODELS.items()):
            assert model in rows, (
                f"{DIMENSION_DOCUMENTS[server].name} must list {model!r} — sub-AC 4 names it"
            )
            assert rows[model][0] == dims, (
                f"{DIMENSION_DOCUMENTS[server].name} lists {model!r} as {rows[model][0]}; "
                f"sub-AC 4 states {dims}"
            )

    def test_the_four_story_named_models_are_declared_in_code(self) -> None:
        """And the code agrees, read through HP-2.4's own accessor."""
        for model, dims in sorted(STORY_REQUIRED_MODELS.items()):
            assert declared_dimension(model) == dims, (
                f"declared_dimension({model!r}) is {declared_dimension(model)}, "
                f"sub-AC 4 states {dims}"
            )

    @pytest.mark.parametrize("server", sorted(TEMPLATES))
    def test_the_no_migration_claim_is_explicit(self, server: str) -> None:
        """Sub-AC 4 requires this STATED, not left to be inferred from the
        dimensionless column."""
        text = _read(TEMPLATES[server])
        assert NO_MIGRATION_CLAIM in text, (
            f"{TEMPLATES[server].name} must state explicitly that every listed "
            f"model works today with {NO_MIGRATION_CLAIM!r} (sub-AC 4)"
        )

    @pytest.mark.parametrize("server", sorted(TEMPLATES))
    def test_the_template_names_the_code_table_as_the_single_source(
        self, server: str
    ) -> None:
        """A reader must be able to find the authority, or the next person to add
        a model will edit the prose and stop there."""
        text = _read(TEMPLATES[server])
        assert "embedding_dimensions.py" in text, (
            f"{TEMPLATES[server].name} must name "
            "backend/app/retrieval/embedding_dimensions.py as the single source"
        )
        assert Path(__file__).name in text, (
            f"{TEMPLATES[server].name} must name this drift gate "
            f"({Path(__file__).name}) so a reader knows the pinning is enforced"
        )


# ==========================================================================
# The templates must not name settings or limits the code does not have
# ==========================================================================


class TestNoInventedConfiguration:
    """A template that names a variable the code never reads sends an operator to
    configure something that will not take effect — and they have no way to tell."""

    @pytest.mark.parametrize("server", sorted(TEMPLATES))
    def test_every_in_boundary_variable_named_is_real(self, server: str) -> None:
        real = in_boundary_config_keys()
        assert real, "failed to resolve any config keys from in_boundary_config"
        named = set(_IN_BOUNDARY_TOKEN_RE.findall(_read(TEMPLATES[server])))
        assert named, f"{TEMPLATES[server].name} names no in-boundary variable at all"
        invented = named - real
        assert not invented, (
            f"{TEMPLATES[server].name} names in-boundary variables the config module "
            f"does not define: {sorted(invented)}. Real keys: {sorted(real)}"
        )

    @pytest.mark.parametrize("server", sorted(TEMPLATES))
    def test_every_real_in_boundary_variable_is_documented(self, server: str) -> None:
        """The other direction — a settable variable no template mentions is a
        lever an on-prem operator cannot find."""
        missing = in_boundary_config_keys() - set(
            _IN_BOUNDARY_TOKEN_RE.findall(_read(TEMPLATES[server]))
        )
        assert not missing, (
            f"{TEMPLATES[server].name} does not mention these real in-boundary "
            f"config variables: {sorted(missing)}"
        )

    @pytest.mark.parametrize("server", sorted(TEMPLATES))
    def test_the_probe_timeout_variable_name_is_correct(self, server: str) -> None:
        from app.model_gateway import probe

        assert probe.ENV_PROBE_TIMEOUT in _read(TEMPLATES[server]), (
            f"{TEMPLATES[server].name} must name {probe.ENV_PROBE_TIMEOUT} — an "
            "operator whose model server starts after the app will hit this"
        )

    @pytest.mark.parametrize("server", sorted(TEMPLATES))
    def test_the_chunk_size_arithmetic_matches_the_chunker(self, server: str) -> None:
        """Both templates reason about the context window a model needs from the
        platform's maximum chunk size. If the chunker changes, that reasoning is
        wrong and an operator sizes their model server against a stale number."""
        text = _read(TEMPLATES[server])
        expected = f"MAX_CHUNK_CHARS = {chunking.MAX_CHUNK_CHARS}"
        assert expected in text, (
            f"{TEMPLATES[server].name} must state {expected!r} — it currently "
            "disagrees with app/retrieval/chunking.py, so its context-window "
            "guidance is derived from a stale number"
        )

    @pytest.mark.parametrize("server", sorted(TEMPLATES))
    def test_the_documented_batch_size_matches_the_code_default(
        self, server: str
    ) -> None:
        """The templates tell an operator their server must accept a batch of this
        size. A stale number here is the difference between a working deployment
        and one that indexes nothing."""
        from app.retrieval import embedder

        text = _read(TEMPLATES[server])
        assert "RETRIEVAL_EMBED_BATCH_SIZE" in text, (
            f"{TEMPLATES[server].name} must name RETRIEVAL_EMBED_BATCH_SIZE — it is "
            "the lever for a server that cannot batch"
        )
        assert f"default {embedder._DEFAULT_BATCH_SIZE}" in text, (
            f"{TEMPLATES[server].name} must state the real default batch size "
            f"({embedder._DEFAULT_BATCH_SIZE}); a stale value here misleads an "
            "operator sizing their server"
        )

    @pytest.mark.parametrize("server", sorted(TEMPLATES))
    def test_the_optional_credential_posture_is_stated(self, server: str) -> None:
        """A self-hosted model server is commonly unauthenticated, and the adapter
        declares the credential NOT required for exactly that reason. A template
        that implied a key was mandatory would send an operator hunting for one."""
        text = _read(TEMPLATES[server])
        assert "credential_required=False" in text, (
            f"{TEMPLATES[server].name} must state that the endpoint credential is "
            "optional for a self-hosted server (credential_required=False)"
        )

    def test_the_adapter_really_declares_the_credential_optional(self) -> None:
        """And that claim is true of the code, not just of the document."""
        from app.model_gateway import get_embedding_provider
        from app.model_gateway._interface import ROLE_EMBEDDING

        provider = get_embedding_provider.__globals__["_PROVIDER_REGISTRY"][
            in_boundary_config.IN_BOUNDARY_PROVIDER_NAME
        ]
        target = provider.probe_target(ROLE_EMBEDDING)
        assert target.credential_required is False, (
            "the templates state the endpoint credential is optional for a "
            "self-hosted server; the adapter no longer agrees"
        )


# ==========================================================================
# Sub-AC 3 — the validation claim must stay honest
# ==========================================================================


class TestValidationHonesty:
    """Sub-AC 3 asks that each template be exercised end to end with the versions
    recorded. It was exercised through the real gateway adapter against a local
    stub; no Ollama or vLLM was installed. That is recorded in the documents as a
    split between verified and unverified, and this class stops the split being
    quietly deleted — which is the only way the claim could become dishonest
    without anyone editing a sentence.
    """

    @pytest.mark.parametrize("server", sorted(TEMPLATES))
    def test_a_validation_status_section_exists(self, server: str) -> None:
        text = _read(TEMPLATES[server])
        assert "## Validation status" in text, (
            f"{TEMPLATES[server].name} must carry a Validation status section — a "
            "template that does not say what was proven overstates itself by default"
        )

    @pytest.mark.parametrize("server", sorted(TEMPLATES))
    def test_the_unverified_half_is_still_declared(self, server: str) -> None:
        text = _read(TEMPLATES[server])
        assert "NOT verified" in text, (
            f"{TEMPLATES[server].name} must keep its 'NOT verified' section. If a "
            "real-server run has since happened, move the items into the verified "
            "table and record the versions — do not delete the section."
        )

    @pytest.mark.parametrize("server", sorted(TEMPLATES))
    def test_a_version_record_table_exists_for_the_operator(self, server: str) -> None:
        """Sub-AC 3's 'versions run against' — the row a real-server run fills in."""
        text = _read(TEMPLATES[server])
        assert "Version you ran" in text, (
            f"{TEMPLATES[server].name} must carry the blank version-record table so "
            "a real-server validation has somewhere to record what it ran"
        )

    @pytest.mark.parametrize("server", sorted(TEMPLATES))
    def test_a_verify_on_your_deployment_checklist_exists(self, server: str) -> None:
        text = _read(TEMPLATES[server])
        assert "## Verify on your deployment" in text, (
            f"{TEMPLATES[server].name} must carry the operator verification "
            "checklist — it is what converts the unverified half into a verified one"
        )
        assert text.count("- [ ]") >= 8, (
            f"{TEMPLATES[server].name}: the checklist has only "
            f"{text.count('- [ ]')} items; too few to cover the unverified surface"
        )

    @pytest.mark.parametrize("server", sorted(TEMPLATES))
    def test_the_batch_response_failure_mode_is_documented(self, server: str) -> None:
        """The highest-risk observed failure: a server that returns fewer vectors
        than inputs makes the adapter discard the whole batch, so NOTHING is
        indexed while the run still completes and reports normally. A template
        that omits it leaves an operator with a silently empty index."""
        text = _read(TEMPLATES[server])
        assert "count mismatch" in text, (
            f"{TEMPLATES[server].name} must document the embedding count-mismatch "
            "failure mode — it is silent in the data and only visible in the log"
        )


# ==========================================================================
# The gates are proven to reject
# ==========================================================================


class TestTheGateGoesRedWhenItShould:
    """A gate never observed failing is not known to be a gate.

    Each control drives the SAME parser and comparison the real gates use over a
    deliberately-corrupted synthetic table, so a regression that neutered the
    parser would fail here even while every green test above still passed.
    """

    def test_a_wrong_dimension_is_rejected(self) -> None:
        corrupted = (
            "| Model | Dimensions | Basis |\n"
            "|---|---|---|\n"
            f"| `nomic-embed-text` | 999 | {BASIS_PUBLISHED} |\n"
        )
        rows = parse_dimension_rows(corrupted)
        assert rows == {"nomic-embed-text": (999, BASIS_PUBLISHED)}
        problems = compare_against_declared(rows)
        assert problems, "a wrong dimension must be rejected"
        assert any("999" in p and "768" in p for p in problems), problems

    def test_a_wrong_basis_is_rejected(self) -> None:
        corrupted = f"| `nomic-embed-text` | 768 | {BASIS_MEASURED} |\n"
        problems = compare_against_declared(parse_dimension_rows(corrupted))
        assert problems, "a basis that disagrees with the code must be rejected"
        assert any("basis" in p for p in problems), problems

    def test_an_undeclared_model_is_rejected(self) -> None:
        corrupted = f"| `totally-made-up-embedder` | 512 | {BASIS_PUBLISHED} |\n"
        problems = compare_against_declared(parse_dimension_rows(corrupted))
        assert problems, "a model the code does not declare must be rejected"
        assert any("NOT declared" in p for p in problems), problems

    def test_a_missing_model_is_rejected(self) -> None:
        """The other direction, driven over a table that omits everything but one
        row — the shape a template takes when the code gains a model nobody
        documented."""
        rows = parse_dimension_rows(f"| `nomic-embed-text` | 768 | {BASIS_PUBLISHED} |\n")
        missing = declared_model_names() - set(rows)
        assert missing, "omitting declared models must be detectable"
        assert "mxbai-embed-large" in missing

    def test_an_unchanged_table_is_not_flagged(self) -> None:
        """No false positives: the real templates' own rows pass the comparison.
        Without this, an always-failing parser would satisfy every control above."""
        for path in TEMPLATES.values():
            rows = parse_dimension_rows(_read(path))
            assert rows, f"{path.name}: parser found nothing"
            assert compare_against_declared(rows) == [], path.name

    def test_the_row_parser_ignores_the_documents_other_tables(self) -> None:
        """The templates contain several other three-column tables. If the parser
        picked those up, the gate would fail for reasons unrelated to drift — and
        a maintainer would loosen it. Prove the discrimination directly."""
        not_a_dimension_row = (
            "| `nomic-embed-text` | 8192 tokens | yes, with wide margin |\n"
            "| `mxbai-embed-large` | 512 tokens | prose yes; code/JSON at risk |\n"
            "| `nomic-embed-text:latest` | `nomic-embed-text` | 768 | **runs** |\n"
        )
        assert parse_dimension_rows(not_a_dimension_row) == {}

    def test_the_different_hosts_control_rejects_a_same_host_example(self) -> None:
        """The sub-AC 2 gate must not be satisfiable by pointing both roles at one
        host. Drive its own matcher over a same-host block."""
        same_host = (
            "IN_BOUND" + "ARY_GENERATION_ENDPOINT=http://one.internal:8000/v1/gen\n"
            "IN_BOUND" + "ARY_EMBEDDING_ENDPOINT=http://one.internal:8000/v1/emb\n"
        )
        generation_hosts, embedding_hosts = set(), set()
        for line in same_host.splitlines():
            match = _ENDPOINT_ASSIGNMENT_RE.match(line.strip())
            assert match is not None, line
            if match.group("var").endswith("GENERATION_ENDPOINT"):
                generation_hosts.add(match.group("host"))
            else:
                embedding_hosts.add(match.group("host"))
        assert not {
            (g, e) for g in generation_hosts for e in embedding_hosts if g != e
        }, "a same-host example must NOT satisfy the different-hosts gate"
