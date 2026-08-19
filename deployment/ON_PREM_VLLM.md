# On-Prem Model Serving with vLLM — Configuration Template

AgentIQ 2.1 · Pre-2.1 Hardening Pack · **HP-2 T7 (AC6)**

**Scope:** configuring AgentIQ's `in_boundary` model provider against a customer-operated
**vLLM** server, for both **text generation** and **retrieval embeddings**, including the case
where the two are served from **different hosts** — which for vLLM is not an edge case but the
**normal** topology.

**Audience:** the operator standing up an on-prem / air-gapped AgentIQ deployment, and the
security reviewer confirming that no model call leaves the boundary.

The companion template for Ollama is [`ON_PREM_OLLAMA.md`](./ON_PREM_OLLAMA.md). The general
`DEPLOYMENT_PROFILE` / probe / dimension-check behaviour these settings drive is documented in
[`README.md`](./README.md).

> **vLLM serves one model per process.** Generation and embeddings therefore require **two
> processes**, and usually two hosts. Everything in
> [Configuration — two servers](#configuration--two-servers-the-normal-vllm-topology) is the
> default path, not a variant.

---

## Validation status — read this first

This template is **partially validated**, and the split is stated here rather than implied,
because a template that overstates its validation is worse than one that is honest about it.

### Verified end to end, on this repository, against a local stub that speaks the same HTTP surface

The configuration path below was exercised through the **real** gateway adapter
(`app/model_gateway/in_boundary_provider.py`) with no mocking of the adapter itself — only the
model server was substituted. 32 checks, all passing, each scenario in its own interpreter
with an explicitly constructed environment. The full list is in
[`ON_PREM_OLLAMA.md`](./ON_PREM_OLLAMA.md#validation-status--read-this-first); the items that
matter most for vLLM's two-server shape are repeated here.

| # | What was verified | Observed |
|---|---|---|
| 1 | **Two hosts, no base URL at all** — per-role endpoint overrides stand alone | embeddings hit *only* the embedding host, generation *only* the generation host, zero cross-talk |
| 2 | Each host is sent its **own** model name | the embedding host received the embedding model id, the generation host the generation model id |
| 3 | Each role is probed **at its own endpoint**, independently | both reported `ok`/`reachability`; a failure names *which* host |
| 4 | Vector length equals the declared dimension | `all-MiniLM-L6-v2` → 384, `nomic-embed-text` → 768, `mxbai-embed-large` → 1024 |
| 5 | `IN_BOUNDARY_API_KEY` is honoured as a **bearer** header when set, read live | no restart needed after setting it |
| 6 | Wrong/missing key against an authenticated server **degrades**, never raises | `embed()` returned `[]` |
| 7 | An **unreachable** endpoint **fails startup** under `customer_hosted` | `ProviderUnreachable`, naming the variable, host and port |
| 8 | Leaving either provider variable unset under `customer_hosted` fails startup | `MissingProviderConfiguration`, naming both variables |
| 9 | Hugging Face **namespace prefixes are folded** by the dimension lookup | `BAAI/bge-large-en-v1.5` → `bge-large-en-v1.5` → 1024 |
| 10 | A server returning **fewer vectors than inputs** discards the whole batch | 1 vector for 8 inputs → adapter returned `[]` |
| 11 | Mixed base-URL + one override behaves as documented | the override wins for its role only |
| 12 | `MODEL_PROVIDER_PROBE_TIMEOUT_SECONDS=0` disables the startup refusal | boot continued |

Versions this verification ran against:

| Component | Version |
|---|---|
| AgentIQ | branch `feature/hp-2-boundary-defaults-startup-posture`, commit `6284e5e6` |
| Python | 3.11.9 |
| OS | Darwin 25.2.0 (arm64) |
| Model server | **a local stub** speaking the same two HTTP paths — *not* vLLM |

### NOT verified — requires a run against a real vLLM server

**No vLLM was installed and no model was served during this work**, so the following are
written from vLLM's documented behaviour and are **unproven here**. They are the checklist in
[Verify on your deployment](#verify-on-your-deployment).

- **The exact flag that puts vLLM in embedding mode, for your version.** This is the item most
  likely to differ: vLLM has used `--task embed` and, in later releases, a pooling-runner
  selection (`--runner pooling`). Some embedding architectures are auto-detected and need no
  flag at all. Check `vllm serve --help` on the version you install; do not copy a flag from
  here on faith.
- Whether your chosen embedding model is in vLLM's supported-architecture set, and whether it
  needs `--trust-remote-code`.
- That your vLLM build's `/v1/embeddings` accepts a **JSON array** for `input`. Verified check
  #10 shows the consequence if it does not: the adapter requires one vector per input and
  discards the entire batch otherwise, so **nothing is indexed** while the run still completes.
- Throughput, memory sizing, `--max-model-len`, tensor parallelism. Nothing here is a capacity
  recommendation.

**Record your own versions here when you validate:**

| Component | Version you ran | Date | Result |
|---|---|---|---|
| vLLM (embeddings) | | | |
| vLLM (generation) | | | |
| Embedding model id | | | |
| Generation model id | | | |
| CUDA / driver | | | |

---

## What vLLM must serve

AgentIQ's in-boundary adapter is a plain HTTP client. It needs exactly two endpoints, both on
vLLM's compatibility server:

| Role | Path the adapter calls | Served by |
|---|---|---|
| Generation | `POST {base}/v1/chat/completions` | the generation vLLM process |
| Embeddings | `POST {base}/v1/embeddings` | the embedding vLLM process |

vLLM listens on **port 8000** by default (`--port`, `--host 0.0.0.0`). It exposes `GET /health`
and `GET /v1/models`, which are useful for your own checks — AgentIQ calls neither. The HP-2.3
startup probe opens a **TCP connection only**: no HTTP request, no model call, no cost.

### The exact requests the adapter sends (observed)

```http
POST /v1/embeddings
Content-Type: application/json
Authorization: Bearer <token>        # only when IN_BOUNDARY_API_KEY is set

{"model": "BAAI/bge-large-en-v1.5", "input": ["<text 1>", "<text 2>"]}
```

```http
POST /v1/chat/completions
Content-Type: application/json
Authorization: Bearer <token>        # only when IN_BOUNDARY_API_KEY is set

{"model": "Qwen/Qwen2.5-7B-Instruct", "max_tokens": 64,
 "messages": [{"role": "user", "content": "<prompt>"}]}
```

Three properties matter operationally:

- **`model` must match what vLLM serves it as.** By default that is the full model id you
  launched with (`BAAI/bge-large-en-v1.5`). `--served-model-name` overrides it — see
  [Model ids](#model-ids-and-the-dimension-lookup).
- **`input` is an array.** The retrieval embedding worker sends up to
  `RETRIEVAL_EMBED_BATCH_SIZE` texts per request (**default 64**, verified). Your server must
  accept a batch that large, or lower the variable.
- **The response must carry one vector per input.** Verified check #10: fewer vectors than
  inputs makes the adapter return `[]` for the *whole* batch. It is logged at WARNING
  (`embedding response count mismatch`) — nothing is stored, and the run otherwise completes
  normally.

The adapter sends `max_tokens` for generation and nothing else beyond `model` and `messages`;
it does not send `temperature`, `stream`, or any sampling parameter, so server-side defaults
apply.

---

## Configuration — two servers (the normal vLLM topology)

One vLLM process per model. The two role-specific endpoint variables are **complete URLs
including the path** — they are not joined to a base URL, and `IN_BOUNDARY_BASE_URL` is left
empty. Verified (check #1) with no base URL configured at all.

### Start the servers

```bash
# --- Host A: generation --------------------------------------------------
vllm serve Qwen/Qwen2.5-7B-Instruct \
  --host 0.0.0.0 --port 8000

# --- Host B: embeddings --------------------------------------------------
# NB: the embedding-mode flag is version-sensitive — confirm with
#     `vllm serve --help` before copying this line. See "NOT verified" above.
vllm serve BAAI/bge-large-en-v1.5 \
  --task embed \
  --host 0.0.0.0 --port 8000
```

The older entrypoint form is equivalent if your version predates the `vllm serve` CLI:

```bash
python -m vllm.entrypoints.openai.api_server --model BAAI/bge-large-en-v1.5 --port 8000
```

### AgentIQ configuration

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

# --- Two endpoints, one per role -------------------------------------------
# No base URL: each role names its OWN full endpoint URL, path included.
IN_BOUNDARY_BASE_URL=
IN_BOUNDARY_GENERATION_ENDPOINT=http://vllm-gen.internal:8000/v1/chat/completions
IN_BOUNDARY_EMBEDDING_ENDPOINT=http://vllm-embed.internal:8000/v1/embeddings

# --- Model ids (sent verbatim as the request's "model" field) ---------------
# Must match what each server serves — check GET /v1/models on each host.
IN_BOUNDARY_GENERATION_MODEL=Qwen/Qwen2.5-7B-Instruct
IN_BOUNDARY_EMBEDDING_MODEL=BAAI/bge-large-en-v1.5

# --- Credential: OPTIONAL for vLLM -----------------------------------------
# vLLM is unauthenticated unless launched with --api-key. The platform declares
# credential_required=False for in_boundary, so a missing key is NOT a fault and
# will not fail the startup probe (verified). If BOTH servers are launched with
# --api-key they must share the SAME key — there is one IN_BOUNDARY_API_KEY for
# both roles.
IN_BOUNDARY_API_KEY=

# --- Startup reachability probe (HP-2.3) -----------------------------------
# Seconds per attempt, one attempt, no retries. Under customer_hosted an
# unreachable endpoint FAILS STARTUP — and names WHICH role and host.
MODEL_PROVIDER_PROBE_TIMEOUT_SECONDS=3
```

Each role is probed at its own endpoint and reported independently (verified, check #3), so a
health report tells you which of the two servers is down.

`IN_BOUNDARY_MODEL` is a common fallback used for *both* roles when a role-specific model name
is unset. It is a poor fit for vLLM — the two processes serve different models by construction,
so a single shared name would be wrong for one of them. Leave it empty and set the two
role-specific variables above.

> **One key for two servers.** There is a single `IN_BOUNDARY_API_KEY` covering both roles. If
> your two vLLM processes must hold *different* keys, front them with a reverse proxy that
> normalises the credential, or leave both unauthenticated and rely on network controls. Two
> distinct per-role keys are not expressible today.

### Same host, two ports

Two ports on one box works identically — the two endpoint variables just differ by port. This
is the usual single-GPU-box arrangement:

```bash
IN_BOUNDARY_BASE_URL=
IN_BOUNDARY_GENERATION_ENDPOINT=http://vllm.internal:8000/v1/chat/completions
IN_BOUNDARY_EMBEDDING_ENDPOINT=http://vllm.internal:8001/v1/embeddings
```

### One base URL behind a router

If you front both vLLM processes with a single reverse proxy that routes `/v1/embeddings` and
`/v1/chat/completions` to the right backend, the base-URL form applies and the adapter derives
both paths (verified, check #1 of the Ollama template):

```bash
IN_BOUNDARY_BASE_URL=http://model-router.internal
IN_BOUNDARY_GENERATION_ENDPOINT=
IN_BOUNDARY_EMBEDDING_ENDPOINT=
IN_BOUNDARY_GENERATION_MODEL=Qwen/Qwen2.5-7B-Instruct
IN_BOUNDARY_EMBEDDING_MODEL=BAAI/bge-large-en-v1.5
```

A base URL plus a single role override is also valid — the override wins for its own role and
leaves the other deriving from the base (verified, check #11).

### Only embeddings in-boundary

Generation and embeddings resolve independently, so running retrieval entirely on vLLM while
generation stays elsewhere is a valid posture. Setting `hosted` under `customer_hosted` is
permitted but must be **deliberate** — HP-2 removes the inherited default, not the option.

```bash
DEPLOYMENT_PROFILE=customer_hosted
MODEL_GENERATION_PROVIDER=hosted        # a deliberate, reviewed choice
MODEL_EMBEDDING_PROVIDER=in_boundary
IN_BOUNDARY_EMBEDDING_ENDPOINT=http://vllm-embed.internal:8000/v1/embeddings
IN_BOUNDARY_EMBEDDING_MODEL=BAAI/bge-large-en-v1.5
```

> The reverse — `MODEL_EMBEDDING_PROVIDER=hosted` — silently **disables retrieval**: the
> hosted provider has no embeddings endpoint, so every chunk stays pending forever and every
> search returns nothing. Startup logs a warning for exactly this.

---

## Model ids and the dimension lookup

`IN_BOUNDARY_EMBEDDING_MODEL` is sent verbatim to vLLM **and** is the name the HP-2.4 startup
dimension check looks up. A name with **no declared dimension is skipped, not refused** — an
unknown model is not a mismatch, so the check silently does nothing.

The lookup folds a Hugging Face namespace prefix and a trailing tag, so most vLLM model ids
resolve directly. Verified behaviour
(`app/retrieval/embedding_dimensions.normalise_model_name`):

| You configure | Normalises to | Declared dimension | HP-2.4 check |
|---|---|---|---|
| `sentence-transformers/all-MiniLM-L6-v2` | `all-minilm-l6-v2` | 384 | **runs** |
| `BAAI/bge-large-en-v1.5` | `bge-large-en-v1.5` | 1024 | **runs** |
| `BAAI/bge-base-en-v1.5` | `bge-base-en-v1.5` | 768 | **runs** |
| `BAAI/bge-small-en-v1.5` | `bge-small-en-v1.5` | 384 | **runs** |
| `nomic-ai/nomic-embed-text-v1.5` | `nomic-embed-text-v1.5` | *none* | **skipped** |
| `mixedbread-ai/mxbai-embed-large-v1` | `mxbai-embed-large-v1` | *none* | **skipped** |

The last two are the models the platform declares as `nomic-embed-text` (768) and
`mxbai-embed-large` (1024) — the *models* are supported and work fine; it is the versioned
*id suffix* the lookup cannot resolve. Three remedies, any is fine:

```bash
# (a) Serve it under the declared name (recommended — one launch flag, no code change).
vllm serve nomic-ai/nomic-embed-text-v1.5 --task embed \
  --served-model-name nomic-embed-text
IN_BOUNDARY_EMBEDDING_MODEL=nomic-embed-text

# (b) Prefer a model id that already resolves — BAAI/bge-* and
#     sentence-transformers/all-MiniLM-L6-v2 all do.

# (c) Add a row to app/retrieval/embedding_dimensions.py — one line, and
#     explicitly NO migration and NO re-embed:
#         "nomic-embed-text-v1.5": _entry("nomic-embed-text-v1.5", 768, BASIS_PUBLISHED),
```

Doing none of these is a legitimate choice; it just means you lose the startup dimension check
for that model. Nothing else breaks.

### Context window

`MAX_CHUNK_CHARS = 2000` (verified in `app/retrieval/chunking.py`). At roughly 4 characters per
token that is about 500 tokens — which fits a **512-token** model only just. Code, JSON and
non-Latin text tokenise considerably denser, so a 2000-character chunk of source code can
exceed 512 tokens.

vLLM's behaviour here is a genuine advantage over a silently-truncating server: an input that
exceeds `--max-model-len` is **rejected with an HTTP error** rather than quietly truncated. In
AgentIQ that surfaces as `embed()` returning `[]` for the batch plus a WARNING — visible, and
`pending_embeddings` stops falling. Plan for it:

| Model | Context | For a 2000-char chunk |
|---|---|---|
| `nomic-embed-text` (v1.5 weights) | 8192 tokens | ample margin |
| `bge-large-en-v1.5` | 512 tokens | prose fits; code/JSON may be rejected |
| `bge-base-en-v1.5` / `bge-small-en-v1.5` | 512 tokens | same |
| `all-MiniLM-L6-v2` | 512 tokens (256 trained) | same |
| `mxbai-embed-large` | 512 tokens | same |

If your corpus contains code, prefer an 8192-context model. Raising `--max-model-len` above the
architecture's trained maximum does not add real capacity.

---

## Supported embedding models and their dimensions

The pgvector column is an unqualified `vector` with **no fixed dimension** by design, so
**every model below works today with no schema migration and no re-embed.** Changing model is a
configuration change; the per-vector `(embedding_model, embedding_model_version)` stamp keeps
vectors from different models from ever being compared.

**Single source of truth: [`backend/app/retrieval/embedding_dimensions.py`](../backend/app/retrieval/embedding_dimensions.py)**
(`MODEL_DIMENSIONS`). That table is what HP-2.4's startup check reads via
`declared_dimension()`. The rows below are pinned against it by
`backend/tests/contract/test_hp2_onprem_templates.py`, so this document and the code **cannot
drift**: change one without the other and CI fails.

### Self-hosted — served in-boundary by vLLM, Ollama, or any compatible server

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
change. A model that is absent simply has no declared dimension, so the startup check skips it.

### Changing embedding model on a populated deployment

The dimension check refuses startup when the configured model's declared dimension differs from
what is already stored **under the active model stamp**. Repinning
`IN_BOUNDARY_EMBEDDING_MODEL` to a different model changes the stamp (`in_boundary:<model>` —
verified), so existing vectors become non-active and the R18-B2 backfill converges them in the
background. You do **not** need a migration or a manual re-embed. If you instead hit the
refusal, the stored rows carry the *same* stamp as the model you have configured; the message
names both dimensions and the two remedies.

Note the interaction with `--served-model-name`: the stamp is derived from
`IN_BOUNDARY_EMBEDDING_MODEL`, so **renaming a served model without changing the weights
invalidates the stamp** and triggers a background re-embed of content that did not change.
Pick the served name once and keep it stable.

---

## What startup does with this configuration

| Stage | What happens | On failure under `customer_hosted` |
|---|---|---|
| `DEPLOYMENT_PROFILE` resolved | must be `saas` or `customer_hosted` | unrecognised value → **refuse** |
| Provider names resolved | both variables must be set | either unset → **refuse** (verified #8) |
| `endpoint_configuration` | is there an endpoint at all? | none → **refuse** |
| `credential_presence` | not required for `in_boundary` | never a fault here (verified) |
| `reachability` | one bounded TCP connect **per role** | unreachable → **refuse**, naming the role (verified #7) |
| Embedding dimension | declared vs stored under the active stamp | conflict → **refuse** (both profiles) |

Under `saas` the same conditions are logged and reported unhealthy but never block boot.

**Ordering caveat, and it bites harder with vLLM:** a vLLM process can take minutes to load
weights before it accepts connections. If AgentIQ starts in that window the reachability probe
fails and the process refuses to start. Either gate AgentIQ on both servers' `/health` (a
systemd `After=` plus a readiness wrapper, or a container `depends_on` with a health
condition), or set `MODEL_PROVIDER_PROBE_TIMEOUT_SECONDS=0` to skip reachability probing — at
the cost of losing the check that exists to stop a silently-degraded deployment.

---

## Verify on your deployment

Work top to bottom; each step is cheap and the failures are distinguishable.

- [ ] **1. Both servers are up and admit which model they serve.**
      ```bash
      curl -sS http://vllm-embed.internal:8000/health   # expect 200, empty body
      curl -sS http://vllm-embed.internal:8000/v1/models
      curl -sS http://vllm-gen.internal:8000/v1/models
      ```
      The `id` fields are exactly what the two `IN_BOUNDARY_*_MODEL` variables must contain.
- [ ] **2. The embedding server is genuinely in embedding mode.**
      ```bash
      curl -sS http://vllm-embed.internal:8000/v1/embeddings \
        -H 'Content-Type: application/json' \
        -d '{"model":"BAAI/bge-large-en-v1.5","input":["hello"]}' | head -c 200
      ```
      Expect `{"object":"list","data":[{"object":"embedding","index":0,"embedding":[...`. A
      400/404 here usually means the embedding-mode flag is wrong for your vLLM version — this
      is the item flagged as unverified above.
- [ ] **3. Batch input is accepted.**
      ```bash
      curl -sS http://vllm-embed.internal:8000/v1/embeddings \
        -H 'Content-Type: application/json' \
        -d '{"model":"BAAI/bge-large-en-v1.5","input":["one","two","three"]}' \
        | python -c 'import json,sys; print(len(json.load(sys.stdin)["data"]))'
      ```
      **Must print `3`.** If it prints `1`, set `RETRIEVAL_EMBED_BATCH_SIZE=1`. Leaving it in
      that state indexes **nothing** while every run still completes and reports normally.
- [ ] **4. The dimension matches what the platform declares.**
      ```bash
      curl -sS http://vllm-embed.internal:8000/v1/embeddings \
        -H 'Content-Type: application/json' \
        -d '{"model":"BAAI/bge-large-en-v1.5","input":["hello"]}' \
        | python -c 'import json,sys; print(len(json.load(sys.stdin)["data"][0]["embedding"]))'
      ```
      Must equal the table row for your model (1024 for `bge-large-en-v1.5`).
- [ ] **5. A full-size chunk is accepted, not rejected.** Repeat step 2 with a 2000-character
      input — the platform's maximum chunk size. A 400 here means your `--max-model-len` (or the
      model's own limit) is below what the chunker produces; move to an 8192-context model.
- [ ] **6. Generation responds.**
      ```bash
      curl -sS http://vllm-gen.internal:8000/v1/chat/completions \
        -H 'Content-Type: application/json' \
        -d '{"model":"Qwen/Qwen2.5-7B-Instruct","max_tokens":16,
             "messages":[{"role":"user","content":"reply with OK"}]}' | head -c 300
      ```
      Expect a `choices[0].message.content`.
- [ ] **7. If either server uses `--api-key`, both use the SAME key**, and it is set in
      `IN_BOUNDARY_API_KEY`. Re-run steps 2 and 6 with
      `-H "Authorization: Bearer $KEY"` to confirm.
- [ ] **8. AgentIQ starts.** The startup log records `model_gateway config validated` plus the
      resolved posture. A refusal here is the system working: read the message, it names the
      check, the role, the variable and the host.
- [ ] **9. Health reports both roles.** `GET /api/health` →
      `checks.model_providers.roles.{generation,embedding}.status == "ok"`. With two servers this
      is the fastest way to see which one is down. This is a public route and deliberately
      reports **no endpoint host**.
- [ ] **10. Chunks actually embed.** After a discovery run,
      `GET /api/retrieval/freshness` (analyst+) → `pending_embeddings` should fall toward 0 as
      the background worker drains. A count that never moves with a healthy posture is the
      signature of step 3 or step 5 failing.
- [ ] **11. Retrieval returns something.** A finding's evidence should carry retrieved chunks.
- [ ] **12. Record your versions** in the table at the top of this document.

---

## Troubleshooting

Every row is a failure mode observed during verification or directly implied by verified
behaviour.

| Symptom | Cause | Fix |
|---|---|---|
| Startup: `MissingProviderConfiguration`, naming both variables | `customer_hosted` with a provider variable unset | set **both** `MODEL_GENERATION_PROVIDER` and `MODEL_EMBEDDING_PROVIDER` |
| Startup: `ProviderUnreachable … is not reachable (connection refused)` | that role's vLLM is not up yet, or wrong port | vLLM weight loading takes minutes — gate AgentIQ on `/health`, or set `MODEL_PROVIDER_PROBE_TIMEOUT_SECONDS=0` |
| Startup names only ONE role as unreachable | only that server is down | the roles are probed independently — fix that host only |
| Startup: `InvalidDeploymentProfile` | a typo such as `on_prem` or `customer-hosted` | the vocabulary is closed — use exactly `customer_hosted` |
| Startup: `EmbeddingDimensionMismatch` | the configured model's dimension differs from what is stored under the **active** stamp | repin to the model that produced the stored vectors, or move the model **version** so the backfill can reach those rows |
| `/v1/embeddings` returns 400/404 on a server that otherwise works | the process is in generation mode, not embedding mode | the embedding-mode flag is version-sensitive — check `vllm serve --help` |
| Log: `embedding response count mismatch (texts=64 vectors=1)`, and nothing indexes | the server embedded only the first input | verify step 3; set `RETRIEVAL_EMBED_BATCH_SIZE=1` |
| Log: `HTTP 400`, `embed()` returns `[]`, only on some content | input longer than `--max-model-len` — vLLM rejects rather than truncates | use an 8192-context model, or raise `--max-model-len` within the architecture's real limit |
| Log: `HTTP 404 … model not found` | the configured id is not what the server serves it as | compare against `GET /v1/models`; or pin it with `--served-model-name` |
| Log: `HTTP 401`, `embed()` returns `[]` | a server launched with `--api-key` and no matching `IN_BOUNDARY_API_KEY` | set it (read live — no restart needed); both servers must share one key |
| Log: `embedding skipped: … not configured` | neither a base URL nor the role override is set, or no model name | set the role endpoint and `IN_BOUNDARY_EMBEDDING_MODEL` |
| Retrieval always empty, posture `ok`, `pending_embeddings` stuck | see the count-mismatch and 400 rows above, or `MODEL_EMBEDDING_PROVIDER=hosted` | the hosted provider has no embeddings endpoint |
| Startup dimension check never runs for your model | the configured id has no declared dimension | use `--served-model-name` to serve it under the declared name, or add the one-line row |
| A background re-embed starts after a config-only change | `--served-model-name` was changed, so the vector stamp changed | keep the served name stable across restarts |

---

## Security notes for the reviewer

- **Every model call is outbound-only and stays inside your boundary.** The two URLs you
  configure are the only model endpoints AgentIQ contacts in this posture, and the adapter is
  the only code permitted to contact them — enforced in CI by a scan that fails the build on any
  direct model call outside `backend/app/model_gateway/`.
- **`IN_BOUNDARY_API_KEY` is optional and never logged.** The config object redacts it in
  `repr`, it is resolved live per call rather than cached, and the startup probe reports only
  *presence*, never the value.
- **The startup probe is a TCP connect, not a model call.** No prompt, no cost, one attempt, no
  retries, and it is never re-run by the health endpoint — so a health check can never become a
  lever pointed at your model server.
- **`GET /api/health` publishes no endpoint host.** It is a public route; the host and port stay
  in the startup log.
- **Two servers, one shared key.** If your review requires per-service credentials, front the
  two processes with a proxy that normalises the credential, or rely on network controls.
- **Setting `hosted` under `customer_hosted` remains possible and must be deliberate.** If your
  review requires that nothing leaves the boundary, confirm both provider variables read
  `in_boundary` (or `customer_tenant` pointed inside your tenancy).

## Sign-off

| | Name | Date |
|---|---|---|
| Author | | |
| Reviewer (not the author) | | |
| Validated against real vLLM by | | |
| Notes | | |

---

*Companion template: [`ON_PREM_OLLAMA.md`](./ON_PREM_OLLAMA.md). Adapter:
`backend/app/model_gateway/in_boundary_provider.py` + `in_boundary_config.py`. Dimension table:
`backend/app/retrieval/embedding_dimensions.py`. Drift gate:
`backend/tests/contract/test_hp2_onprem_templates.py`.*
