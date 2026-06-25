"""Sprint 4 route registration order (R16-B1 integration review — HIGH fix).

FastAPI resolves routes in registration order: a module registered earlier wins
any shared path prefix. routes_sprint4_t6.py's module contract says it must be
registered AFTER T4 (T1 → T2 → T3 → T4 → T6). When T6 was registered first, any
future T1–T5 route sharing a path structure with T6 would be silently shadowed
by T6's handler.

These tests pin the contract so the order cannot regress:
  * the enrichment + evidence-trace endpoints resolve to their T6 handlers; and
  * the T1 routes are registered BEFORE the T6 routes (T1–T4 before T6).
"""
from __future__ import annotations


def _routes_for(app, path: str):
    return [r for r in app.routes if getattr(r, "path", None) == path]


def _first_index(app, path: str) -> int:
    for i, r in enumerate(app.routes):
        if getattr(r, "path", None) == path:
            return i
    return -1


def test_enrichment_endpoint_resolves_to_t6_handler():
    from app.main import app

    routes = _routes_for(app, "/api/runs/{run_id}/opportunities/{opp_id}/enrichment")
    assert routes, "enrichment route is not registered"
    endpoint = routes[0].endpoint
    assert endpoint.__name__ == "get_opp_enrichment", (
        f"enrichment endpoint resolves to {endpoint.__name__!r}, not the T6 handler"
    )
    assert endpoint.__module__.endswith("routes_sprint4_t6")


def test_evidence_trace_endpoint_resolves_to_t6_handler():
    from app.main import app

    routes = _routes_for(app, "/api/runs/{run_id}/opportunities/{opp_id}/evidence-trace")
    assert routes, "evidence-trace route is not registered"
    endpoint = routes[0].endpoint
    assert endpoint.__name__ == "get_evidence_trace"
    assert endpoint.__module__.endswith("routes_sprint4_t6")


def test_sprint4_t1_routes_register_before_t6():
    """T1 must be registered before T6 (the documented T1 → … → T6 order), so a
    shared path prefix can never be captured by T6's handler first."""
    from app.main import app

    t1_index = _first_index(app, "/api/runs/{run_id}/compute")          # T1
    t6_index = _first_index(app, "/api/runs/{run_id}/opportunities/{opp_id}/enrichment")  # T6
    assert t1_index != -1, "T1 compute route missing"
    assert t6_index != -1, "T6 enrichment route missing"
    assert t1_index < t6_index, (
        "routes_sprint4_t6 must be registered AFTER routes_sprint4_t1 "
        "(documented order T1 → T2 → T3 → T4 → T6)"
    )
