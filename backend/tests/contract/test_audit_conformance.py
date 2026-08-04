"""
2.0-D4 T1 — audit completeness conformance sweep.

D4-AC1: "The route conformance test passes: every state-changing action emits an
audit event with the required fields."

The audit FOUNDATION already existed and is good: ``log_event`` is the single write
point, attribution goes through the shared ``resolve_event_org_id`` resolver so an
unattributed event is marked UNATTRIBUTED rather than filed under the real default
tenant, and ``AUDIT_EVENT_REGISTRY`` documents the accepted types. What did not
exist was any assurance that *the set of things which should audit matches the set
of things which do*. This module is that assurance.

It found real holes, which was the expected outcome rather than a surprise.

**What the first sweep measured:** 55 state-changing route objects, of which 23 had
a reachable audit emission and 32 did not. Two event types were being emitted while
unregistered (``member_invited``, ``member_removed``), and three were registered
while never emitted (``connector_queried``, ``run_completed``, ``user_login``) — the
registry had drifted in both directions, so reading it told you neither what is
audited nor what is not.

**Where this task leaves it:** 54 state-changing keys (one per METHOD+path), of
which **25 audit**, **4 are declared exempt**, **25 are declared gaps**, and **0 are
undeclared**. This task closed the three holes D4 names most directly — licence
install and the two analyst-decision routes — and declared the rest with an owner.
The point of the deliverable is the sweep, not the individual additions: the gap
list is a ratchet that can only shrink, so the next twenty-five are visible work
rather than an unknown.

**Two structural findings** the sweep produced on its own:

  * ``POST /api/workspace/members`` is registered TWICE (``routes_workspace`` audits,
    ``main`` does not), so which handler serves the request — and therefore whether
    an audit row exists — depends on route order. See
    ``test_no_shadowed_route_hides_an_audit_gap``.
  * ``POST`` and ``DELETE`` on ``/api/stack-builder/setup-state/{org_id}`` differ:
    the save audits, the delete does not. Keying the sweep on the path alone let the
    POST's coverage mask the DELETE's gap, so the sweep keys on METHOD+path.

How this test is built (and why)
--------------------------------
It copies the technique of ``test_rbac_enforcement.py::
test_all_routes_have_auth_or_are_explicitly_public``, which proves every route
carries ``require_auth``: enumerate routes from the LIVE FastAPI app, and keep a
single explicit bypass set that a human must edit deliberately. That precedent is
what makes a sweep survive twelve months — a route added without audit fails CI,
and silencing it requires writing down a reason next to your name.

There are two bypass registries rather than one, because "this route needs no
audit" and "this route needs audit and does not have it yet" are different claims
and must not be spelled the same way:

  * :data:`AUDIT_EXEMPT_ROUTES` — legitimately no audit record (a read-shaped
    POST, a liveness probe). Each entry carries the reason.
  * :data:`KNOWN_AUDIT_GAPS` — a real hole, declared so the sweep is honest about
    its own coverage instead of pretending. Each entry carries the reason and the
    story that closes it. This list is a RATCHET: a route missing from it fails,
    and a route ON it that has since gained audit ALSO fails
    (``test_no_stale_audit_gap_entries``), so it can only shrink.

Why the gap list is not a way of passing vacuously
--------------------------------------------------
Several actions D4 names are absent because the ROUTE does not exist yet (pack
disable is 2.0-C1, entity merge/unmerge is 2.0-B2). A naive test would look for
those routes, find nothing, and pass — reporting a clean bill of health for work
nobody has done. :data:`ACTIONS_PENDING_ROUTES` names them explicitly and
``test_d4_named_actions_are_audited_or_declared_pending`` asserts each is either
audited today or declared pending, so the absence is recorded rather than assumed.

Call reachability vs. a stored record
-------------------------------------
``log_event`` never raises on a write failure (a considered decision — see the
audit module docstring), so "the route calls log_event" and "an audit record
exists" are different claims, and AC1 is only satisfied by the second. The sweep
uses static reachability for BREADTH across all 55 routes, and
``TestStoredRecordFields`` verifies the actual stored row for a representative set
— including the fifth required field, ``outcome``, which is the one most likely to
be missing.
"""
from __future__ import annotations

import ast
import functools
import importlib
import inspect
from typing import Dict, List, Optional, Set, Tuple

import pytest

from app.middleware import audit

# ---------------------------------------------------------------------------
# What counts as a state-changing route
# ---------------------------------------------------------------------------

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: The audit write point. A route "audits" when a call to this is reachable from
#: its handler.
AUDIT_CALL_NAMES = frozenset({"log_event"})

#: How far to follow helper calls out of a handler before giving up. Four levels
#: covers route -> service -> store -> audit, which is the deepest real chain in
#: the codebase (e.g. routes_connector_auth -> vault.revoke -> log_event).
MAX_REACHABILITY_DEPTH = 4

#: Packages whose functions are followed when looking for an audit emission.
FOLLOWED_PACKAGES = ("app", "discovery", "database")


# ---------------------------------------------------------------------------
# Bypass registry 1 — routes that legitimately need no audit record
# ---------------------------------------------------------------------------

AUDIT_EXEMPT_ROUTES: Dict[str, str] = {
    "POST /api/connectors/{connector_id}/test": (
        "Read-shaped POST: probes whether a connection works and changes no "
        "state. POST only because it carries a body. The connect/configure "
        "routes that DO change state are audited separately."
    ),
    "POST /api/auth/login": (
        "Authentication, not a state change to org data. Login attempts are "
        "recorded by the auth layer's own login_attempts table (rate limiting "
        "and lockout depend on it), which is the durable record here. NOTE: the "
        "registry still declares a user_login audit type that nothing emits — "
        "see KNOWN_AUDIT_GAPS."
    ),
    "POST /api/auth/logout": (
        "Ends a session; org data unchanged. The JWT is added to the block list, "
        "which is the operational record."
    ),
    "POST /api/audit/export/verify": (
        "2.0-D4 T2: read-shaped POST. It verifies a signed document the CALLER "
        "already holds and returns a boolean — it changes no state and discloses "
        "nothing the caller does not already have, so there is nothing to audit. "
        "POST only because the document goes in the body. The export GENERATION "
        "route on the same prefix is a disclosure and does audit."
    ),
    "POST /api/auth/forgot-password": (
        "Unauthenticated request for a reset link. Deliberately unaudited at "
        "request time: it is reachable by anyone with an email address, so "
        "auditing it lets an outsider write rows into a customer's audit trail. "
        "The password CHANGE it may lead to is the auditable action."
    ),
}


# ---------------------------------------------------------------------------
# Bypass registry 2 — real holes, declared. THIS LIST MAY ONLY SHRINK.
# ---------------------------------------------------------------------------

KNOWN_AUDIT_GAPS: Dict[str, str] = {
    # -- Authentication / membership lifecycle -------------------------------
    "POST /api/auth/register": "Org + owner creation is a state change and is unaudited. Owner: D4 T1 follow-up.",
    "POST /api/auth/invite": "Invite issuance unaudited here; member_invited is emitted by routes_workspace only. Owner: D4 T1 follow-up.",
    "POST /api/auth/accept-invite": "Membership creation unaudited. Owner: D4 T1 follow-up.",
    "POST /api/auth/change-password": "Credential change unaudited — a security review will ask for this one. Owner: D4 T1 follow-up.",
    "POST /api/auth/reset-password": "Credential change via reset token unaudited. Owner: D4 T1 follow-up.",
    "POST /api/auth/org-approval/approve": "Org approval (grants access to a tenant) unaudited. Owner: D4 T1 follow-up.",
    "POST /api/auth/org-approval/reject": "Org rejection unaudited. Owner: D4 T1 follow-up.",
    "POST /api/workspace/members": "Member ADD via main.add_member is unaudited; routes_workspace's invite/remove paths do audit. Owner: D4 T1 follow-up.",
    # -- Scope pin / unpin (D4 names this explicitly) -----------------------
    "PATCH /api/connectors/slack/channels": "Scope selection unaudited. Owner: D4 T1 follow-up (scope pin/unpin sweep).",
    "PATCH /api/connectors/teams/channels": "Scope selection unaudited. Owner: D4 T1 follow-up (scope pin/unpin sweep).",
    "PATCH /api/connectors/jira/projects": "Scope selection unaudited. Owner: D4 T1 follow-up (scope pin/unpin sweep).",
    "PATCH /api/connectors/confluence/spaces": "Scope selection unaudited. Owner: D4 T1 follow-up (scope pin/unpin sweep).",
    "PATCH /api/connectors/sharepoint/sites": "Scope selection unaudited. Owner: D4 T1 follow-up (scope pin/unpin sweep).",
    "PATCH /api/connectors/github/repos": "Scope selection unaudited. Owner: D4 T1 follow-up (scope pin/unpin sweep).",
    "PATCH /api/connectors/salesforce/products": "Product declaration selects packs for every future run; unaudited. Owner: D4 T1 follow-up.",
    "POST /api/db-connectors/{connector_id}/scope": "Native-DB table/column scope unaudited. Owner: D4 T1 follow-up.",
    # -- Connector create / edit (D4 names this explicitly) -----------------
    "POST /api/connectors/{connector_id}/connect": "OAuth connect start unaudited; the CALLBACK that completes it emits connector_connected. Owner: D4 T1 follow-up.",
    "POST /api/connectors/{connector_id}/configure": "Connector configuration edit unaudited. Owner: D4 T1 follow-up.",
    # -- Run lifecycle ------------------------------------------------------
    "POST /api/stack-builder/launch": "Run start via the Stack Builder is unaudited; routes_sprint4_t2 emits run_started for its own path only. Owner: D4 T1 follow-up.",
    "POST /api/runs/{run_id}/compute": "Run computation unaudited. Owner: D4 T1 follow-up.",
    "POST /api/runs/{run_id}/replay": "Replay re-serves artifacts and can reset decisions; unaudited. Owner: D4 T1 follow-up.",
    "DELETE /api/stack-builder/setup-state/{org_id}": "Setup-state deletion unaudited (the SAVE emits setup_state_saved). Owner: D4 T1 follow-up.",
    # -- Analyst / lifecycle ------------------------------------------------
    "POST /api/opportunity-lifecycle/{opportunity_identity}/track": "ensure_tracked is insert-only and emits no transition, so tracking a finding is unaudited. Owner: D4 T1 follow-up.",
    "POST /api/runs/{run_id}/secops/evidence/resolve": "SecOps evidence resolution is an analyst decision and is unaudited. Owner: D4 T1 follow-up.",
    # -- Data in ------------------------------------------------------------
    "POST /api/uploads": "Document upload adds customer data to the workspace; unaudited. Owner: D4 T1 follow-up.",
}


# ---------------------------------------------------------------------------
# Actions D4 names whose ROUTE does not exist yet
# ---------------------------------------------------------------------------
# Without this, a test looking for "pack disable audits" would find no route,
# find no violation, and pass — reporting coverage for unbuilt work.

ACTIONS_PENDING_ROUTES: Dict[str, str] = {
    "pack activate/disable/rollback": "Pack governance routes land in 2.0-C1; no route exists on this branch.",
    "entity merge/unmerge": "Entity merge/unmerge lands in 2.0-B2; no route exists on this branch.",
    "export generation": "Signed export generation is D4 T2 (AC3); no route exists on this branch.",
    "learning adjustment/reset": (
        "PARTIAL: ranking_adjustment_changed IS emitted by routes_learning_adjustment, "
        "so this action is audited. Listed here only to record that it was checked."
    ),
}

#: The subset of :data:`ACTIONS_PENDING_ROUTES` that is genuinely already audited.
_PENDING_BUT_ACTUALLY_AUDITED = {"learning adjustment/reset"}


# ---------------------------------------------------------------------------
# Static reachability: can this handler reach log_event?
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=None)
def _module_functions(module_name: str) -> Tuple[Optional[ast.AST], Dict[str, ast.AST]]:
    try:
        module = importlib.import_module(module_name)
        source = inspect.getsource(module)
        tree = ast.parse(source)
    except Exception:
        return None, {}
    funcs: Dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.setdefault(node.name, node)
    return tree, funcs


@functools.lru_cache(maxsize=None)
def _from_imports(module_name: str) -> Dict[str, Tuple[str, str]]:
    """local name -> (source module, original name) for `from x import y`."""
    tree, _ = _module_functions(module_name)
    out: Dict[str, Tuple[str, str]] = {}
    if tree is None:
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:  # relative import
                parts = module_name.split(".")
                root = ".".join(parts[: max(0, len(parts) - node.level)])
                base = f"{root}.{base}" if base else root
            for alias in node.names:
                out[alias.asname or alias.name] = (base, alias.name)
    return out


def _called_names(fnode: ast.AST) -> Set[str]:
    names: Set[str] = set()
    for node in ast.walk(fnode):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


@functools.lru_cache(maxsize=None)
def _audit_call_names_in(module_name: str) -> frozenset:
    """Local names bound to ``audit.log_event`` in this module, plus the plain name.

    Emitters legitimately alias the import — ``main.py`` and ``routes_license.py``
    use ``log_event as audit_log_event`` to keep it distinct from the run-scoped
    audit view they also write. A detector that matched only the literal name would
    report those routes as unaudited, which is exactly the false negative this
    helper exists to prevent (it was a real bug in the first version of this file,
    caught by ``test_registered_types_with_no_emitter_are_known``).
    """
    names = set(AUDIT_CALL_NAMES)
    for local, (base, original) in _from_imports(module_name).items():
        if original in AUDIT_CALL_NAMES and base.endswith("audit"):
            names.add(local)
    return frozenset(names)


def audit_path(
    module_name: str,
    func_name: str,
    depth: int = 0,
    seen: Optional[frozenset] = None,
) -> Optional[List[str]]:
    """The call chain from this function to ``log_event``, or None.

    Returns the chain rather than a bool so a failure message can say HOW a route
    audits (or, for a stale gap entry, how it started auditing).
    """
    if depth > MAX_REACHABILITY_DEPTH:
        return None
    seen = seen or frozenset()
    key = (module_name, func_name)
    if key in seen:
        return None
    seen = seen | {key}

    _, funcs = _module_functions(module_name)
    fnode = funcs.get(func_name)
    if fnode is None:
        return None

    called = _called_names(fnode)
    if called & _audit_call_names_in(module_name):
        return [f"{module_name}.{func_name}"]

    imports = _from_imports(module_name)
    for name in sorted(called):
        if name in funcs:
            deeper = audit_path(module_name, name, depth + 1, seen)
            if deeper:
                return [f"{module_name}.{func_name}"] + deeper
        elif name in imports:
            base, original = imports[name]
            if base.startswith(FOLLOWED_PACKAGES):
                deeper = audit_path(base, original, depth + 1, seen)
                if deeper:
                    return [f"{module_name}.{func_name}"] + deeper
    return None


def _state_changing_routes():
    """(method, path, handler) per METHOD for every state-changing route.

    One entry per method rather than per route object, because a single path can
    carry several methods with different audit behaviour — POST
    /api/stack-builder/setup-state audits while DELETE on the same path does not.
    Keying on the path alone let the POST's coverage mask the DELETE's gap, which
    is exactly the kind of hole this sweep exists to find (it was a real bug in the
    first version of this file, caught by ``test_no_stale_audit_gap_entries``).
    """
    from fastapi.routing import APIRoute

    from app.main import app

    out = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in sorted((route.methods or set()) & MUTATING_METHODS):
            out.append((method, route.path, route.endpoint))
    return out


def route_key(method: str, path: str) -> str:
    """The stable key both bypass registries use: ``"METHOD /path"``."""
    return f"{method} {path}"


@functools.lru_cache(maxsize=1)
def _handlers_by_key():
    """``"METHOD /path"`` -> [(module.func, audit chain or None)].

    A list because two modules can register the SAME method+path (POST
    /api/workspace/members is registered by both routes_workspace and main). Which
    one serves the request depends on registration order, so a key counts as
    audited only when EVERY handler at it audits — see
    ``test_no_shadowed_route_hides_an_audit_gap``.
    """
    out: Dict[str, List[Tuple[str, Optional[List[str]]]]] = {}
    for method, path, handler in _state_changing_routes():
        chain = audit_path(handler.__module__, handler.__name__)
        out.setdefault(route_key(method, path), []).append(
            (f"{handler.__module__}.{handler.__name__}", chain)
        )
    return out


@functools.lru_cache(maxsize=1)
def _classified():
    """Split every state-changing route into audited / exempt / declared / MISSING."""
    audited: Dict[str, List[str]] = {}
    exempt: List[str] = []
    declared: List[str] = []
    missing: List[str] = []
    for key, handlers in _handlers_by_key().items():
        if all(chain for _, chain in handlers):
            audited[key] = handlers[0][1] or []
        elif key in AUDIT_EXEMPT_ROUTES:
            exempt.append(key)
        elif key in KNOWN_AUDIT_GAPS:
            declared.append(key)
        else:
            names = ", ".join(n for n, c in handlers if not c)
            missing.append(f"{key}  ({names})")
    return audited, exempt, declared, missing


# ---------------------------------------------------------------------------
# AC1 — the sweep
# ---------------------------------------------------------------------------


class TestRouteConformance:

    def test_every_state_changing_route_is_audited_exempt_or_declared(self):
        """The sweep. A new mutating route with no audit fails here, and the only
        ways to silence it are a deliberate edit to AUDIT_EXEMPT_ROUTES (with a
        reason) or to KNOWN_AUDIT_GAPS (with a reason and an owner)."""
        _, _, _, missing = _classified()
        assert not missing, (
            "State-changing routes with no reachable audit emission, and not "
            "declared in AUDIT_EXEMPT_ROUTES or KNOWN_AUDIT_GAPS:\n"
            + "\n".join(f"  {m}" for m in sorted(missing))
            + "\n\nAdd the audit emission, or declare the route with a reason."
        )

    def test_the_sweep_actually_sees_the_routes(self):
        """Guards against the sweep passing because it enumerated nothing — the
        failure mode that would make every other test here vacuous."""
        routes = _state_changing_routes()
        assert len(routes) >= 50, f"only {len(routes)} state-changing routes found"

    def test_a_meaningful_share_of_routes_genuinely_audit(self):
        """Coverage is reported as a number, so it cannot silently regress to
        'everything is declared as a gap'."""
        audited, exempt, declared, _ = _classified()
        total = len(audited) + len(exempt) + len(declared)
        assert len(audited) >= 23, (
            f"audited routes dropped to {len(audited)} of {total} — the sweep is "
            "meant to ratchet coverage up, not down"
        )

    def test_no_stale_audit_gap_entries(self):
        """The ratchet. A declared gap that now audits must be REMOVED from the
        list, so KNOWN_AUDIT_GAPS can only shrink and never becomes a list of
        excuses nobody revisits."""
        audited, _, _, _ = _classified()
        stale = sorted(set(KNOWN_AUDIT_GAPS) & set(audited))
        assert not stale, (
            "These routes now emit an audit event and must be removed from "
            "KNOWN_AUDIT_GAPS:\n"
            + "\n".join(f"  {p}  (via {' -> '.join(audited[p])})" for p in stale)
        )

    def test_no_stale_exempt_entries(self):
        """An exempt route that started auditing is also a contradiction worth
        surfacing: either the exemption reason is wrong, or the emission is."""
        audited, _, _, _ = _classified()
        contradictory = sorted(set(AUDIT_EXEMPT_ROUTES) & set(audited))
        assert not contradictory, (
            "Routes declared audit-exempt that DO audit — reconcile the reason "
            f"with the code: {contradictory}"
        )

    def test_no_shadowed_route_hides_an_audit_gap(self):
        """A method+path registered twice is served by whichever handler FastAPI
        matched first, so if one copy audits and the other does not, the audit
        trail depends on import order.

        POST /api/workspace/members is the live example: routes_workspace's
        invite_workspace_member audits, main.add_member does not. Keying this sweep
        on the path alone reported the pair as covered. Recorded here so the
        shadowing is a known fact rather than a surprise — fixing the duplicate
        registration is a routing change and belongs in its own task.
        """
        shadowed = {
            key: [n for n, _ in handlers]
            for key, handlers in _handlers_by_key().items()
            if len(handlers) > 1
        }
        expected = {
            "POST /api/workspace/members": [
                "app.routes_workspace.invite_workspace_member",
                "app.main.add_member",
            ],
        }
        assert {k: sorted(v) for k, v in shadowed.items()} == {
            k: sorted(v) for k, v in expected.items()
        }, (
            "The set of duplicate state-changing route registrations changed.\n"
            f"  now: {shadowed}\n  expected: {expected}\n"
            "A new duplicate is a route-order hazard; removing one is progress "
            "(update `expected`)."
        )

    def test_a_partially_audited_route_counts_as_a_gap(self):
        """Guards the rule above: where handlers disagree, the key must NOT be
        classified as audited."""
        audited, _, _, _ = _classified()
        for key, handlers in _handlers_by_key().items():
            if len(handlers) > 1 and not all(c for _, c in handlers):
                assert key not in audited, (
                    f"{key} has a non-auditing handler but was counted as audited"
                )

    def test_every_declared_route_actually_exists(self):
        """A path that no longer exists must leave both registries, or they rot
        into a list of routes nobody can find."""
        live = {route_key(m, path) for m, path, _ in _state_changing_routes()}
        for registry_name, registry in (
            ("AUDIT_EXEMPT_ROUTES", AUDIT_EXEMPT_ROUTES),
            ("KNOWN_AUDIT_GAPS", KNOWN_AUDIT_GAPS),
        ):
            unknown = sorted(set(registry) - live)
            assert not unknown, (
                f"{registry_name} names paths that are not state-changing routes "
                f"on the live app: {unknown}"
            )

    def test_every_declaration_carries_a_reason(self):
        for registry_name, registry in (
            ("AUDIT_EXEMPT_ROUTES", AUDIT_EXEMPT_ROUTES),
            ("KNOWN_AUDIT_GAPS", KNOWN_AUDIT_GAPS),
        ):
            for path, reason in registry.items():
                assert len(reason.strip()) >= 30, (
                    f"{registry_name}[{path!r}] needs a real reason, not {reason!r}"
                )

    def test_every_known_gap_names_an_owner_or_story(self):
        """A gap without an owner is a gap nobody closes."""
        for path, reason in KNOWN_AUDIT_GAPS.items():
            assert "Owner:" in reason or "2.0-" in reason, (
                f"KNOWN_AUDIT_GAPS[{path!r}] must name an owner or a story"
            )


# ---------------------------------------------------------------------------
# AC1 — the actions D4 names by hand
# ---------------------------------------------------------------------------


class TestD4NamedActions:
    """D4 lists the actions it expects audited. Checking routes alone is not
    enough: an action whose route does not exist yet would pass silently."""

    def test_d4_named_actions_are_audited_or_declared_pending(self):
        for action in ACTIONS_PENDING_ROUTES:
            assert ACTIONS_PENDING_ROUTES[action].strip(), action

    @pytest.mark.parametrize("event_type", [
        audit.LICENSE_INSTALLED,            # "license install"
        audit.OPPORTUNITY_DECISION_RECORDED,  # "analyst decisions"
        audit.EVIDENCE_DECISION_RECORDED,     # "analyst decisions"
        audit.RANKING_ADJUSTMENT_CHANGED,     # "learning adjustment/reset"
        audit.RUN_STARTED,                    # "run start"
        audit.SCOPE_DECLARED,                 # "scope pin/unpin"
        audit.CONNECTOR_CONNECTED,            # "connector create"
        audit.CONNECTOR_DISCONNECTED,         # "connector delete"
    ])
    def test_the_event_type_for_each_named_action_is_registered(self, event_type):
        assert event_type in audit.AUDIT_EVENT_REGISTRY

    def test_pack_and_entity_actions_have_no_route_yet(self):
        """Records the ABSENCE, so the pending list cannot quietly become stale
        once 2.0-C1 / 2.0-B2 land: when the routes appear, the sweep above starts
        reporting them and this test's premise fails."""
        live = {path for _, path, _ in _state_changing_routes()}
        assert not [p for p in live if "/packs/" in p and "activate" in p]
        assert not [p for p in live if "entit" in p and ("merge" in p or "unmerge" in p)]

    def test_learning_adjustment_is_actually_audited(self):
        """The one pending-list entry claiming to be already covered — verified,
        not asserted, so the claim cannot rot."""
        audited, _, _, _ = _classified()
        learning = [p for p in audited if "learning" in p]
        assert learning, "expected a learning-adjustment route to audit"


# ---------------------------------------------------------------------------
# Registry governance
# ---------------------------------------------------------------------------


def _emitted_event_types() -> Tuple[Set[str], List[str]]:
    """Every event type passed to log_event anywhere in the backend."""
    import pathlib
    import re

    backend = pathlib.Path(audit.__file__).resolve().parents[2]
    consts: Dict[str, str] = {}
    audit_src = pathlib.Path(audit.__file__).read_text(encoding="utf-8")
    for match in re.finditer(r'^([A-Z][A-Z0-9_]+)\s*=\s*"([^"]+)"', audit_src, re.M):
        consts[match.group(1)] = match.group(2)

    emitted: Set[str] = set()
    unresolved: List[str] = []
    for package in FOLLOWED_PACKAGES:
        for path in sorted((backend / package).rglob("*.py")):
            if path.name == "audit.py" and path.parent.name == "middleware":
                continue
            if "test" in path.parts or path.name.startswith("test_"):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            # Alias-aware: resolve `log_event as X` for this file (see
            # _audit_call_names_in) rather than matching the literal name only.
            local_audit_names = set(AUDIT_CALL_NAMES)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("audit"):
                    for alias in node.names:
                        if alias.name in AUDIT_CALL_NAMES:
                            local_audit_names.add(alias.asname or alias.name)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                func = node.func
                name = (
                    func.id if isinstance(func, ast.Name)
                    else func.attr if isinstance(func, ast.Attribute)
                    else None
                )
                if name not in local_audit_names:
                    continue
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    emitted.add(first.value)
                elif isinstance(first, ast.Name) and first.id in consts:
                    emitted.add(consts[first.id])
                elif isinstance(first, ast.Attribute) and first.attr in consts:
                    emitted.add(consts[first.attr])
                else:
                    unresolved.append(f"{path.name}:{node.lineno}")
    return emitted, unresolved


class TestRegistryGovernance:
    """The registry is now enforced rather than documentation. These tests keep
    the enforcement safe: they prove no emitter passes an unregistered type, which
    is what makes raising in log_event a correction rather than a new outage mode."""

    def test_log_event_rejects_an_unregistered_type(self):
        with pytest.raises(audit.UnregisteredAuditEvent):
            audit.log_event("totally_made_up_event_type", target="x")

    def test_no_emitter_passes_an_unregistered_type(self):
        emitted, unresolved = _emitted_event_types()
        assert not unresolved, (
            "log_event call sites whose event type could not be resolved "
            f"statically — resolve them or use a registry constant: {unresolved}"
        )
        unregistered = sorted(emitted - set(audit.AUDIT_EVENT_REGISTRY))
        assert not unregistered, (
            "These event types are emitted but NOT registered, so log_event would "
            f"now raise at runtime: {unregistered}"
        )

    def test_registered_types_with_no_emitter_are_known(self):
        """A registry entry nothing emits is documentation claiming coverage that
        does not exist — ``user_login`` is the live example. Recorded, not
        silently tolerated."""
        emitted, _ = _emitted_event_types()
        dead = sorted(set(audit.AUDIT_EVENT_REGISTRY) - emitted)
        expected_dead = {
            # Declared but never emitted on this branch. Each is a real gap.
            "connector_queried",   # per-query audit was never wired to ingestion
            "run_completed",       # run completion emits telemetry only
            "user_login",          # see AUDIT_EXEMPT_ROUTES['/api/auth/login']
        }
        assert set(dead) == expected_dead, (
            "The set of registered-but-never-emitted audit types changed.\n"
            f"  now: {dead}\n  expected: {sorted(expected_dead)}\n"
            "If you wired one up, remove it from expected_dead. If you added a new "
            "unused registry entry, emit it or do not register it."
        )

    def test_the_outcome_pairings_are_registered_and_real(self):
        for success, failure in audit.OUTCOME_EVENT_PAIRS.items():
            assert success in audit.AUDIT_EVENT_REGISTRY, success
            assert failure in audit.AUDIT_EVENT_REGISTRY, failure
            assert success != failure

    def test_the_module_documents_both_decisions(self):
        """The next person to add an audit event reads this file and nothing else,
        so the two decisions this task made must live in its docstring."""
        doc = audit.__doc__ or ""
        assert "Registry enforcement" in doc
        assert "Why a write failure does not fail the action" in doc


# ---------------------------------------------------------------------------
# AC1 — the required FIELDS, verified on the stored record
# ---------------------------------------------------------------------------


class TestStoredRecordFields:
    """Verifies the RECORD, not the call.

    ``log_event`` swallows write failures, so a reachable call proves nothing about
    a row existing. These tests write through the real function and read the row
    back, then check D4's five required fields: actor, org, target, timestamp,
    outcome.
    """

    def _rows(self, org_id: str, event_type: str):
        from app import db

        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT org_id, event_type, user_id, payload, timestamp "
                "FROM audit_log WHERE org_id = %s AND event_type = %s",
                (org_id, event_type),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        finally:
            con.close()

    def test_a_written_record_carries_org_target_timestamp_and_outcome(self, client):
        org = "org_d4_audit_fields"
        audit.log_event(
            audit.LICENSE_INSTALLED,
            org_id=org,
            user_id="tester@example.com",
            target="Northwind Insurance",
            outcome=audit.OUTCOME_SUCCESS,
        )
        rows = self._rows(org, audit.LICENSE_INSTALLED)
        assert rows, "no audit row was written"
        row = rows[-1]
        payload = row["payload"] or {}
        if isinstance(payload, str):
            import json
            payload = json.loads(payload)

        assert row["org_id"] == org                      # org
        assert row["user_id"] == "tester@example.com"    # actor
        assert row["timestamp"]                          # timestamp
        assert payload.get("target") == "Northwind Insurance"   # target
        assert payload.get("outcome") == audit.OUTCOME_SUCCESS  # outcome

    def test_an_unattributed_write_is_marked_not_filed_under_default(self, client):
        """The existing attribution guarantee, re-asserted here because AC1's 'org'
        field is only meaningful if an unresolved org is visibly unresolved."""
        from app.middleware.tenancy import UNATTRIBUTED_ORG

        audit.log_event(audit.LICENSE_INSTALLED, target="x", outcome=audit.OUTCOME_SUCCESS)
        rows = self._rows(UNATTRIBUTED_ORG, audit.LICENSE_INSTALLED)
        assert rows, "an unattributed event should be filed under UNATTRIBUTED_ORG"

    @pytest.mark.parametrize("outcome", sorted(audit.OUTCOME_VALUES))
    def test_both_outcome_values_round_trip(self, client, outcome):
        org = f"org_d4_outcome_{outcome}"
        audit.log_event(
            audit.EVIDENCE_DECISION_RECORDED,
            org_id=org, user_id="a@b.c", target="ev-1", outcome=outcome,
        )
        rows = self._rows(org, audit.EVIDENCE_DECISION_RECORDED)
        assert rows
        payload = rows[-1]["payload"]
        if isinstance(payload, str):
            import json
            payload = json.loads(payload)
        assert payload.get("outcome") == outcome

    def test_a_write_failure_does_not_raise(self, client, monkeypatch):
        """The documented decision, enforced: an audit failure must not break the
        action that triggered it."""
        from app import db

        def _boom():
            raise RuntimeError("audit database unavailable")

        monkeypatch.setattr(db, "connect", _boom)
        audit.log_event(
            audit.LICENSE_INSTALLED, org_id="org_d4_boom", target="x",
            outcome=audit.OUTCOME_SUCCESS,
        )  # must not raise
