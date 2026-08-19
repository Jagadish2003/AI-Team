# On-Prem Model Serving with Ollama — Configuration Template

AgentIQ 2.1 · Pre-2.1 Hardening Pack · **HP-2 T7 (AC6)**

**Scope:** configuring AgentIQ's `in_boundary` model provider against a customer-operated
**Ollama** server, for both **text generation** and **retrieval embeddings**, including the
case where the two are served from **different hosts**.

**Audience:** the operator standing up an on-prem / air-gapped AgentIQ deployment, and the
security reviewer confirming that no model call leaves the boundary.

The companion template for vLLM is [`ON_PREM_VLLM.md`](./ON_PREM_VLLM.md). The general
`DEPLOYMENT_PROFILE` / probe / dimension-check behaviour these settings drive is documented
in [`README.md`](./README.md).

---

## Validation status — read this first

This template is **partially validated**, and the split is stated here rather than implied,
because a template that overstates its validation is worse than one that is honest about it.

### Verified end to end, on this repository, against a local stub that speaks the same HTTP surface

The configuration path below was exercised through the **real** gateway adapter
(`app/model_gateway/in_boundary_provider.py`) with no mocking of the adapter itself — only
the model server was substituted. 32 checks, all passing, each scenario in its own
interpreter with an explicitly constructed environment — the app package loads `.env` on
import, so an in-process sweep can let a real deployment value stand in for one under test.

| # | What was verified | Observed |
|---|---|---|
| 1 | `IN_BOUNDARY_BASE_URL` alone drives **both** roles | adapter issued `POST /v1/chat/completions` and `POST /v1/embeddings` |
| 2 | `embed()` returns one vector per input text | 2 inputs → 2 vectors |
| 3 | Vector length equals the declared dimension | `nomic-embed-text` → 768, `mxbai-embed-large` → 1024, `all-MiniLM-L6-v2` → 384 |
| 4 | **Different hosts**: per-role endpoint overrides, no base URL at all | embeddings hit *only* the embedding host, generation *only* the generation host, zero cross-talk |
| 5 | **Mixed**: base URL for generation + embedding override | the override wins for embeddings; generation still derives from the base URL |
| 6 | Each host is sent its **own** model name | embedding host got the embedding model, generation host got the generation model |
| 7 | Unauthenticated server works | no `Authorization` header is sent when `IN_BOUNDARY_API_KEY` is unset |
| 8 | `IN_BOUNDARY_API_KEY` is honoured when set, and read **live** | bearer header present; no restart needed after setting it |
| 9 | Wrong/missing key against an authenticated server **degrades**, never raises | `embed()` returned `[]` |
| 10 | `DEPLOYMENT_PROFILE=customer_hosted` + both provider variables → startup validation passes | HP-2.3 probe reported `ok`/`reachability` for both roles |
| 11 | An **unreachable** endpoint **fails startup** under `customer_hosted` | `ProviderUnreachable`, naming the variable, host and port |
| 12 | The same condition under `saas` does **not** block boot | logged + reported unhealthy only |
| 13 | `MODEL_PROVIDER_PROBE_TIMEOUT_SECONDS=0` disables the refusal | boot continued |
| 14 | Leaving either provider variable unset under `customer_hosted` fails startup | `MissingProviderConfiguration`, naming both variables |
| 15 | The embedding-model **name normalisation** matrix below | exactly as tabulated |
| 16 | A server that returns **fewer vectors than inputs** discards the whole batch | 1 vector for 8 inputs → adapter returned `[]` |

Versions this verification ran against:

| Component | Version |
|---|---|
| AgentIQ | branch `feature/hp-2-boundary-defaults-startup-posture`, commit `6284e5e6` |
| Python | 3.11.9 |
| OS | Darwin 25.2.0 (arm64) |
| Model server | **a local stub** speaking the same two HTTP paths — *not* Ollama |

### NOT verified — requires a run against a real Ollama server

**No Ollama binary was installed and no model was pulled during this work**, so the
following are written from Ollama's documented behaviour and are **unproven here**. They are
the checklist in [Verify on your deployment](#verify-on-your-deployment).

- That your Ollama version's `/v1/embeddings` accepts a **JSON array** for `input` (batch
  embedding). Verified check #16 above shows the consequence if it does not: the adapter
  requires one vector per input and discards the entire batch otherwise, so **nothing is
  indexed** while the run still completes. Older Ollama builds accepted only a single
  string. This is the single highest-risk item on the list.
- The exact `ollama pull` tag → dimension mapping for your pulled models. The dimension
  **table** below is the platform's declaration; that your pulled tag emits that dimension
  is a property of the model you pulled.
- Whether `num_ctx` truncation affects your corpus (see
  [Context window](#context-window--the-quiet-truncation-risk)).
- Generation model behaviour and quality. Nothing here is a model recommendation.

**Record your own versions here when you validate:**

| Component | Version you ran | Date | Result |
|---|---|---|---|
| Ollama server | | | |
| Embedding model + tag | | | |
| Generation model + tag | | | |

---

## What Ollama must serve

AgentIQ's in-boundary adapter is a plain HTTP client. It needs exactly two endpoints, both
on Ollama's compatibility surface (**not** Ollama's native `/api/*` surface):

| Role | Path the adapter calls | Ollama surface |
|---|---|---|
| Generation | `POST {base}/v1/chat/completions` | compatibility layer |
| Embeddings | `POST {base}/v1/embeddings` | compatibility layer |

Ollama listens on **port 11434** by default. To accept connections from another host, start
it with `OLLAMA_HOST=0.0.0.0:11434`.

### The exact requests the adapter sends (observed)

```http
POST /v1/embeddings
Content-Type: application/json
Authorization: Bearer <token>        # only when IN_BOUNDARY_API_KEY is set

{"model": "nomic-embed-text", "input": ["<text 1>", "<text 2>"]}
```

```http
POST /v1/chat/completions
Content-Type: application/json
Authorization: Bearer <token>        # only when IN_BOUNDARY_API_KEY is set

{"model": "llama3.1:8b", "max_tokens": 64,
 "messages": [{"role": "user", "content": "<prompt>"}]}
```

Two properties of these requests matter operationally:

- **`input` is an array.** The retrieval embedding worker sends up to
  `RETRIEVAL_EMBED_BATCH_SIZE` texts per request (**default 64**, verified). Your server must
  accept a batch that large, or lower the variable — `RETRIEVAL_EMBED_BATCH_SIZE=1` sends one
  text per request at the cost of far more round trips.
- **The response must carry one vector per input.** Verified check #16: a response with fewer
  vectors than inputs makes the adapter return `[]` for the *whole* batch. The mismatch is
  logged at WARNING (`embedding response count mismatch`) — nothing is stored, and the run
  otherwise completes normally.

The adapter never calls `GET /`, so it does not matter that Ollama 404s there. The HP-2.3
startup probe opens a **TCP connection only** — no HTTP request, no model call, no cost.

---

## Configuration — one host serving both roles

The simplest topology: one Ollama box, both models loaded, both roles derived from a single
base URL.

```bash
# --- Deployment posture (HP-2.1) -------------------------------------------
# Required. Under customer_hosted there is NO provider default: leave either
# variable below unset and startup fails, naming both. That is deliberate — an
# inherited cloud default is what HP-2 removes.
DEPLOYMENT_PROFILE=customer_hosted

# --- Which provider serves each role (HP-2.2) ------------------------------
# Resolved INDEPENDENTLY. Both must be set explicitly under customer_hosted.
MODEL_GENERATION_PROVIDER=in_boundary
MODEL_EMBEDDING_PROVIDER=in_boundary

# --- The Ollama endpoint ---------------------------------------------------
# Base URL only: the adapter derives /v1/chat/completions and /v1/embeddings.
# No trailing slash, no path.
IN_BOUNDARY_BASE_URL=http://ollama.internal:11434

# --- Model names (sent verbatim as the request's "model" field) -------------
# These must be names YOUR Ollama serves — check `ollama list`.
IN_BOUNDARY_GENERATION_MODEL=llama3.1:8b
IN_BOUNDARY_EMBEDDING_MODEL=nomic-embed-text

# --- Credential: OPTIONAL for Ollama ---------------------------------------
# Ollama is unauthenticated by default and the platform declares
# credential_required=False for in_boundary, so a missing key is NOT a fault and
# will not fail the startup probe (verified). Set it only if you front Ollama
# with a reverse proxy that requires a bearer token.
IN_BOUNDARY_API_KEY=

# --- Startup reachability probe (HP-2.3) -----------------------------------
# Seconds per attempt, one attempt, no retries. Under customer_hosted an
# unreachable endpoint FAILS STARTUP.
MODEL_PROVIDER_PROBE_TIMEOUT_SECONDS=3
```

Leave `IN_BOUNDARY_GENERATION_ENDPOINT` and `IN_BOUNDARY_EMBEDDING_ENDPOINT` **unset** in
this topology. `IN_BOUNDARY_MODEL` is a common fallback for both roles; the two role-specific
variables above take precedence and are clearer, so prefer them.

### Preparing the Ollama host

```bash
# Bind to the network (default binds to localhost only).
export OLLAMA_HOST=0.0.0.0:11434
ollama serve

# Pull one embedding model and one generation model.
ollama pull nomic-embed-text        # 768-dim embeddings, 8192-token context
ollama pull llama3.1:8b             # generation

ollama list                          # the names to put in the two variables above
```

---

## Configuration — generation and embeddings on DIFFERENT hosts

This is the common real topology: generation wants a large GPU, embeddings are cheap and
belong next to the database. The two role-specific endpoint variables exist for exactly this,
and each is a **complete URL including the path** — they are not joined to a base URL.

Verified (check #4): with **no** `IN_BOUNDARY_BASE_URL` at all, embeddings reached only the
embedding host and generation only the generation host, each carrying its own model name.

```bash
DEPLOYMENT_PROFILE=customer_hosted
MODEL_GENERATION_PROVIDER=in_boundary
MODEL_EMBEDDING_PROVIDER=in_boundary

# No base URL. Each role names its own full endpoint URL.
IN_BOUNDARY_BASE_URL=
IN_BOUNDARY_GENERATION_ENDPOINT=http://ollama-gpu.internal:11434/v1/chat/completions
IN_BOUNDARY_EMBEDDING_ENDPOINT=http://ollama-embed.internal:11434/v1/embeddings

IN_BOUNDARY_GENERATION_MODEL=llama3.1:70b       # served by ollama-gpu
IN_BOUNDARY_EMBEDDING_MODEL=nomic-embed-text    # served by ollama-embed

IN_BOUNDARY_API_KEY=
MODEL_PROVIDER_PROBE_TIMEOUT_SECONDS=3
```

Each role is probed **at its own endpoint** and reported independently (verified, check
#10/#4), so a health report tells you *which* host is down, not merely that something is.

### Mixed: base URL for one role, override for the other

An override wins over the base URL for its own role and leaves the other alone (verified,
check #5). Useful when the embedding model moves to its own box but generation does not:

```bash
IN_BOUNDARY_BASE_URL=http://ollama-gpu.internal:11434            # generation derives from this
IN_BOUNDARY_EMBEDDING_ENDPOINT=http://ollama-embed.internal:11434/v1/embeddings
```

### Only embeddings in-boundary

Generation and embeddings resolve independently, so this is a valid — and common — posture:
run retrieval entirely on-prem while generation stays elsewhere. Note that setting `hosted`
under `customer_hosted` is permitted but must be **deliberate**; HP-2 removes the inherited
default, not the option.

```bash
DEPLOYMENT_PROFILE=customer_hosted
MODEL_GENERATION_PROVIDER=hosted        # a deliberate, reviewed choice
MODEL_EMBEDDING_PROVIDER=in_boundary
IN_BOUNDARY_BASE_URL=http://ollama-embed.internal:11434
IN_BOUNDARY_EMBEDDING_MODEL=nomic-embed-text
```

> The reverse — `MODEL_EMBEDDING_PROVIDER=hosted` — silently **disables retrieval**: the
> hosted provider has no embeddings endpoint, so every chunk stays pending forever and every
> search returns nothing. Startup logs a warning for exactly this.

---

## Model names: the normalisation trap

`IN_BOUNDARY_EMBEDDING_MODEL` is sent verbatim to Ollama **and** is the name the HP-2.4
startup dimension check looks up. Ollama's short pull tags do not all match the names the
platform declares, and a name with **no declared dimension is skipped, not refused** — an
unknown model is not a mismatch, so the check silently does nothing.

Verified behaviour (`app/retrieval/embedding_dimensions.normalise_model_name`):

| You configure | Normalises to | Declared dimension | HP-2.4 check |
|---|---|---|---|
| `nomic-embed-text` | `nomic-embed-text` | 768 | **runs** |
| `nomic-embed-text:latest` | `nomic-embed-text` | 768 | **runs** (tag folded) |
| `mxbai-embed-large` | `mxbai-embed-large` | 1024 | **runs** |
| `mxbai-embed-large:335m` | `mxbai-embed-large` | 1024 | **runs** (tag folded) |
| `all-minilm` | `all-minilm` | *none* | **skipped** |
| `bge-large` | `bge-large` | *none* | **skipped** |

The two Ollama short tags `all-minilm` and `bge-large` are the models the platform declares
as `all-MiniLM-L6-v2` (384) and `bge-large-en-v1.5` (1024) — the *models* are supported and
work fine; it is the *name* the check cannot resolve. Two remedies, either is fine:

```bash
# (a) Alias the pulled model to the declared name, then configure that name.
ollama cp all-minilm all-MiniLM-L6-v2
IN_BOUNDARY_EMBEDDING_MODEL=all-MiniLM-L6-v2

# (b) Or add a row to app/retrieval/embedding_dimensions.py — one line, and
#     explicitly NO migration and NO re-embed:
#         "all-minilm": _entry("all-minilm", 384, BASIS_PUBLISHED),
```

Doing neither is a legitimate choice; it just means you lose the startup dimension check for
that model. Nothing else breaks.

### Context window — the quiet truncation risk

`MAX_CHUNK_CHARS = 2000` (verified in `app/retrieval/chunking.py`). At roughly 4 characters
per token that is about 500 tokens — which fits a **512-token** model only just. Code, JSON
and non-Latin text tokenise considerably denser, so a 2000-character chunk of source code can
exceed 512 tokens and be **truncated server-side with no error**: the tail of the chunk is
simply not represented in the vector, and nothing reports it.

| Model | Context | Comfortable for a 2000-char chunk? |
|---|---|---|
| `nomic-embed-text` | 8192 tokens | yes, with wide margin |
| `mxbai-embed-large` | 512 tokens | prose yes; code/JSON at risk |
| `all-MiniLM-L6-v2` | 512 tokens (256 trained) | prose yes; code/JSON at risk |
| `bge-large-en-v1.5` | 512 tokens | prose yes; code/JSON at risk |

Prefer `nomic-embed-text` unless your corpus is short English prose. If you use a 512-token
model on a corpus containing code, also raise Ollama's `num_ctx` for that model — Ollama
defaults it well below some models' maximum, and the model's own limit still applies.

---

## Supported embedding models and their dimensions

The pgvector column is an unqualified `vector` with **no fixed dimension** by design, so
**every model below works today with no schema migration and no re-embed.** Changing model is
a configuration change; the per-vector `(embedding_model, embedding_model_version)` stamp
keeps vectors from different models from ever being compared.

**Single source of truth: [`backend/app/retrieval/embedding_dimensions.py`](../backend/app/retrieval/embedding_dimensions.py)**
(`MODEL_DIMENSIONS`). That table is what HP-2.4's startup check reads via
`declared_dimension()`. The rows below are pinned against it by
`backend/tests/contract/test_hp2_onprem_templates.py`, so this document and the code
**cannot drift**: change one without the other and CI fails.

### Self-hosted — served in-boundary by Ollama, vLLM, or any compatible server

| Model | Dimensions | Basis |
|---|---|---|
| `all-MiniLM-L6-v2` | 384 | published |
| `bge-small-en-v1.5` | 384 | published |
| `bge-base-en-v1.5` | 768 | published |
| `nomic-embed-text` | 768 | published |
| `bge-large-en` | 1024 | published |
| `bge-large-en-v1.5` | 1024 | published |
| `mxbai-embed-large` | 1024 | published |

### Managed — reachable through `in_boundary` only via a proxy inside your boundary

Listed because the same declaration governs them, and an on-prem deployment that fronts a
managed model with an in-boundary gateway needs these numbers too.

| Model | Dimensions | Basis |
|---|---|---|
| `text-embedding-3-small` | 1536 | measured |
| `text-embedding-ada-002` | 1536 | published |
| `text-embedding-3-large` | 3072 | published |

`basis` follows the convention used by `app/scale_envelope.py`: `published` means the model
publisher documents the number; `measured` means it was observed from vectors this platform
actually produced.

**Adding a model is one line** in `MODEL_DIMENSIONS` — no migration, no re-embed, no schema
change. A model that is absent simply has no declared dimension, so the startup check skips
it (see the normalisation trap above).

### Changing embedding model on a populated deployment

The dimension check refuses startup when the configured model's declared dimension differs
from what is already stored **under the active model stamp**. Repinning
`IN_BOUNDARY_EMBEDDING_MODEL` to a different model changes the stamp
(`in_boundary:<model>` — verified, check #8 of the harness), so existing vectors become
non-active and the R18-B2 backfill converges them in the background. You do **not** need a
migration or a manual re-embed. If you instead hit the refusal, it means the stored rows carry
the *same* stamp as the model you have configured; the message names both dimensions and the
two remedies.

---

## What startup does with this configuration

| Stage | What happens | On failure under `customer_hosted` |
|---|---|---|
| `DEPLOYMENT_PROFILE` resolved | must be `saas` or `customer_hosted` | unrecognised value → **refuse** |
| Provider names resolved | both variables must be set | either unset → **refuse** (verified #14) |
| `endpoint_configuration` | is there an endpoint at all? | none → **refuse** |
| `credential_presence` | not required for `in_boundary` | never a fault here (verified #7) |
| `reachability` | one bounded TCP connect per role | unreachable → **refuse** (verified #11) |
| Embedding dimension | declared vs stored under the active stamp | conflict → **refuse** (both profiles) |

Under `saas` the same conditions are logged and reported unhealthy but never block boot
(verified #12).

**Ordering caveat worth planning for:** if AgentIQ starts before Ollama, the reachability
probe fails and the process refuses to start. Either order your service units so Ollama comes
up first (a systemd `After=`/`Requires=` is enough), or set
`MODEL_PROVIDER_PROBE_TIMEOUT_SECONDS=0` to skip reachability probing — at the cost of losing
the check that exists to stop a silently-degraded deployment.

---

## Verify on your deployment

Work top to bottom; each step is cheap and the failures are distinguishable.

- [ ] **1. Ollama reachable from the AgentIQ host.**
      `curl -sS http://ollama.internal:11434/api/tags` returns your model list. If this fails,
      nothing below can work — `OLLAMA_HOST=0.0.0.0:11434` and firewall first.
- [ ] **2. The compatibility surface serves embeddings.**
      ```bash
      curl -sS http://ollama.internal:11434/v1/embeddings \
        -H 'Content-Type: application/json' \
        -d '{"model":"nomic-embed-text","input":["hello"]}' | head -c 200
      ```
      Expect `{"object":"list","data":[{"object":"embedding","index":0,"embedding":[...`.
- [ ] **3. Batch input is accepted — the highest-risk item.**
      ```bash
      curl -sS http://ollama.internal:11434/v1/embeddings \
        -H 'Content-Type: application/json' \
        -d '{"model":"nomic-embed-text","input":["one","two","three"]}' \
        | python -c 'import json,sys; print(len(json.load(sys.stdin)["data"]))'
      ```
      **Must print `3`.** If it prints `1`, your Ollama does not batch: either upgrade it, or
      set `RETRIEVAL_EMBED_BATCH_SIZE=1`. Leaving it unset in that state indexes **nothing**
      while every run still completes and reports normally.
- [ ] **4. The dimension matches what the platform declares.**
      ```bash
      curl -sS http://ollama.internal:11434/v1/embeddings \
        -H 'Content-Type: application/json' \
        -d '{"model":"nomic-embed-text","input":["hello"]}' \
        | python -c 'import json,sys; print(len(json.load(sys.stdin)["data"][0]["embedding"]))'
      ```
      Must equal the table row for your model (768 for `nomic-embed-text`).
- [ ] **5. Generation responds.**
      ```bash
      curl -sS http://ollama-gpu.internal:11434/v1/chat/completions \
        -H 'Content-Type: application/json' \
        -d '{"model":"llama3.1:8b","max_tokens":16,
             "messages":[{"role":"user","content":"reply with OK"}]}' | head -c 300
      ```
      Expect a `choices[0].message.content`.
- [ ] **6. AgentIQ starts.** With the configuration above, the process boots and the startup
      log records `model_gateway config validated` plus the resolved posture. A refusal here is
      the system working: read the message, it names the check, the variable and the host.
- [ ] **7. Health reports the posture.** `GET /api/health` →
      `checks.model_providers.roles.{generation,embedding}.status == "ok"`. This is a public
      route and deliberately reports **no endpoint host**.
- [ ] **8. Chunks actually embed.** After a discovery run,
      `GET /api/retrieval/freshness` (analyst+) → `pending_embeddings` should fall toward 0 as
      the background worker drains. A `pending_embeddings` count that never moves with a
      healthy posture is the signature of step 3 failing.
- [ ] **9. Retrieval returns something.** A finding's evidence should carry retrieved chunks.
      Empty retrieval with `pending_embeddings == 0` and no stale chunks points at the model,
      not the plumbing.
- [ ] **10. Record your versions** in the table at the top of this document.

---

## Troubleshooting

Every row is a failure mode observed during verification or directly implied by verified
behaviour.

| Symptom | Cause | Fix |
|---|---|---|
| Startup: `MissingProviderConfiguration`, naming both variables | `customer_hosted` with a provider variable unset | set **both** `MODEL_GENERATION_PROVIDER` and `MODEL_EMBEDDING_PROVIDER` |
| Startup: `ProviderUnreachable … is not reachable (connection refused)` | Ollama not running, wrong port, or bound to localhost | start Ollama with `OLLAMA_HOST=0.0.0.0:11434`; check the firewall; or start Ollama before AgentIQ |
| Startup: `InvalidDeploymentProfile` | a typo such as `on_prem` or `customer-hosted` | the vocabulary is closed — use exactly `customer_hosted` |
| Startup: `EmbeddingDimensionMismatch` | the configured model's dimension differs from what is stored under the **active** stamp | repin to the model that produced the stored vectors, or move the model **version** so the backfill can reach those rows |
| Log: `embedding response count mismatch (texts=64 vectors=1)`, and nothing indexes | the server embedded only the first input | verify step 3; upgrade Ollama or set `RETRIEVAL_EMBED_BATCH_SIZE=1` |
| Log: `embedding skipped: IN_BOUNDARY_EMBEDDING_ENDPOINT or IN_BOUNDARY_BASE_URL not configured` | neither a base URL nor the role override is set | set one of them |
| Log: `embedding skipped: IN_BOUNDARY_MODEL not configured` | no model name for that role | set `IN_BOUNDARY_EMBEDDING_MODEL` (or `IN_BOUNDARY_MODEL`) |
| Log: `HTTP 404` from the endpoint | pointed at Ollama's native `/api/*` surface, or a path typo in a role override | the role overrides are **full URLs** and must end in `/v1/chat/completions` / `/v1/embeddings` |
| Log: `HTTP 401`, `embed()` returns `[]` | a proxy in front of Ollama requires a token | set `IN_BOUNDARY_API_KEY` (read live — no restart needed) |
| Log: `HTTP 404 … model not found` | the configured name is not a name Ollama serves | compare against `ollama list`; Ollama names are exact, tags included |
| Retrieval always empty, posture `ok`, `pending_embeddings` stuck | see the count-mismatch row above, or `MODEL_EMBEDDING_PROVIDER=hosted` | the hosted provider has no embeddings endpoint |
| Startup dimension check never runs for your model | the configured name has no declared dimension | alias to the declared name, or add the one-line row — see the normalisation trap |
| Long chunks appear truncated in retrieval | a 512-token model with dense content | use `nomic-embed-text` (8192), or raise `num_ctx` |

---

## Security notes for the reviewer

- **Every model call is outbound-only and stays inside your boundary.** The two URLs you
  configure are the only model endpoints AgentIQ contacts in this posture, and the adapter is
  the only code permitted to contact them — enforced in CI by a scan that fails the build on
  any direct model call outside `backend/app/model_gateway/`.
- **`IN_BOUNDARY_API_KEY` is optional and never logged.** The config object redacts it in
  `repr`, it is resolved live per call rather than cached, and the startup probe reports only
  *presence*, never the value.
- **The startup probe is a TCP connect, not a model call.** No prompt, no cost, one attempt,
  no retries, and it is never re-run by the health endpoint — so a health check can never
  become a lever pointed at your model server.
- **`GET /api/health` publishes no endpoint host.** It is a public route; the host and port
  stay in the startup log.
- **Setting `hosted` under `customer_hosted` remains possible and must be deliberate.** If
  your review requires that nothing leaves the boundary, confirm both provider variables read
  `in_boundary` (or `customer_tenant` pointed inside your tenancy).

## Sign-off

| | Name | Date |
|---|---|---|
| Author | | |
| Reviewer (not the author) | | |
| Validated against real Ollama by | | |
| Notes | | |

---

*Companion template: [`ON_PREM_VLLM.md`](./ON_PREM_VLLM.md). Adapter:
`backend/app/model_gateway/in_boundary_provider.py` + `in_boundary_config.py`. Dimension
table: `backend/app/retrieval/embedding_dimensions.py`. Drift gate:
`backend/tests/contract/test_hp2_onprem_templates.py`.*
