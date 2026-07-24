"""Manual runtime check — R191-H1 connector/vault behaviour.

Run this ON A MACHINE THAT CAN REACH THE BACKEND DB (the vault lives in the
`credentials` table). It exercises the REAL vault + REAL connector
``_get_client()`` code paths — no mocks — and answers two questions for each
connector:

  1. Does the connector resolve its credential from the vault (per-org), NOT
     from the environment?  (H1 T1/T2 — no env-credential fallback.)
  2. When the vault has no credential, does it FAIL CLOSED with a loud, NAMED
     error message (never a silent env default)?  (H1 AC1/AC4.)

It deliberately sets bogus *_URL / *_TOKEN env vars first, so if any connector
still honoured an env fallback the check would surface it as a WRONG PASS
(connected via env) — which this script flags as a FAILURE.

Usage (from backend/, with the venv active and .env pointing at the DB):

    python scripts/check_vault_connectors.py                # default org
    python scripts/check_vault_connectors.py --org my-org   # a specific org

Exit code 0 = every connector behaved correctly (vault-resolved OR loud named
miss). Exit code 1 = at least one connector behaved wrongly (silent env
fallback, wrong/blank error, or an unexpected crash).

This is a diagnostic, not a test — it reads whatever is really in your vault,
so the output depends on which connectors you have actually connected.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# This script lives in backend/scripts/ but imports the `discovery` / `app`
# packages that live under backend/. Ensure backend/ is on sys.path regardless
# of how the script is launched (python scripts/check_vault_connectors.py, or
# from another cwd), so imports resolve without needing `-m scripts.…`.
_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# Bogus env values. If a connector "connects" using any of these, the H1 fix
# regressed — the whole point is that these must be ignored.
_BOGUS_ENV = {
    "SF_INSTANCE_URL": "https://env-should-never-be-used.example",
    "SF_ACCESS_TOKEN": "ENV-TOKEN-MUST-NOT-BE-USED",
    "JIRA_URL": "https://env-should-never-be-used.example",
    "JIRA_TOKEN": "ENV-TOKEN-MUST-NOT-BE-USED",
    "SERVICENOW_URL": "https://env-should-never-be-used.example",
    "JAVA_APP_TOKEN": "ENV-TOKEN-MUST-NOT-BE-USED",
    "DOTNET_APP_TOKEN": "ENV-TOKEN-MUST-NOT-BE-USED",
}
_BOGUS_MARKER = "env-should-never-be-used"


def _banner(text: str) -> None:
    print("\n" + "=" * 72)
    print(text)
    print("=" * 72)


def _check_saas(label: str, get_client, expected_error_substrings, results):
    """Drive a SaaS connector's _get_client() against the live vault."""
    print(f"\n--- {label} ---")
    try:
        client = get_client()
    except Exception as exc:  # the connector's own named IngestError, ideally
        msg = str(exc)
        exc_name = type(exc).__name__
        leaked = _BOGUS_MARKER in msg
        named = any(s.lower() in msg.lower() for s in expected_error_substrings)
        if leaked:
            print(f"  FAIL: error leaked/used the bogus env value:\n    {msg}")
            results.append((label, False, "env value leaked into error"))
        elif named and not msg.strip().endswith(":"):
            print(f"  PASS: fail-closed with a loud, named error ({exc_name}):")
            print(f"    {msg}")
            results.append((label, True, "loud named error on vault miss"))
        else:
            print(f"  WARN: raised {exc_name} but the message is not clearly named:")
            print(f"    {msg!r}")
            results.append((label, False, "error not clearly named"))
        return

    # No exception → a credential resolved. Confirm it came from the vault, not env.
    url = getattr(client, "instance_url", None) or getattr(client, "base_url", None)
    if url and _BOGUS_MARKER in str(url):
        print(f"  FAIL: connected using the BOGUS ENV url ({url}) — env fallback "
              "still active!")
        results.append((label, False, "connected via env fallback"))
    else:
        print(f"  PASS: connected using a vault-resolved credential "
              f"(url={url!r}, not from env).")
        results.append((label, True, "vault-resolved credential"))


def _check_operational(label: str, ingestor_factory, org, results):
    """Drive an operational-app ingestor (Java/.NET) against the live vault."""
    print(f"\n--- {label} ---")
    try:
        ingestor = ingestor_factory()
        # Drain the change generator so credential resolution actually runs.
        for _ in ingestor.ingest_changes(org, None):
            pass
        health = list(getattr(ingestor, "credential_health", []) or [])
    except Exception as exc:
        print(f"  WARN: ingestor raised unexpectedly ({type(exc).__name__}): {exc}")
        results.append((label, False, "unexpected raise (should fail closed, not up)"))
        return

    if not health:
        print("  INFO: no credential-missing health recorded — either every "
              "target is credentialled in the vault, or no targets are configured.")
        results.append((label, True, "no vault miss (targets credentialled or none)"))
        return

    ok = True
    for h in health:
        msg = str(h.get("message", ""))
        if _BOGUS_MARKER in msg:
            ok = False
            print(f"  FAIL: health message leaked the bogus env value: {msg}")
        elif h.get("status") == "error" and h.get("appId") and h.get("credentialRef"):
            print(f"  PASS: target '{h['appId']}' failed closed with an actionable "
                  f"reason (credential_ref='{h['credentialRef']}').")
            print(f"        {msg}")
        else:
            ok = False
            print(f"  WARN: health record not clearly actionable: {h}")
    results.append((label, ok, "fail-closed health surfaced" if ok else "weak health"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--org", default=os.getenv("DEFAULT_ORG", "default"),
                        help="org id to resolve credentials for (default: 'default')")
    args = parser.parse_args()
    org = args.org

    # Load the real .env first (DATABASE_URL + CREDENTIAL_VAULT_KEY) so the vault
    # is reachable. Do this BEFORE planting the bogus credential vars, and note
    # load_dotenv() does not override already-set vars — so the bogus values we
    # set next always win over any credential URL/token in .env (which is exactly
    # what we want: those must be ignored by the connectors).
    try:
        from dotenv import load_dotenv
        load_dotenv(_BACKEND_DIR / ".env")
    except Exception:
        pass  # app.db also calls load_dotenv() on import; this is belt-and-braces

    # Force live mode + plant the bogus env values so any surviving env fallback
    # would show up as a wrong PASS.
    os.environ["INGEST_MODE"] = "live"
    for k, v in _BOGUS_ENV.items():
        os.environ[k] = v

    _banner(f"R191-H1 connector/vault runtime check — org='{org}', INGEST_MODE=live")
    print("Bogus env credentials planted (must be IGNORED):")
    for k in _BOGUS_ENV:
        print(f"  {k}=<bogus>")

    # Point ingestion at this org for the CLI/standalone vault path.
    try:
        from discovery.ingest import set_ingest_org
        set_ingest_org(org)
    except Exception:
        pass  # not fatal; get_ingest_org defaults are fine for most connectors

    results: list[tuple[str, bool, str]] = []

    # ---- SaaS connectors (URL + token from the credential record only) --------
    try:
        from discovery.ingest import salesforce as sf
        _check_saas(
            "Salesforce", sf._get_client,
            expected_error_substrings=["Salesforce credential", "instance URL"],
            results=results,
        )
    except Exception as exc:
        print(f"\n--- Salesforce ---\n  ERROR importing/driving: {exc}")
        results.append(("Salesforce", False, f"import/drive error: {exc}"))

    try:
        from discovery.ingest import jira
        _check_saas(
            "Jira", jira._get_client,
            expected_error_substrings=["Jira credential", "base URL"],
            results=results,
        )
    except Exception as exc:
        print(f"\n--- Jira ---\n  ERROR importing/driving: {exc}")
        results.append(("Jira", False, f"import/drive error: {exc}"))

    # ---- Operational-app connectors (Java / .NET) — fail-closed on vault miss --
    try:
        from discovery.ingest.java_app import JavaAppIngestor
        _check_operational("Java Application", JavaAppIngestor, org, results)
    except Exception as exc:
        print(f"\n--- Java Application ---\n  INFO: not exercised ({exc})")

    try:
        from discovery.ingest.dotnet_app import DotNetAppIngestor
        _check_operational(".NET Application", DotNetAppIngestor, org, results)
    except Exception as exc:
        print(f"\n--- .NET Application ---\n  INFO: not exercised ({exc})")

    # ---- Summary --------------------------------------------------------------
    _banner("SUMMARY")
    all_ok = True
    for label, ok, note in results:
        flag = "PASS" if ok else "FAIL"
        if not ok:
            all_ok = False
        print(f"  [{flag}] {label}: {note}")

    print()
    if all_ok:
        print("RESULT: all exercised connectors behaved correctly — credentials "
              "resolve from the vault and a miss fails closed with a named error.")
    else:
        print("RESULT: at least one connector behaved WRONGLY (see FAIL lines "
              "above). Do NOT raise the PR until resolved.")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
