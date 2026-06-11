# AUTH-1 — Tracked design debt & deferred hardening

These are deliberate POC scoping decisions and design tradeoffs in the AUTH-1
auth layer. They are **not bugs** — they are choices made to ship a working
login surface quickly — but each must be revisited before AUTH-1 is treated as
production-grade. File/keep a linked follow-up ticket for every item before
closing the AUTH-1 epic.

The code-level security bugs from the review (signature-less role/jti decode,
bcrypt byte truncation, password-change session revocation, org self-join by
name, weak-secret warning) have been **fixed** in this change. The items below
are the ones the review asked to be *tracked* rather than fixed inline.

---

## P1 — Targeted account-lockout via email-scoped rate limiting (review #7)

**Where:** `backend/app/auth/user_auth.py` — `check_login_rate_limit()`.

**What:** Rate limiting is scoped to the email only (5 failed attempts / 15 min
→ 429). This was a deliberate deviation from AUTH-1 AC7's per-IP limiting,
because per-IP locked out co-located POC testers sharing one office NAT /
localhost IP (see the module docstring rationale).

**Risk:** Because the throttle keys on email alone, an attacker who knows a
victim's email can deliberately submit 5 bad passwords and lock that account out
for 15 minutes on demand — a cheap, repeatable denial-of-service against a
specific user. For time-sensitive enterprise workflows (nCino loan origination,
STRS benefit decisions) a targeted lockout has real operational impact.

**Intended fix (post-POC):** Move to a combined `(email, ip)` throttle key, or a
two-tier scheme — a higher per-IP threshold to blunt distributed brute force
plus a per-`(email, ip)` lockout — so a single attacker IP cannot lock an
account globally and a legitimate user on a different IP is never affected.
Alternatively, delegate rate limiting to the API gateway (see review #8 / the
deployment note below) and reduce the application-layer limiter to defence in
depth.

**Owner:** Backend. **Status:** open follow-up.

---

## P2 — Dual DDL path: `ensure_auth_tables()` vs Alembic (review #8)

**Where:** `backend/app/main.py` startup → `app.auth.user_auth.ensure_auth_tables()`
and migrations `0004_create_users_and_login_attempts.py` / `0005_create_orgs.py`.

**What:** The runtime `ensure_auth_tables()` runs `CREATE TABLE IF NOT EXISTS`
on startup; Alembic migrations create the same tables. This mirrors the existing
`ensure_entities_table()` pattern (flagged in PR #104) and exists because
`seed_loader.py` does not run Alembic, so a fresh dev DB needs the tables created
some other way.

**Risk:** In an environment where the app starts before `alembic upgrade head`
runs, the tables already exist (created by `ensure_auth_tables()`) but are absent
from Alembic's `alembic_version` tracking. A later `alembic upgrade head`
silently succeeds (the DDL is `IF NOT EXISTS`) and stamps the head revision,
skipping any *data*/transform logic those migrations might carry.

**Intended fix (post-POC):** Treat `ensure_auth_tables()` as a development
convenience only. In staging/production, Alembic is the single source of truth —
run `alembic upgrade head` as a deploy step and gate or skip the runtime DDL
there (e.g. via an env flag). The DDL itself can never drift because the
migration and the runtime path both import the same `ALL_*_DDL` tuples.

**Owner:** Backend / DevOps. **Status:** open follow-up.

---

## P2 — Global email uniqueness blocks multi-org users (review #12)

**Where:** `backend/database/models/users.py` — `idx_users_email_unique`.

**What:** `email` is unique across the entire platform. A consultant who
legitimately needs access to two customer workspaces cannot register twice.
Documented in `users.py` as the canonical POC constraint.

**Intended fix (post-POC):** Build the multi-org identity model — move the unique
constraint from `email` to `(email, org_id)`, add a separate identity anchor to
`users`, and migrate `idx_users_email_unique`. Joining a second workspace is via
invite only (org self-join by name has been removed — review #5).

**Owner:** Backend. **Status:** open follow-up.

---

## Deployment assumption — rate-limiting layer

If the deployment sits behind an API gateway / reverse proxy (AWS API Gateway,
nginx, Cloudflare) with rate limiting already configured, the `login_attempts`
table is redundant. Confirm the gateway protection and document the decision in
`deployment/README.md` before removing the table. Do not delete it on assumption.