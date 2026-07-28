# Projection band width — the deterministic rules

**Story:** 2.0-A1 (Intervention Modelling), task 4 — *"Band width from evidence, not taste."*
**Implementation:** `backend/discovery/projection/band_width.py` (single source of truth).
**Acceptance criteria covered:** AC2 (deterministic, documented, thinner evidence ⇒ wider band) and AC4 (capped single-source confidence is labelled and never out-ranks a corroborated equivalent on projection strength alone).

---

## 1. The rule in one sentence

A projection band's width is a pure function of four evidence inputs. It is never a hand-set number, never a per-pack knob, never operator-tunable, and never a taste judgment.

There is deliberately **no** configuration surface — no env var, no config file entry, no `set_band_width(...)`, no width parameter on any public function. Changing a band means changing the evidence or changing this model (and bumping `BAND_WIDTH_MODEL_VERSION`), never editing a number for a demo.

---

## 2. The four inputs

| # | Input | Where it comes from | Classifier |
|---|-------|--------------------|------------|
| 1 | **Sample size** | The observed population behind the finding — the detector's volume field, else its affected-instance count, else the primary metric when that metric is itself a count. A *rate* (ratio/pct) is never treated as a sample size. | `classify_sample_tier` |
| 2 | **Recurrence stability** | The coefficient of variation of the temporal `recent_values` series. | `classify_recurrence_stability` |
| 3 | **Corroboration status** | The ENT-2 corroboration fields on the opportunity (`triple_corroboration`, `corroboration_sources`, `corroboration_rule_ids`). | `classify_corroboration_status` |
| 4 | **Confidence cap status** | True when corroboration cannot elevate confidence (single-source, or a conversation source under the standing MEDIUM ceiling), or when the pipeline already recorded LOW confidence. | `classify_confidence_cap` |

Nothing else is an input. Not the pack, not the tier, not the score, not the analyst's decision, not the run.

### Sample size tiers

| Tier | Observed sample | Penalty |
|------|-----------------|---------|
| `strong` | ≥ 100 | 0.00 |
| `moderate` | ≥ 30 | 0.35 |
| `thin` | ≥ 10 | 0.70 |
| `minimal` | < 10, or not counted at all | 1.00 |

An absent sample is `minimal`, never assumed adequate — the widest band is the honest answer when nothing was counted.

### Recurrence stability tiers

Computed from the coefficient of variation (σ/μ) of the observed series.

| Tier | Condition | Penalty |
|------|-----------|---------|
| `steady` | CV ≤ 0.25 | 0.00 |
| `variable` | CV ≤ 0.60 | 0.50 |
| `bursty` | CV > 0.60 | 1.00 |
| `unknown` | fewer than 3 observations, or a non-positive mean | 0.70 |

`unknown` is penalised heavily but not maximally: absent history is not evidence of instability, and assuming `steady` would narrow a band on evidence that does not exist.

### Corroboration status tiers

| Tier | Meaning | Penalty |
|------|---------|---------|
| `triple` | Three-way cross-system corroboration | 0.00 |
| `corroborated` | At least one elevating source corroborates | 0.30 |
| `supporting_only` | Only a non-elevating (conversation) source corroborates — COR-05 | 0.85 |
| `single_source` | Nothing corroborates the finding — COR-08 | 1.00 |

COR-08 is checked before COR-05: a finding stamped with both stands on one source, and must not be flattered into the stronger `supporting_only` state.

### Confidence cap status

| State | Penalty |
|-------|---------|
| not capped | 0.00 |
| capped | 1.00 |

This axis deliberately **overlaps** the corroboration axis. It is a second, explicit charge for the same weakness, which is what makes a capped finding structurally unable to present a band as narrow as a corroborated equivalent's. That is AC4's requirement, applied to the geometry rather than to a label.

---

## 3. From inputs to a band

```
evidence_penalty = 0.35·sample + 0.25·stability + 0.25·corroboration + 0.15·cap
half_width       = 0.15 + 0.30 · evidence_penalty
low              = max(0.05, 0.40 − half_width)
high             = min(0.90, 0.40 + half_width)
```

* **Weights sum to 1.0**, so `evidence_penalty ∈ [0, 1]` and `half_width ∈ [0.15, 0.45]`.
* Sample size leads because it is the only axis that measures how much was actually observed.
* The confidence cap is smallest because it double-charges the corroboration weakness by design.
* `0.40` is the base midpoint — the share of identified recurring instances an agent is expected to handle when evidence is strong. Well below "all of them": the agent handles the identified cases, the residual requires judgment.
* The band is clamped to `[5%, 90%]` and can never collapse to a point estimate — if rounding would make low equal high, high is bumped by one point.

### Worked examples

| Evidence | Penalty | Band | Width | Tier |
|----------|---------|------|-------|------|
| 400 observed, steady, triple corroborated, uncapped | 0.000 | 25–55% | 30 | narrow |
| 400 observed, steady, corroborated, uncapped | 0.075 | 23–57% | 34 | moderate |
| 400 observed, steady, **single-source (capped)** | 0.400 | 13–67% | 54 | wide |
| 12 observed, bursty, single-source (capped) | 0.885 | 5–82% | 77 | very wide |

The third row is the AC4 case: same sample, same stability, same detector — losing corroboration widens the band from 34 to 54 points, and the finding is additionally labelled.

### Band tiers (from the computed width)

| Tier | Width in points | Label |
|------|-----------------|-------|
| `narrow` | ≤ 32 | Narrow band |
| `moderate` | ≤ 48 | Moderate band |
| `wide` | ≤ 64 | Wide band |
| `very_wide` | > 64 | Very wide band |

Tiers are read from the *computed width*, not from the inputs, so the label can never disagree with the band it describes.

### Evidence label

`evidence_quality = 1 − evidence_penalty`, bucketed:

| Quality | Tier | Label |
|---------|------|-------|
| ≥ 0.75 | `strong` | Strong evidence |
| ≥ 0.50 | `adequate` | Adequate evidence |
| ≥ 0.25 | `limited` | Limited evidence |
| < 0.25 | `thin` | Thin evidence |

Separately, `thinEvidence` is `true` whenever *any* axis is materially weak (thin/minimal sample, bursty/unknown recurrence, or capped confidence). This is a lower bar than the tier: a large sample can carry a capped finding into "adequate" overall while the reason for the extra width still needs saying. When both hold, the label reads e.g. *"Adequate evidence — band widened"*.

---

## 4. Projection strength and the AC4 ordering rule

**Projection strength** is the comparable scalar a surface orders or displays with:

```
strength = evidence_quality, clamped to ≤ 0.50 when confidence is capped
```

AC4 says a capped (single-source) finding "never ranks above a corroborated equivalent on projection strength alone". Two mechanisms enforce it, and both are present on purpose:

1. **Structural** — `projection_rank_key` returns `(capped, −strength)`. Every capped projection sorts after every uncapped one regardless of the scalar. This is what an *ordering* uses.
2. **Numeric** — the scalar itself is clamped to `CAPPED_STRENGTH_CEILING = 0.50`, so a capped projection reads as at most "moderate" strength however large its sample. This is what a *UI* renders.

Either alone would satisfy AC4. Both are present because a rendered number and a sort order must never quietly disagree.

A projection with no band (direction `no_material_change`) has no strength: `value` is `null` and it sorts last within its group rather than being treated as maximally strong.

### Where strength is used

* **Opportunity Review** — displays the band, the band tier, the evidence label, the per-axis drivers, and the capped label. No ordering.
* **Agent Roadmap (Agentforce Blueprint)** — `app/roadmap_engine.py::_apply_projection_strength_rule` applies *only* the capped demotion within each stage. Stage membership stays tier-driven and approved items stay ahead of unreviewed ones: those orderings encode analyst decisions a projection has no business overturning. The sort is stable, so everything else keeps its incoming order.

`order_by_projection_strength` (full strongest-first ordering) exists for callers that genuinely want to rank by strength; the roadmap deliberately uses the conservative `demote_capped_projections` instead.

---

## 5. Determinism and reproducibility (AC2, AC5)

* No clock read, no randomness, no DB access, no LLM, no `app` import.
* Every float on the wire is rounded (4 dp for scalars, 2 dp for driver contributions) so a stored projection compares byte-for-byte with a recomputed one.
* Given the same opportunity record, `build_projection` returns an identical dict in every process, forever.
* `BAND_WIDTH_MODEL_VERSION` is stamped onto every band, and `PROJECTION_SCHEMA_VERSION` onto every projection, so 2.0-A2 can tell "the evidence moved" from "the model moved" when it validates a stored projection against a measured outcome.

**Recalibrating.** When 2.0-A2's real outcomes justify narrowing or widening the model, edit the penalty tables, weights, or geometry in `band_width.py` and bump `BAND_WIDTH_MODEL_VERSION` (and `PROJECTION_SCHEMA_VERSION` if stored bands stop being comparable). Do not add a config knob — the band's honesty depends on it not being adjustable per deployment or per demo.

---

## 6. Vocabulary (AC3)

Every string this model emits is descriptive, never predictive: it explains a *width*, it does not promise a *result*. No band label, evidence label, rationale, driver, or strength label may contain savings or guarantee language ("will save", "will reduce", "will cut", "guarantee(d)", "savings", "ROI", "eliminates", "ensures"). This is enforced by a recursive string sweep over the entire serialized projection in `backend/discovery/tests/test_projection_band_width.py` and on the wire in `backend/tests/contract/test_a1_band_width.py`.

---

## 7. Tests

| Test file | Covers |
|-----------|--------|
| `backend/discovery/tests/test_projection_band_width.py` | Deterministic seeded findings: same input ⇒ same band; thinner evidence ⇒ strictly wider; stronger corroboration ⇒ strictly narrower; capped labelling and strength clamping; per-axis monotonicity; ordering rule; vocabulary sweep. |
| `backend/tests/contract/test_a1_band_width.py` | The same guarantees on the wire, through the real pipeline hook and the API surfaces, plus the roadmap ordering rule. |
| `backend/discovery/tests/test_projection_model.py` | The pre-existing T1–T3 relational band assertions, unchanged. |
| `frontend/src/__tests__/A1_BandWidth.test.tsx` | Opportunity Review renders the band and evidence label; Blueprint renders projection strength with its capped label. |
