"""HP-2.8 — integration-style regression coverage for the HP-2 boundary rules.

Why this file exists, given HP-2.1-2.7 already ship 228 tests
-------------------------------------------------------------
Everything already covered is covered at the FUNCTION level: the tests call
``validate_provider_config()`` directly, inject the dimension guard's three
collaborators, or PATCH ``provider_posture`` to hand the health shaper a posture.
That is the right way to pin the rules, and it leaves two holes that only an
integration test can close.

**The startup path itself is never exercised.** The nearest thing,
``test_hp2_no_cloud_default.test_lifespan_still_calls_validate_provider_config``,
greps the lifespan's SOURCE for the call. A refactor that moved the call behind a
condition, wrapped it in a ``try/except``, or ordered it after the first request
would keep that test green while the deployment silently stopped refusing to boot.

**The probe cannot run in the contract suite at all.** ``conftest.py`` sets
``AGENTIQ_DISABLE_BACKGROUND_JOBS=1`` at import, and HP-2.3 skips probing under
that flag — deliberately, so no test reaches the network. The consequence is that
every posture in the suite is either ``unknown`` or mocked, so the *reachability*
behaviour at the heart of HP-2.3/2.5 has no in-suite coverage whatsoever.

Both holes are closed the same way: boot the REAL app through its REAL lifespan in
a SUBPROCESS, with the environment constructed per scenario and the probe enabled.
A subprocess is not incidental — the flag above is process-global and read at
import, and the app package loads ``.env`` on import, so an in-process attempt
would either inherit the suite's probe-disabling flag or let a real deployment
value stand in for one under test.

Hermetic by construction
------------------------
Nothing here reaches the network. Every endpoint is ``127.0.0.1``: port 1 for
"unreachable" (reliably closed) and a real listening socket this module owns for
"reachable". Where reachability is not the behaviour under test the probe is
switched off with ``MODEL_PROVIDER_PROBE_TIMEOUT_SECONDS=0``, which makes the
result independent of whether CI has egress — a test whose verdict depends on
reaching a vendor API is a test that fails for reasons unrelated to the code.

That the probe does a bare TCP connect and nothing more (no HTTP request, no model
call) is what makes a plain listening socket a faithful stand-in for a reachable
model server.
"""
from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

BACKEND_ROOT: Path = Path(__file__).resolve().parents[2]

#: Written to a temp file and run by each subprocess. Reports a JSON verdict on
#: stdout rather than relying on the exit code, so a refusal and a crash are
#: distinguishable and the assertion can name the exception it expected.
_BOOT_SCRIPT = r'''
import json, sys, traceback
verdict = {}
try:
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        verdict["booted"] = True
        resp = client.get("/api/health")
        verdict["http_status"] = resp.status_code
        body = resp.json()
        verdict["health_status"] = body.get("status")
        verdict["health_ok"] = body.get("ok")
        verdict["model_providers"] = (body.get("checks") or {}).get("model_providers")
        verdict["raw_body"] = resp.text
except BaseException as exc:               # noqa: BLE001 - a refusal IS the result
    verdict["booted"] = False
    verdict["error_type"] = type(exc).__name__
    verdict["error"] = str(exc)
    # The traceback is what assertions match on: a lifespan exception can be
    # wrapped (ExceptionGroup, starlette re-raise), so the class name is reliably
    # found here even when it is not the outermost type.
    verdict["traceback"] = traceback.format_exc()
sys.stdout.write("@@VERDICT@@" + json.dumps(verdict))
'''

_UNREACHABLE = "http://127.0.0.1:1"          # port 1: reliably closed
_PROBE_OFF = {"MODEL_PROVIDER_PROBE_TIMEOUT_SECONDS": "0"}
_PROBE_ON = {"MODEL_PROVIDER_PROBE_TIMEOUT_SECONDS": "3"}

#: A model whose dimension the platform DECLARES, so the guard has something to
#: compare against. Resolved through the code rather than restated as a number.
_DECLARED_MODEL = "nomic-embed-text"


# ---------------------------------------------------------------------------
# A real listening socket, standing in for a reachable model server
# ---------------------------------------------------------------------------


class _Listener:
    """Accepts and immediately closes connections, on a port nobody else owns."""

    def __init__(self) -> None:
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(16)
        self.port = self._srv.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._srv.accept()
                conn.close()
            except OSError:
                return

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        try:
            self._srv.close()
        except OSError:
            pass


_LISTENER: Optional[_Listener] = None


def _listener() -> _Listener:
    global _LISTENER
    if _LISTENER is None:
        _LISTENER = _Listener()
    return _LISTENER


# ---------------------------------------------------------------------------
# Booting the real app
# ---------------------------------------------------------------------------

_SCRIPT_PATH: Optional[Path] = None
_BOOT_CACHE: Dict[tuple, Dict[str, Any]] = {}


def _script() -> Path:
    global _SCRIPT_PATH
    if _SCRIPT_PATH is None:
        import tempfile

        handle = tempfile.NamedTemporaryFile(
            "w", suffix="_hp28_boot.py", delete=False, encoding="utf-8"
        )
        handle.write(_BOOT_SCRIPT)
        handle.close()
        _SCRIPT_PATH = Path(handle.name)
    return _SCRIPT_PATH


def _subprocess_env(overrides: Dict[str, Optional[str]]) -> Dict[str, str]:
    """A deliberately narrow environment: only what the scenario states.

    Every gateway variable is stripped first, so a value from the developer's
    ``.env`` or the surrounding suite can never stand in for one under test.
    ``AGENTIQ_DISABLE_BACKGROUND_JOBS`` is removed because the suite sets it and it
    disables the probe — leaving it would make every reachability assertion here
    vacuous, which is the exact hole this file exists to close.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(
            ("MODEL_", "IN_BOUNDARY_", "CUSTOMER_TENANT_", "DEPLOYMENT_")
        )
    }
    # conftest points DATABASE_URL at the migrated, disposable test database.
    env["DATABASE_URL"] = os.environ["DATABASE_URL"]
    env["PYTHONPATH"] = str(BACKEND_ROOT)
    env["INGEST_MODE"] = "offline"
    env.pop("AGENTIQ_DISABLE_BACKGROUND_JOBS", None)
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def _boot(_state: str = "", **overrides: Optional[str]) -> Dict[str, Any]:
    """Boot the real app once per distinct environment and cache the verdict.

    ``_state`` names any DATABASE state the scenario depends on. It is part of the
    cache key but never part of the environment: two scenarios here use an
    IDENTICAL configuration and differ only in whether a conflicting vector is
    stored, so keying on the environment alone would serve one the other's cached
    verdict and both would appear to pass whatever the code did.
    """
    key = (_state,) + tuple(sorted((k, str(v)) for k, v in overrides.items()))
    if key in _BOOT_CACHE:
        return _BOOT_CACHE[key]

    proc = subprocess.run(
        [sys.executable, str(_script())],
        cwd=str(BACKEND_ROOT),
        env=_subprocess_env(overrides),
        capture_output=True,
        text=True,
        timeout=180,
    )
    marker = "@@VERDICT@@"
    if marker not in proc.stdout:
        raise AssertionError(
            "the boot subprocess reported no verdict — it died before it could.\n"
            f"overrides={overrides}\nrc={proc.returncode}\n"
            f"stdout tail:\n{proc.stdout[-1500:]}\nstderr tail:\n{proc.stderr[-2500:]}"
        )
    verdict = json.loads(proc.stdout.split(marker, 1)[1])
    _BOOT_CACHE[key] = verdict
    return verdict


def _refused_with(verdict: Dict[str, Any], exception_name: str) -> bool:
    return verdict.get("booted") is False and exception_name in (
        verdict.get("traceback") or ""
    )


def _roles(verdict: Dict[str, Any]) -> Dict[str, Any]:
    return ((verdict.get("model_providers") or {}).get("roles")) or {}


# ===========================================================================
# Scenario 1 — customer-hosted refuses when the provider variables are unset
# ===========================================================================


class TestCustomerHostedRefusesUnsetProviders:
    """HP-2.2 through the real lifespan, not through a direct function call."""

    @pytest.fixture(scope="class")
    def verdict(self) -> Dict[str, Any]:
        return _boot(DEPLOYMENT_PROFILE="customer_hosted", **_PROBE_OFF)

    def test_startup_is_refused(self, verdict: Dict[str, Any]) -> None:
        assert _refused_with(verdict, "MissingProviderConfiguration"), (
            "a customer-hosted deployment with no provider configured BOOTED. "
            "HP-2.2's whole purpose is that a call leaving the customer's boundary "
            f"is never inherited from a default. verdict={verdict}"
        )

    def test_the_refusal_names_both_variables(self, verdict: Dict[str, Any]) -> None:
        message = verdict.get("traceback") or ""
        for variable in ("MODEL_GENERATION_PROVIDER", "MODEL_EMBEDDING_PROVIDER"):
            assert variable in message, (
                f"the refusal does not name {variable}; an operator cannot act on it"
            )

    def test_the_refusal_names_every_valid_value(self, verdict: Dict[str, Any]) -> None:
        message = verdict.get("traceback") or ""
        for value in ("hosted", "in_boundary", "customer_tenant"):
            assert value in message, f"the refusal does not offer {value!r}"

    def test_no_http_surface_is_served(self, verdict: Dict[str, Any]) -> None:
        """A refusal must stop the process, not degrade to a running app."""
        assert "health_status" not in verdict, (
            "the app answered a request despite refusing startup — the refusal has "
            "to happen during the lifespan, before anything is served"
        )

    def test_one_configured_role_is_still_refused(self) -> None:
        """Both roles resolve independently, so half a configuration is not one."""
        verdict = _boot(
            DEPLOYMENT_PROFILE="customer_hosted",
            MODEL_GENERATION_PROVIDER="hosted",
            **_PROBE_OFF,
        )
        assert _refused_with(verdict, "MissingProviderConfiguration"), verdict


# ===========================================================================
# Scenario 2 — SaaS is byte-for-byte backward compatible
# ===========================================================================


class TestSaasBackwardCompatibility:
    """The deployments that exist today must be unaffected by all of HP-2."""

    @pytest.fixture(scope="class")
    def verdict(self) -> Dict[str, Any]:
        return _boot(DEPLOYMENT_PROFILE="saas", **_PROBE_OFF)

    def test_startup_succeeds_with_nothing_configured(
        self, verdict: Dict[str, Any]
    ) -> None:
        assert verdict.get("booted") is True, (
            f"a SaaS deployment with no model configuration failed to start: {verdict}"
        )

    def test_both_roles_still_default_to_the_hosted_provider(
        self, verdict: Dict[str, Any]
    ) -> None:
        roles = _roles(verdict)
        assert set(roles) == {"generation", "embedding"}, roles
        for role, entry in roles.items():
            assert entry["provider"] == "hosted", (
                f"{role} resolved to {entry['provider']!r} under saas; the "
                "backward-compatible default is 'hosted'"
            )

    def test_health_still_reports_ok(self, verdict: Dict[str, Any]) -> None:
        assert verdict["http_status"] == 200
        assert verdict["health_ok"] is True
        assert verdict["health_status"] == "healthy"

    def test_a_missing_credential_is_reported_but_does_not_degrade_health(
        self, verdict: Dict[str, Any]
    ) -> None:
        """The state every dev box, every CI run and every keyless SaaS install is
        in — and the one HP-2.5 most deliberately refuses to flag.

        Note what actually happens with reachability switched off: the probe still
        runs its CHEAPER checks (endpoint configuration, then credential presence),
        so ``hosted`` with no API key reports ``unavailable`` on
        ``credential_presence`` rather than ``unknown``. That is honest reporting,
        and it must NOT make the service unhealthy: LLM enrichment is optional by
        design, the deterministic fallbacks work without a key, and degrading here
        would make every such deployment cry wolf permanently.
        """
        providers = verdict["model_providers"]
        assert providers["status"] == "unavailable", providers
        for role, entry in _roles(verdict).items():
            assert entry["check"] == "credential_presence", (role, entry)
            assert entry["probed"] is False, (
                f"{role} was probed for reachability despite the probe being off"
            )
        # The whole point: reported, and still healthy.
        assert verdict["health_status"] == "healthy"
        assert verdict["health_ok"] is True

    def test_a_provider_needing_no_credential_reports_unknown_instead(self) -> None:
        """The contrast that proves the check above is about the CREDENTIAL and not
        merely about probing being off. ``in_boundary`` declares
        ``credential_required=False`` (Ollama and vLLM are commonly
        unauthenticated), so with reachability skipped nothing failed and the
        posture is genuinely unmeasured."""
        verdict = _boot(
            DEPLOYMENT_PROFILE="saas",
            **_in_boundary(_UNREACHABLE),
            **_PROBE_OFF,
        )
        assert verdict["model_providers"]["status"] == "unknown", verdict[
            "model_providers"
        ]
        assert verdict["health_status"] == "healthy"

    def test_the_profile_may_be_left_unset_entirely(self) -> None:
        """An existing deployment sets no profile at all; that must mean saas."""
        verdict = _boot(DEPLOYMENT_PROFILE=None, **_PROBE_OFF)
        assert verdict.get("booted") is True, verdict
        for entry in _roles(verdict).values():
            assert entry["provider"] == "hosted"

    def test_an_invalid_profile_refuses_rather_than_assuming_saas(self) -> None:
        """HP-2.1: a typo must not silently reinstate the cloud-calling default."""
        verdict = _boot(DEPLOYMENT_PROFILE="on_prem", **_PROBE_OFF)
        assert _refused_with(verdict, "InvalidDeploymentProfile"), verdict


# ===========================================================================
# Scenario 3 / 4 — an unreachable provider, in both profiles
# ===========================================================================


def _in_boundary(base_url: str) -> Dict[str, str]:
    return {
        "MODEL_GENERATION_PROVIDER": "in_boundary",
        "MODEL_EMBEDDING_PROVIDER": "in_boundary",
        "IN_BOUNDARY_BASE_URL": base_url,
        "IN_BOUNDARY_GENERATION_MODEL": "a-generation-model",
        "IN_BOUNDARY_EMBEDDING_MODEL": _DECLARED_MODEL,
    }


class TestUnreachableProviderUnderCustomerHosted:
    """HP-2.3: with no fallback available, an unreachable provider is fatal."""

    @pytest.fixture(scope="class")
    def verdict(self) -> Dict[str, Any]:
        return _boot(
            DEPLOYMENT_PROFILE="customer_hosted",
            **_in_boundary(_UNREACHABLE),
            **_PROBE_ON,
        )

    def test_startup_is_refused(self, verdict: Dict[str, Any]) -> None:
        assert _refused_with(verdict, "ProviderUnreachable"), (
            "a customer-hosted deployment booted with its configured model "
            f"provider unreachable. verdict={verdict}"
        )

    def test_the_refusal_names_the_host_and_port(self, verdict: Dict[str, Any]) -> None:
        message = verdict.get("traceback") or ""
        assert "127.0.0.1" in message and ":1" in message, message[-600:]

    def test_the_refusal_names_the_escape_hatch(self, verdict: Dict[str, Any]) -> None:
        """A reachability refusal — and only that — offers the way past itself."""
        assert "MODEL_PROVIDER_PROBE_TIMEOUT_SECONDS" in (
            verdict.get("traceback") or ""
        )

    def test_the_escape_hatch_actually_works(self) -> None:
        """Documented remedies that do not work are worse than none."""
        verdict = _boot(
            DEPLOYMENT_PROFILE="customer_hosted",
            **_in_boundary(_UNREACHABLE),
            **_PROBE_OFF,
        )
        assert verdict.get("booted") is True, verdict
        assert verdict["model_providers"]["status"] == "unknown"


class TestUnreachableProviderUnderSaas:
    """The same condition must NOT stop a SaaS deployment serving — but it must
    be reported. A transient vendor blip is not a reason to refuse traffic."""

    @pytest.fixture(scope="class")
    def verdict(self) -> Dict[str, Any]:
        return _boot(
            DEPLOYMENT_PROFILE="saas", **_in_boundary(_UNREACHABLE), **_PROBE_ON
        )

    def test_startup_succeeds(self, verdict: Dict[str, Any]) -> None:
        assert verdict.get("booted") is True, verdict

    def test_health_reports_unhealthy(self, verdict: Dict[str, Any]) -> None:
        """Story AC3 end to end: the surface an operator watches tells the truth."""
        assert verdict["health_status"] == "unhealthy", verdict["model_providers"]
        assert verdict["health_ok"] is False

    def test_the_endpoint_still_answers_200(self, verdict: Dict[str, Any]) -> None:
        """Unhealthy is a payload, not a transport failure — a monitor has to be
        able to read the reason rather than see a dead endpoint."""
        assert verdict["http_status"] == 200

    def test_both_roles_are_reported_unavailable_on_reachability(
        self, verdict: Dict[str, Any]
    ) -> None:
        roles = _roles(verdict)
        assert set(roles) == {"generation", "embedding"}, roles
        for role, entry in roles.items():
            assert entry["status"] == "unavailable", (role, entry)
            assert entry["check"] == "reachability", (role, entry)
            assert entry["probed"] is True, (role, entry)

    def test_no_endpoint_host_leaks_to_the_public_payload(
        self, verdict: Dict[str, Any]
    ) -> None:
        """`/api/health` needs no credential, so internal topology must not be on
        it. Checked on the real serialised response, not on a shaped dict."""
        body = verdict["raw_body"]
        assert "127.0.0.1" not in body, body
        assert "endpointHost" not in body
        for entry in _roles(verdict).values():
            assert set(entry) == {"provider", "status", "check", "probed"}, entry


class TestAReachableProviderIsHealthy:
    """The positive control. Without it every assertion above could be passing
    because the probe reports unavailable for some unrelated reason."""

    @pytest.fixture(scope="class")
    def verdict(self) -> Dict[str, Any]:
        return _boot(
            DEPLOYMENT_PROFILE="customer_hosted",
            **_in_boundary(_listener().base_url),
            **_PROBE_ON,
        )

    def test_startup_succeeds(self, verdict: Dict[str, Any]) -> None:
        assert verdict.get("booted") is True, verdict

    def test_health_reports_healthy(self, verdict: Dict[str, Any]) -> None:
        assert verdict["health_status"] == "healthy", verdict["model_providers"]
        assert verdict["health_ok"] is True

    def test_the_probe_really_ran_and_succeeded(self, verdict: Dict[str, Any]) -> None:
        """Proves the unreachable cases above are a measurement, not a default."""
        for role, entry in _roles(verdict).items():
            assert entry["probed"] is True, (role, entry)
            assert entry["status"] == "ok", (role, entry)
            assert entry["check"] == "reachability", (role, entry)


# ===========================================================================
# Scenario 5 — an embedding-dimension conflict refuses startup
# ===========================================================================


@contextlib.contextmanager
def _seed_conflicting_vector():
    """Store one vector under the ACTIVE model stamp with the wrong dimension.

    Seeded through the real table so the guard's real SQL (``vector_dims()``)
    reads it. The active stamp is resolved from the code rather than restated, so
    a change to the identity format cannot leave this pointing at a stamp nothing
    checks — which would make the test vacuous rather than red.

    A context manager rather than a fixture on purpose: the row is removed as soon
    as the boot that needs it has run. Held any longer it would refuse every later
    boot in this database, including the non-vacuity check that must succeed, and
    the outcome would depend on test ordering.
    """
    from app import db

    saved = {
        key: os.environ.get(key)
        for key in ("MODEL_EMBEDDING_PROVIDER", "IN_BOUNDARY_EMBEDDING_MODEL")
    }
    os.environ["MODEL_EMBEDDING_PROVIDER"] = "in_boundary"
    os.environ["IN_BOUNDARY_EMBEDDING_MODEL"] = _DECLARED_MODEL
    try:
        from app.retrieval.embedder import active_embedding_model
        from app.retrieval.embedding_dimensions import declared_dimension

        identity, version = active_embedding_model()
        declared = declared_dimension(identity)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert declared, (
        f"{identity!r} has no declared dimension, so the guard would SKIP and this "
        "test would prove nothing. Pick a model listed in MODEL_DIMENSIONS."
    )
    wrong = 8
    assert wrong != declared

    chunk_id = f"hp28-{uuid.uuid4().hex[:24]}"
    literal = "[" + ",".join(["0.01"] * wrong) + "]"
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO retrieval_chunks (
                    chunk_id, org_id, content, content_hash, content_type,
                    source_system, source_artifact, chunk_position,
                    embedding, embedding_model, embedding_model_version,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, %s, 'prose', 'document', %s, 0,
                          %s::vector, %s, %s, NOW(), NOW())
                """,
                (
                    chunk_id,
                    "hp28-dimension-probe",
                    "seeded by the HP-2.8 dimension-conflict test",
                    "0" * 64,
                    f"hp28/{chunk_id}",
                    literal,
                    identity,
                    version or "",
                ),
            )
        conn.commit()
    try:
        yield {"identity": identity, "declared": declared, "stored": wrong}
    finally:
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM retrieval_chunks WHERE chunk_id = %s", (chunk_id,)
                )
            conn.commit()


def _count_seeded_conflicting_rows() -> int:
    from app import db

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM retrieval_chunks WHERE chunk_id LIKE 'hp28-%%'"
            )
            return int((cur.fetchone() or [0])[0])


class TestDimensionMismatchRefusesStartup:
    """HP-2.4 end to end: the conflict is read from real pgvector, at boot.

    Reachability is deliberately satisfied by the local listener, so the refusal
    can only be the dimension check — a boot that failed earlier in the lifespan
    would prove nothing about it.
    """

    @pytest.fixture(scope="class")
    def conflict(self):
        """Seed, boot, unseed. The row lives only for the boot that needs it."""
        with _seed_conflicting_vector() as info:
            verdict = _boot(
                _state="conflicting-vector",
                DEPLOYMENT_PROFILE="customer_hosted",
                **_in_boundary(_listener().base_url),
                **_PROBE_ON,
            )
        return verdict, info

    def test_startup_is_refused(self, conflict) -> None:
        verdict, _ = conflict
        assert _refused_with(verdict, "EmbeddingDimensionMismatch"), (
            "a deployment whose configured embedding model cannot write into its "
            f"own index BOOTED. verdict={verdict}"
        )

    def test_the_refusal_reports_both_dimensions(self, conflict) -> None:
        verdict, info = conflict
        message = verdict.get("traceback") or ""
        assert str(info["declared"]) in message, message[-800:]
        assert str(info["stored"]) in message, message[-800:]

    def test_the_refusal_names_the_model(self, conflict) -> None:
        verdict, info = conflict
        assert info["identity"] in (verdict.get("traceback") or "")

    def test_the_refusal_says_the_backfill_will_not_fix_it(self, conflict) -> None:
        """The remedy an engineer would reach for first is the wrong one: the
        backfill only re-embeds NON-active vectors, and these carry the active
        stamp. Saying so is the difference between a usable message and an hour
        watching a job change nothing."""
        verdict, _ = conflict
        assert "backfill" in (verdict.get("traceback") or "").lower()

    def test_it_refuses_under_saas_too(self) -> None:
        """Unlike reachability this is not environmental: whoever operates the
        deployment, the model provably cannot write into the index."""
        with _seed_conflicting_vector():
            verdict = _boot(
                _state="conflicting-vector",
                DEPLOYMENT_PROFILE="saas",
                **_in_boundary(_listener().base_url),
                **_PROBE_ON,
            )
        assert _refused_with(verdict, "EmbeddingDimensionMismatch"), verdict


# ===========================================================================
# Non-vacuity — the same configuration boots once the conflict is gone
# ===========================================================================


class TestTheDimensionGateIsRealNotAccidental:
    def test_the_identical_configuration_boots_with_no_conflicting_vector(self) -> None:
        """The only difference from the refusal above is the seeded row. Without
        this, that refusal could have been caused by anything in the lifespan."""
        assert _count_seeded_conflicting_rows() == 0, (
            "a seeded conflict row outlived its context manager, so this check "
            "cannot distinguish the gate from a leak"
        )
        verdict = _boot(
            _state="no-conflicting-vector",
            DEPLOYMENT_PROFILE="customer_hosted",
            **_in_boundary(_listener().base_url),
            **_PROBE_ON,
        )
        assert verdict.get("booted") is True, (
            "the same configuration that must refuse WITH a conflicting vector "
            f"also fails without one, so the refusal proves nothing: {verdict}"
        )
        assert verdict["health_status"] == "healthy"


def teardown_module(module) -> None:  # noqa: ARG001
    if _LISTENER is not None:
        _LISTENER.close()
    if _SCRIPT_PATH is not None and _SCRIPT_PATH.exists():
        _SCRIPT_PATH.unlink()
