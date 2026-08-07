import React, { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { CheckCircle2, Circle, Info, Loader2, RefreshCw, XCircle } from "lucide-react";
import { useLocation, useNavigate } from "react-router-dom";
import { InfoPanel } from "../components/common/InfoPanel";
import LoadingPanel from "../components/common/LoadingPanel";
import { Skeleton } from "../components/common/Skeleton";
import PageShell from "../components/common/PageShell";
import TemplateRunNotice from "../components/discovery_run/TemplateRunNotice";
import { useDiscoveryRunContext } from "../context/DiscoveryRunContext";
import { useConnectorContext } from "../context/ConnectorContext";
import { useSourceIntakeContext } from "../context/SourceIntakeContext";
import { useRunContext } from "../context/RunContext";
import {
  DISCOVERY_SOURCE_REQUIREMENT_MESSAGE,
  isDiscoveryReadyConnector,
} from "../utils/sourceReadiness";
import { apiGet } from "../lib/apiClient";

// ---------------------------------------------------------------------------
// DISCOVERY_STEPS — ordered list of all seven discovery stages (CS-4 T5 AC4).
// Labels and sub-labels match Section 2 of the CS-4 spec.
// ---------------------------------------------------------------------------
interface DiscoveryStep {
  id: string;
  label: string;
  subLabel: string;
  // CS-4 T6 (AT-314): approved explanation shown via an info tooltip on the
  // two Salesforce steps so the customer understands why both passes occur.
  infoTooltip?: string;
  // R191-P1: the backend step id that drives this row's progress/failed state.
  // Multi-pack runs render ONE pack step per declared Salesforce product, all of
  // which reflect the single backend "sf_ncino" (second Salesforce pass) step —
  // so each carries progressStepId="sf_ncino" while keeping a unique row id.
  // Defaults to `id` when unset.
  progressStepId?: string;
}

// CS-4 T6 (AT-314 / Section 4): approved wording — must match exactly.
export const SALESFORCE_DUAL_EXTRACTION_TOOLTIP =
  "AgentIQ reads from your Salesforce system in two passes: the first reads " +
  "CRM signals (Cases, Workflows, Approvals), the second reads nCino lending " +
  "signals (Loans, Covenants, Checklists). These are different datasets " +
  "serving different detectors. Both passes use your authorised read-only " +
  "token and are logged in the audit trail.";

// Order matches the backend runner's update_run_step() emission order
// (backend/discovery/runner.py): Salesforce CRM → ServiceNow → Jira → Slack →
// the pack-specific second Salesforce pass (sf_ncino) → detect → enrich →
// complete. All connected SOURCES (systems of record + conversation sources like
// Slack) are emitted first; the pack-specific second pass (labelled by the
// selected pack — Service Cloud / nCino / etc.) is emitted last among the ingest
// steps, so Discovery Progress shows every connected source before the selected
// pack — consistent with the run log.
const DISCOVERY_STEPS: DiscoveryStep[] = [
  {
    id: "sf_crm",
    label: "Salesforce CRM",
    subLabel: "Ingesting case metrics, flows, and approval data",
    infoTooltip: SALESFORCE_DUAL_EXTRACTION_TOOLTIP,
  },
  {
    id: "sn",
    label: "ServiceNow",
    subLabel: "Ingesting incident metrics and change data",
  },
  {
    id: "jira",
    label: "Jira",
    subLabel: "Ingesting issue metrics and project activity",
  },
  {
    id: "azure_events",
    label: "Azure Events",
    subLabel: "Ingesting Azure alerts, activity log, and service health events",
  },
  {
    id: "aws_events",
    label: "AWS Events",
    subLabel: "Ingesting CloudWatch alarm, EventBridge, and CloudTrail events",
  },
  {
    id: "slack",
    label: "Slack",
    subLabel: "Ingesting channel activity, escalation, and cross-reference signals",
  },
  {
    id: "teams",
    label: "Microsoft Teams",
    subLabel: "Ingesting channel activity, escalation, and cross-reference signals",
  },
  {
    id: "confluence",
    label: "Confluence",
    subLabel: "Ingesting page and space activity signals",
  },
  {
    id: "sharepoint",
    label: "SharePoint",
    subLabel: "Ingesting document library activity signals",
  },
  {
    id: "github",
    label: "GitHub",
    subLabel: "Ingesting pull request, commit, and branch signals",
  },
  {
    id: "java_app",
    label: "Java Application",
    subLabel: "Ingesting operational health and log signals",
  },
  {
    id: "dotnet_app",
    label: ".NET Application",
    subLabel: "Ingesting operational health and log signals",
  },
  {
    id: "sf_ncino",
    label: "nCino Lending",
    subLabel: "Ingesting nCino loan origination signals",
    infoTooltip: SALESFORCE_DUAL_EXTRACTION_TOOLTIP,
  },
  // The FSC second Salesforce pass has its own backend step (the runner emits
  // "sf_fsc" after fsc_ingest()). It exists here only to give that step a
  // canonical position in STEP_INDEX — the visible row is built by
  // buildPackSteps() from the declared product, so it is a PACK step and is
  // never rendered from this canonical list (see PACK_STEP_IDS).
  {
    id: "sf_fsc",
    label: "Financial Services Cloud",
    subLabel: "Ingesting wealth management and relationship banking signals",
  },
  {
    id: "detect",
    label: "Pattern Detection",
    subLabel: "Running detectors across all ingested signals",
  },
  {
    id: "enrich",
    label: "Entity Enrichment",
    subLabel: "Extracting entities and mapping relationships",
  },
  {
    id: "complete",
    label: "Complete",
    subLabel: "Discovery run finished successfully",
  },
];

const STEP_INDEX = Object.fromEntries(
  DISCOVERY_STEPS.map((s, i) => [s.id, i])
);

// ---------------------------------------------------------------------------
// Connected-source → discovery-step mapping (dynamic progress).
//
// The Discovery Progress list is NOT a fixed pipeline: it shows a stage for
// every source actually connected for the run (the same set surfaced in the
// Discovery Log / Run Summary), plus the always-present processing stages
// (Pattern Detection → Entity Enrichment → Complete). A source that is not
// connected is not shown.
//
// SOURCE_STEP_IDS are the pipeline steps that correspond to an ingested source
// (everything except the processing stages). STEP_SOURCE_TOKENS maps each such
// step to the connector id/name token(s) that mean "this source is connected".
// Salesforce drives two passes (CRM + the declared second product), so both
// sf_crm and sf_ncino map to the salesforce source.
// ---------------------------------------------------------------------------
const SOURCE_STEP_IDS = new Set([
  "sf_crm",
  "sn",
  "jira",
  "sf_ncino",
  "sf_fsc",
  "slack",
  "teams",
  "confluence",
  "sharepoint",
  "github",
  "java_app",
  "dotnet_app",
  "azure_events",
  "aws_events",
]);

// The pack-specific second Salesforce pass (labelled by the selected pack —
// Service Cloud / nCino / …). It is a source step, but it is rendered LAST among
// the source stages — after every connected source — so the progress list reads
// "all connected sources → the selected pack".
const PACK_STEP_IDS = new Set(["sf_ncino", "sf_fsc"]);

const STEP_SOURCE_TOKENS: Record<string, string[]> = {
  sf_crm: ["salesforce"],
  sf_ncino: ["salesforce"],
  sf_fsc: ["salesforce"],
  sn: ["servicenow"],
  jira: ["jira"],
  slack: ["slack"],
  teams: ["teams"],
  confluence: ["confluence"],
  sharepoint: ["sharepoint"],
  github: ["github"],
  java_app: ["java_app"],
  dotnet_app: ["dotnet_app"],
  // The native cloud event connectors arrive either as the run's system ids
  // ("azure_events") or as display labels ("Azure Events"); normalizeSource only
  // lower-cases, so both spellings are listed.
  azure_events: ["azure_events", "azure events"],
  aws_events: ["aws_events", "aws events"],
};

// Normalise a connected-source label/id for matching: lower-cased, trimmed.
// Connected sources arrive either as connector ids ("salesforce", "servicenow")
// or display names ("Salesforce", "ServiceNow", "Microsoft Teams"); lower-casing
// reconciles both for the single-word source ids we map.
function normalizeSource(value: string): string {
  return (value ?? "").trim().toLowerCase();
}

// Proper display names for connectors rendered as generic source stages, so a
// raw lower-cased id ("teams", "github") shows with correct branding/casing
// (e.g. "Salesforce CRM" alongside) rather than "teams" / "github".
const SOURCE_DISPLAY_NAMES: Record<string, string> = {
  github: "GitHub",
  teams: "Microsoft Teams",
  slack: "Slack",
  aws_events: "AWS Events",
  "aws events": "AWS Events",
  azure_events: "Azure Events",
  "azure events": "Azure Events",
};

// Human-friendly label for a connected source: a known brand name, else the raw
// value Title-Cased (so the first letter is always capitalised).
function prettySourceLabel(value: string): string {
  const n = normalizeSource(value);
  if (SOURCE_DISPLAY_NAMES[n]) return SOURCE_DISPLAY_NAMES[n];
  return (value ?? "")
    .trim()
    .split(/[\s_-]+/)
    .filter(Boolean)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

// Reverse map: connector token → the known NON-pack source step it drives, so
// the progress list can place each connected source at its own position in the
// connected-source order (see DiscoveryStepList). Salesforce is intentionally
// excluded here — it drives two passes (sf_crm + the pack second pass), so it is
// mapped explicitly to sf_crm at its ordered position and the pack pass is
// appended after every connected source.
const KNOWN_TOKEN_STEP_ID: Record<string, string> = (() => {
  const out: Record<string, string> = {};
  for (const [stepId, tokens] of Object.entries(STEP_SOURCE_TOKENS)) {
    if (PACK_STEP_IDS.has(stepId)) continue;
    for (const t of tokens) {
      if (t === "salesforce") continue; // handled explicitly as sf_crm
      out[t] = stepId;
    }
  }
  return out;
})();

// Parse the ordered connector list out of the Discovery Log CONNECT event
// message ("Using authenticated connectors: a, b, c" for live runs, or
// "Connected sources: a, b, c" offline). Returns the tokens in log order, or
// null when no CONNECT event is present yet. This is the authoritative order the
// Discovery Progress list mirrors so the two views agree exactly.
export function parseConnectOrder(
  events: { stage?: string; message?: string }[]
): string[] | null {
  const connectEvt = events.find(
    (e) => (e.stage ?? "").trim().toUpperCase() === "CONNECT"
  );
  const msg = connectEvt?.message ?? "";
  const m = msg.match(
    /(?:authenticated connectors|connected sources)\s*:\s*(.+)$/i
  );
  if (!m) return null;
  const tokens = m[1]
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  return tokens.length ? tokens : null;
}

// Reorder a connected-source list so it follows the Discovery Log CONNECT order.
// Sources present in the log are sorted by their log position; any source not in
// the log (e.g. Salesforce/Slack surfaced from the run record but omitted from
// the live-connector log line) keeps its original relative order and follows the
// logged sources. Salesforce product variants share the "salesforce" log rank.
export function orderSourcesByConnectLog(
  sources: string[],
  connectOrder: string[] | null
): string[] {
  if (!connectOrder || connectOrder.length === 0) return sources;
  const rank = new Map<string, number>();
  connectOrder.forEach((tok, i) => {
    const n = normalizeSource(tok);
    if (!rank.has(n)) rank.set(n, i);
  });
  const rankOf = (s: string): number => {
    const n = normalizeSource(s);
    if (rank.has(n)) return rank.get(n)!;
    if (n.startsWith("salesforce") && rank.has("salesforce")) {
      return rank.get("salesforce")!;
    }
    return Number.MAX_SAFE_INTEGER;
  };
  return sources
    .map((s, i) => ({ s, i }))
    .sort((a, b) => {
      const ra = rankOf(a.s);
      const rb = rankOf(b.s);
      return ra !== rb ? ra - rb : a.i - b.i; // stable within equal ranks
    })
    .map((x) => x.s);
}

// CS-4: the second Salesforce pass ("sf_ncino" step) reflects the Salesforce
// product the workspace declared in Integration Hub (SalesforceProductPicker).
// The step id stays "sf_ncino" so backend current_step progress mapping is
// unaffected — only the user-facing label/sub-label change. nCino keeps the
// dual-extraction tooltip narrative; for any other product that nCino-specific
// explanation no longer applies and is dropped from both Salesforce steps.
// `tooltipDataset` is the phrase describing the SECOND extraction pass; it is
// slotted into the dual-extraction tooltip so both Salesforce steps explain the
// two passes in terms of the declared product.
const SF_SECOND_PASS_BY_PRODUCT: Record<
  string,
  { label: string; subLabel: string; tooltipDataset: string }
> = {
  salesforce_ncino: {
    label: "nCino Lending",
    subLabel: "Ingesting nCino loan origination signals",
    tooltipDataset: "nCino lending signals (Loans, Covenants, Checklists)",
  },
  salesforce_sc: {
    label: "Service Cloud",
    subLabel: "Ingesting case management, service request, and SLA signals",
    tooltipDataset: "Service Cloud signals (Case management, service requests, SLAs)",
  },
  salesforce_pss: {
    label: "Public Sector Solutions / Benefits",
    subLabel: "Ingesting benefits administration and member service signals",
    tooltipDataset:
      "Public Sector Solutions signals (Benefits administration, member services, PSS objects)",
  },
  salesforce_fsc: {
    label: "Financial Services Cloud",
    subLabel: "Ingesting wealth management and relationship banking signals",
    tooltipDataset:
      "Financial Services Cloud signals (Wealth management, relationship banking)",
  },
  salesforce_rc: {
    label: "Revenue Cloud",
    subLabel: "Ingesting CPQ, contract, and revenue operation signals",
    tooltipDataset: "Revenue Cloud signals (CPQ, contracts, revenue operations)",
  },
  salesforce_hc: {
    label: "Health Cloud",
    subLabel: "Ingesting patient management and care programme signals",
    tooltipDataset: "Health Cloud signals (Patient management, care programmes)",
  },
};

// Compose the dual-extraction explanation for a given second-pass dataset.
// For nCino this reproduces SALESFORCE_DUAL_EXTRACTION_TOOLTIP verbatim
// (AT-314 approved wording), so that constant stays the single source of truth.
function buildDualExtractionTooltip(tooltipDataset: string): string {
  return (
    "AgentIQ reads from your Salesforce system in two passes: the first reads " +
    "CRM signals (Cases, Workflows, Approvals), the second reads " +
    tooltipDataset +
    ". These are different datasets serving different detectors. Both passes " +
    "use your authorised read-only token and are logged in the audit trail."
  );
}

// Build the display step list for a declared Salesforce product. When no
// product is declared (undefined) the default nCino labels/tooltip are used so
// existing behaviour and tests are unchanged. Both Salesforce steps always
// carry the dual-extraction tooltip, worded for the declared product.
function resolveDiscoverySteps(salesforceProduct?: string): DiscoveryStep[] {
  const productId =
    salesforceProduct && SF_SECOND_PASS_BY_PRODUCT[salesforceProduct]
      ? salesforceProduct
      : "salesforce_ncino";
  const meta = SF_SECOND_PASS_BY_PRODUCT[productId];
  const tooltip =
    productId === "salesforce_ncino"
      ? SALESFORCE_DUAL_EXTRACTION_TOOLTIP
      : buildDualExtractionTooltip(meta.tooltipDataset);

  return DISCOVERY_STEPS.map((step) => {
    if (step.id === "sf_crm") {
      return { ...step, infoTooltip: tooltip };
    }
    if (step.id === "sf_ncino") {
      return {
        ...step,
        label: meta.label,
        subLabel: meta.subLabel,
        infoTooltip: tooltip,
      };
    }
    return step;
  });
}

// R191-P1: a Salesforce workspace can declare MULTIPLE products, each mapping to
// a discovery pack that runs in one discovery run. Build one "second Salesforce
// pass" step PER declared product (e.g. Service Cloud AND nCino Lending), so the
// Discovery Progress reflects every pack the run activates — not just the first.
// Every such row reflects the single backend "sf_ncino" step (progressStepId),
// but keeps a unique row id. An empty declaration falls back to the nCino default
// (unchanged single-pack behaviour + existing tests).
// The backend step each declared product's second Salesforce pass is driven by.
// Most products are read by the nCino-shaped second pass ("sf_ncino"); Financial
// Services Cloud has its OWN runner step ("sf_fsc", emitted after fsc_ingest()),
// so its row must track that step rather than borrowing nCino's.
const PACK_PROGRESS_STEP_BY_PRODUCT: Record<string, string> = {
  salesforce_fsc: "sf_fsc",
};

function buildPackSteps(products: string[]): DiscoveryStep[] {
  const declared = products.length > 0 ? products : ["salesforce_ncino"];
  const seen = new Set<string>();
  const out: DiscoveryStep[] = [];
  for (const product of declared) {
    const productId = SF_SECOND_PASS_BY_PRODUCT[product]
      ? product
      : "salesforce_ncino";
    if (seen.has(productId)) continue; // one row per distinct product
    seen.add(productId);
    const meta = SF_SECOND_PASS_BY_PRODUCT[productId];
    const tooltip =
      productId === "salesforce_ncino"
        ? SALESFORCE_DUAL_EXTRACTION_TOOLTIP
        : buildDualExtractionTooltip(meta.tooltipDataset);
    out.push({
      id: `sf_pack:${productId}`,
      label: meta.label,
      subLabel: meta.subLabel,
      infoTooltip: tooltip,
      progressStepId: PACK_PROGRESS_STEP_BY_PRODUCT[productId] ?? "sf_ncino",
    });
  }
  return out;
}

// ---------------------------------------------------------------------------
// StepInfoTooltip — accessible info tooltip for a discovery step (CS-4 T6).
// Renders a keyboard-focusable info icon. The explanation shows on hover and
// on keyboard focus, is exposed to assistive tech via aria-label, and is
// linked to the trigger through aria-describedby (role="tooltip"). The native
// title attribute provides a fallback hover/tap tooltip.
// ---------------------------------------------------------------------------
export function StepInfoTooltip({ text }: { text: string }) {
  const tooltipId = useId();

  return (
    <span className="group relative inline-flex">
      <button
        type="button"
        aria-label={text}
        aria-describedby={tooltipId}
        title={text}
        className="inline-flex h-4 w-4 items-center justify-center rounded-full text-muted/70 transition-colors hover:text-accent focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/60"
      >
        <Info size={14} aria-hidden="true" />
      </button>
      <span
        role="tooltip"
        id={tooltipId}
        className="pointer-events-none absolute left-1/2 top-full z-10 mt-2 w-72 -translate-x-1/2 rounded-md border border-border bg-bg/95 p-2 text-xs leading-4 text-text opacity-0 shadow-lg transition-opacity duration-150 group-hover:opacity-100 group-focus-within:opacity-100"
      >
        {text}
      </span>
    </span>
  );
}

// ---------------------------------------------------------------------------
// buildDiscoverySteps — the RENDERED progress rows for a run.
//
// Extracted out of DiscoveryStepList so the status pill's PERCENTAGE and the
// checklist derive from the same array. The percentage means "how many of these
// rows are done", which is only true if both sides agree on what the rows are —
// previously the percentage divided by the canonical 14-step pipeline while the
// list rendered a different (connected-source-driven) set, so the number and the
// green checks disagreed.
// ---------------------------------------------------------------------------
export function buildDiscoverySteps({
  salesforceProduct,
  salesforceProducts,
  connectedSources,
}: {
  // Declared Salesforce product id (e.g. "salesforce_sc"). Drives the label of
  // the second Salesforce pass so the run reflects the workspace's declaration.
  salesforceProduct?: string;
  // R191-P1: the FULL declared Salesforce product list. When the workspace
  // declares several products (a multi-pack run), each gets its own pack step in
  // the progress list. Falls back to `salesforceProduct` (single) when absent.
  salesforceProducts?: string[];
  // The sources actually connected for this run (connector ids/names — the same
  // set shown in the Discovery Log / Run Summary). When provided, the progress
  // list shows a stage for each connected source (plus the processing stages)
  // and omits unconnected sources. When omitted (undefined), every known source
  // stage is shown — the legacy fixed-pipeline behaviour.
  connectedSources?: string[];
}): DiscoveryStep[] {
  // The declared Salesforce products driving the per-pack progress steps: the
  // full list when provided, else the single legacy prop.
  const declaredProducts =
    salesforceProducts && salesforceProducts.length > 0
      ? salesforceProducts
      : salesforceProduct
      ? [salesforceProduct]
      : [];
  const canonical = resolveDiscoverySteps(declaredProducts[0] ?? salesforceProduct);
  const connectedProvided = connectedSources !== undefined;

  const byId: Record<string, DiscoveryStep> = Object.fromEntries(
    canonical.map((s) => [s.id, s])
  );

  // 1. Source stages, in the SAME order as the connected-source list (which the
  //    caller aligns to the Discovery Log CONNECT order — see DiscoveryRunPage).
  //    Each connected source becomes one stage at its own position: a known
  //    stage (ServiceNow / Jira / Slack / Salesforce CRM) when the token maps to
  //    one, otherwise a generic catch-all stage. Salesforce drives two passes —
  //    its CRM stage sits at the salesforce position here and the pack-specific
  //    second pass is appended AFTER every connected source (below).
  const sourceSteps: DiscoveryStep[] = [];
  const seenSource = new Set<string>();
  let salesforceConnected = false;
  if (connectedProvided) {
    for (const src of connectedSources ?? []) {
      const n = normalizeSource(src);
      if (!n) continue;
      if (n === "salesforce" || n.startsWith("salesforce")) {
        // Any Salesforce product variant → a single CRM stage at this position.
        if (!salesforceConnected) {
          salesforceConnected = true;
          sourceSteps.push(byId["sf_crm"]);
        }
        continue;
      }
      if (seenSource.has(n)) continue; // de-dupe repeated tokens
      seenSource.add(n);
      const knownId = KNOWN_TOKEN_STEP_ID[n];
      if (knownId && byId[knownId]) {
        sourceSteps.push(byId[knownId]);
      } else {
        const label = prettySourceLabel(src);
        sourceSteps.push({
          id: `src:${n}`,
          label,
          subLabel: `Ingesting ${label} signals`,
        });
      }
    }
  } else {
    // Legacy (no connected-source list): show every known non-pack source stage
    // in canonical backend-emission order.
    for (const s of canonical) {
      if (SOURCE_STEP_IDS.has(s.id) && !PACK_STEP_IDS.has(s.id)) {
        sourceSteps.push(s);
      }
    }
    salesforceConnected = true; // legacy always shows the pack stage
  }

  // 2. Pack-specific second Salesforce pass(es) — appended AFTER every connected
  //    source ("all connected sources → selected packs"). One row per declared
  //    Salesforce product (a multi-pack run shows Service Cloud AND nCino, etc.).
  //    Shown only when Salesforce is connected (or in the legacy all-stages mode).
  const packSteps = salesforceConnected ? buildPackSteps(declaredProducts) : [];

  // 3. Processing stages — always shown, in canonical order.
  const processingSteps = canonical.filter((s) => !SOURCE_STEP_IDS.has(s.id));

  return [...sourceSteps, ...packSteps, ...processingSteps];
}

// ---------------------------------------------------------------------------
// computeStepStates — the SEQUENTIAL state machine for the rendered rows.
//
// Invariant (the reason this is one pass over the whole list rather than a
// per-row calculation): the states always read as a prefix of settled rows
// (completed / failed), then AT MOST ONE active row (the frontier), then pending
// rows. Row state used to be computed independently per row, which let several
// rows spin at once — every generic source row (a connected source with no
// backend step of its own, e.g. AWS/Azure Events before they had one) shared the
// single "active until detection starts" rule and so all spun together with
// whichever known step was genuinely running, and every pack row of a multi-pack
// run shared one progressStepId and so spun together too.
//
// The number of settled rows is the number of pipeline steps the backend has
// genuinely passed, so the checklist can never show more progress than the run
// has actually made. Which ROW carries the spinner is positional: when the
// rendered source order (the Discovery Log CONNECT order) differs from the
// backend's ingest order, the spinner marks the n-th stage rather than naming the
// exact connector. A row with no backend step of its own (a generic source)
// sequences by pipeline position — inferred, but monotone; that inference is the
// trade for sequencing, and the rule it replaces (settled only once detection
// starts) is what put every generic row in a spinner simultaneously.
// ---------------------------------------------------------------------------
export type DiscoveryStepStateName =
  | "completed"
  | "active"
  | "failed"
  | "pending";

export interface DiscoveryStepState {
  step: DiscoveryStep;
  state: DiscoveryStepStateName;
}

export function computeStepStates({
  steps,
  currentStep,
  runComplete = false,
  failedSteps = [],
}: {
  steps: DiscoveryStep[];
  currentStep: string | null;
  runComplete?: boolean;
  failedSteps?: string[];
}): DiscoveryStepState[] {
  const activeIdx =
    currentStep != null ? (STEP_INDEX[currentStep] ?? -1) : -1;
  const failed = new Set(failedSteps);
  // The backend step id that drives a row — pack rows point at their product's
  // pass (sf_ncino / sf_fsc) via progressStepId, other rows use their own id.
  const stateIdOf = (step: DiscoveryStep) => step.progressStepId ?? step.id;

  // Per-row gate: the canonical step index the run must pass for that row to be
  // settled. A row with no backend step of its own (a generic source) inherits
  // the highest gate before it, so it settles when the pipeline passes the
  // previous known source.
  let prevGate = 0;
  const gates = steps.map((step) => {
    const ownIdx = STEP_INDEX[stateIdOf(step)];
    if (ownIdx === undefined) return prevGate;
    prevGate = Math.max(ownIdx, prevGate);
    return ownIdx;
  });

  // The frontier is derived from HOW MANY rows the run has passed, not from where
  // they sit in the list. Counting matters because the rendered source order is
  // the Discovery Log CONNECT order, which need not match the backend's ingest
  // order: painting "the first unsettled row" would then leave two consecutive
  // backend steps pointing at the same row, stalling the checklist and the
  // percentage for a stage. Counting advances the frontier exactly once per step
  // the backend passes, while the prefix stays contiguous by construction.
  //
  // The terminal "complete" row can never be settled by activeIdx (nothing
  // follows it), so it greens only via runComplete — while the run is still going
  // it shows the spinner even if the backend already emitted "complete".
  let frontier = 0;
  for (let i = 0; i < steps.length; i += 1) {
    if (failed.has(stateIdOf(steps[i])) || activeIdx > gates[i]) frontier += 1;
  }

  return steps.map((step, i) => {
    // A failed ingest takes precedence over every other state: it must never
    // show the completed green check, even after the run advances past it.
    if (failed.has(stateIdOf(step))) return { step, state: "failed" };
    // The run has finished (materialised): every non-failed stage is done.
    // Authoritative over activeIdx — the backend's last-seen current_step can be
    // stale or point at an early stage for an already-finished run, so a
    // finished run must never show a spinner or a pending circle.
    if (runComplete) return { step, state: "completed" };
    if (i < frontier) return { step, state: "completed" };
    // Nothing has been emitted yet (no current_step): the whole list is pending
    // rather than showing a spinner on a stage that has not started.
    if (i === frontier) {
      return { step, state: activeIdx >= 0 ? "active" : "pending" };
    }
    return { step, state: "pending" };
  });
}

// ---------------------------------------------------------------------------
// stepProgressPercent — the run percentage, divided EQUALLY across the rendered
// rows: each row is worth 100/N. The in-progress row counts as half a row so a
// run that has just started does not sit at "Running (0%)" for the whole of its
// first stage. Clamped to 99 while the run is still going — 100 belongs to a
// finished run only.
// ---------------------------------------------------------------------------
export function stepProgressPercent(states: DiscoveryStepState[]): number {
  if (states.length === 0) return 0;
  let credit = 0;
  for (const { state } of states) {
    if (state === "completed" || state === "failed") credit += 1;
    else if (state === "active") credit += 0.5;
  }
  return Math.min(Math.round((credit / states.length) * 100), 99);
}

// ---------------------------------------------------------------------------
// DiscoveryStepList — renders all steps with completed / active / pending state
// ---------------------------------------------------------------------------
export function DiscoveryStepList({
  currentStep,
  runComplete = false,
  salesforceProduct,
  salesforceProducts,
  failedSteps = [],
  connectedSources,
}: {
  currentStep: string | null;
  // True only once the discovery run has truly finished (100%). The backend can
  // emit the "complete" step while the run is still computing post-processing,
  // so the terminal step's green tick is gated on this flag, not on currentStep.
  runComplete?: boolean;
  salesforceProduct?: string;
  salesforceProducts?: string[];
  // Step ids whose ingest failed (from the run status' failed_steps). A failed
  // step is rendered with an error icon, never as a completed green check — so
  // a failed stage is not misrepresented as successful (CS-4 / AT-313).
  failedSteps?: string[];
  connectedSources?: string[];
}) {
  const steps = buildDiscoverySteps({
    salesforceProduct,
    salesforceProducts,
    connectedSources,
  });
  const states = computeStepStates({
    steps,
    currentStep,
    runComplete,
    failedSteps,
  });

  return (
    <ol className="space-y-3">
      {states.map(({ step, state }) => {
        const isFailed = state === "failed";
        const isCompleted = state === "completed";
        const isActive = state === "active";

        return (
          <li key={step.id} className="flex items-start gap-3">
            {/* Icon */}
            <div className="mt-0.5 shrink-0">
              {isFailed ? (
                <XCircle
                  size={20}
                  className="text-red-400"
                  aria-label="failed"
                />
              ) : isCompleted ? (
                <CheckCircle2
                  size={20}
                  className="text-emerald-400"
                  aria-label="completed"
                />
              ) : isActive ? (
                <Loader2
                  size={20}
                  className="animate-spin text-accent"
                  aria-label="active"
                />
              ) : (
                <Circle
                  size={20}
                  className="text-muted/40"
                  aria-label="pending"
                />
              )}
            </div>

            {/* Labels */}
            <div className="min-w-0">
              <div
                className={`flex items-center gap-1.5 text-sm font-semibold leading-5 ${
                  isFailed
                    ? "text-red-300"
                    : isCompleted
                      ? "text-emerald-300"
                      : isActive
                        ? "text-text"
                        : "text-muted/60"
                }`}
              >
                <span>{step.label}</span>
                {step.infoTooltip && (
                  <StepInfoTooltip text={step.infoTooltip} />
                )}
              </div>
              <div
                className={`text-xs leading-4 ${
                  isFailed
                    ? "text-red-300/80"
                    : isCompleted || isActive
                      ? "text-muted"
                      : "text-muted/40"
                }`}
              >
                {isFailed ? `${step.subLabel} — failed` : step.subLabel}
              </div>
            </div>
          </li>
        );
      })}
    </ol>
  );
}

function RunStatusPill({
  computing,
  isComplete,
  displayPct,
  status,
}: {
  computing: boolean;
  isComplete: boolean;
  displayPct: number;
  status?: string;
}) {
  const normalizedStatus = status?.toLowerCase();
  const isPartial = normalizedStatus === "partial";
  const isFinished = isComplete || isPartial;
  const label = computing
    ? `Running (${displayPct}%)`
    : isFinished
      ? "Completed 100%"
        : (status ?? "-");
  const cls = computing
    ? "border-accent/40 bg-accent/10 text-blue-100"
    : isFinished
      ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-200"
        : "border-border bg-bg/30 text-muted";
  const dotCls = computing
    ? "bg-accent"
    : isFinished
      ? "bg-emerald-400"
        : "bg-muted/50";

  return (
    <span
      className={`inline-flex h-7 items-center gap-2 whitespace-nowrap rounded-full border px-3 text-[13px] font-semibold leading-none align-middle ${cls}`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${dotCls}`} />
      {label}
    </span>
  );
}

function ComputingPill() {
  return (
    <span className="inline-flex h-7 items-center gap-2 rounded-full border border-accent/30 bg-accent/10 px-3 text-[13px] font-semibold leading-none text-blue-100">
      <Loader2
        size={14}
        strokeWidth={2.5}
        className="shrink-0 animate-spin text-accent"
      />
      <span>Computing</span>
    </span>
  );
}

function SourceIntelligenceReadyPill() {
  return (
    <span className="inline-flex h-7 items-center gap-2 rounded-full border border-accent/30 bg-accent/10 px-3 text-[13px] font-semibold leading-none text-blue-100">
      <span className="h-1.5 w-1.5 rounded-full bg-accent" />
      Source Intelligence ready
    </span>
  );
}

function formatRunTimestamp(value?: string | null) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function formatRunMessage(message?: string | null) {
  return (message ?? "").replace(/(\.\.\.|…)$/, "");
}

export default function DiscoveryRunPage() {
  const [autoScroll, setAutoScroll] = useState(true);
  const [logHasOverflow, setLogHasOverflow] = useState(false);
  const logScrollRef = useRef<HTMLDivElement | null>(null);
  const nav = useNavigate();
  const location = useLocation();
  const autoStartRequested =
    (location.state as { autoStart?: boolean } | null)?.autoStart === true;
  const { runId } = useRunContext();
  const { connectors } = useConnectorContext();
  const { uploadedFiles } = useSourceIntakeContext(); // T41-8: sampleWorkspaceEnabled removed

  const {
    run,
    events,
    loading,
    error,
    started,
    computing,
    currentStep,
    failedSteps,
    startRun,
    refetch,
    refresh,
    refreshing,
  } = useDiscoveryRunContext();

  // current_step / failed_steps now come from DiscoveryRunContext's single
  // /status poll (no separate page-level poller) — see the context provider.

  // CS-4: the declared Salesforce product (from Integration Hub) decides what
  // the second Salesforce discovery pass is labelled as. Single declaration
  // (radio), so the first declared id wins. Failure → undefined → default copy.
  const [salesforceProduct, setSalesforceProduct] = useState<string | undefined>(
    undefined
  );
  // R191-P1: the FULL declared Salesforce product list — each drives its own pack
  // step so a multi-pack run (e.g. Service Cloud + nCino) shows both in progress.
  const [salesforceProducts, setSalesforceProducts] = useState<string[]>([]);

  useEffect(() => {
    let cancelled = false;
    apiGet<{ ok: boolean; products: string[]; labels: string[] }>(
      "/api/connectors/salesforce/products"
    )
      .then((data) => {
        if (cancelled) return;
        const products = Array.isArray(data?.products) ? data.products : [];
        setSalesforceProducts(products);
        setSalesforceProduct(products[0]); // primary, for backward-compat labels
      })
      .catch(() => {
        // Non-blocking: with no declaration the default nCino copy is used.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const status = run?.status?.toLowerCase();
  const isMaterialized =
    status === "complete" || status === "completed" || status === "partial";
  const isComplete = status === "complete" || status === "completed";
  const runScopedPath = (path: string) =>
    runId ? `${path}?runId=${runId}` : path;

  const inputs = useMemo(() => {
    const connectedSources = connectors
      .filter(isDiscoveryReadyConnector)
      .map((c) => c.name);
    return {
      connectedSources,
      uploadedFiles: uploadedFiles.map((f) => f.name),
      sampleWorkspaceEnabled: false,
      mode: "live" as const,
    };
  }, [connectors, uploadedFiles]); // T41-8: sampleWorkspaceEnabled removed

  // Fix Pack Sprint 7: read connected sources from run record when available.
  // Stack Builder runs store selectedSystemIds on the run record via the
  // launch endpoint (routes_stack_builder_launch.py). Use those system IDs
  // as the display source list so Discovery Run shows the actual systems
  // used — not Integration Hub connector status (which shows None when
  // systems are not yet authorized in Integration Hub).
  const runSelectedSystems: string[] = run?.selectedSystemIds ?? [];
  const summaryInputs = useMemo(() => {
    if (runSelectedSystems.length > 0) {
      return {
        connectedSources: runSelectedSystems,
        uploadedFiles: uploadedFiles.map((f) => f.name),
        sampleWorkspaceEnabled: false,
        mode: "live" as const,
      };
    }
    return run?.inputs ?? inputs;
  }, [run, runSelectedSystems, inputs, uploadedFiles]);

  // Discovery Progress mirrors the Discovery Log CONNECT order exactly: parse the
  // ordered connector list out of the CONNECT event and reorder the run's
  // connected sources to match it, so the progress stages appear in the same
  // order the log lists the connected systems. Falls back to the summary order
  // until the CONNECT event has been logged.
  const connectLogOrder = useMemo(() => parseConnectOrder(events), [events]);
  const progressConnectedSources = useMemo(
    () =>
      orderSourcesByConnectLog(
        summaryInputs.connectedSources,
        connectLogOrder
      ),
    [summaryInputs.connectedSources, connectLogOrder]
  );

  // The rendered progress rows — built ONCE here and shared with the checklist
  // below, so the percentage counts exactly the rows the user can see.
  const progressSteps = useMemo(
    () =>
      buildDiscoverySteps({
        salesforceProduct,
        salesforceProducts,
        connectedSources: progressConnectedSources,
      }),
    [salesforceProduct, salesforceProducts, progressConnectedSources]
  );

  // Percentage is derived from the run's current_step (the SAME signal that
  // drives the Discovery Progress checklist, so the number and the green-checked
  // steps always agree) divided EQUALLY across the rendered rows — each row is
  // worth 100/N. It previously divided by the canonical 14-step pipeline instead,
  // which bore no relation to the rows actually on screen.
  //
  // Note this is a step-count, not an animation: the step signal changes a
  // handful of times per run, so the number updates only when progress really
  // moves (an earlier version animated 0→99 one integer at a time through a
  // requestAnimationFrame chain — ~99 whole-page re-renders).
  const displayPct = useMemo(() => {
    if (isComplete) return 100;
    if (!computing) return 0;
    return stepProgressPercent(
      computeStepStates({
        steps: progressSteps,
        currentStep,
        runComplete: false,
        failedSteps,
      })
    );
  }, [isComplete, computing, progressSteps, currentStep, failedSteps]);

  const hasAtLeastOneSource =
    inputs.connectedSources.length > 0 ||
    inputs.uploadedFiles.length > 0; // T41-8: sampleWorkspaceEnabled removed

  const pageDescription =
    "Monitor discovery progress, live logs, and the run summary for connected sources and uploaded files.";

  useEffect(() => {
    if (!runId && autoStartRequested && !loading && hasAtLeastOneSource) {
      void startRun(inputs);
    }
  }, [
    runId,
    autoStartRequested,
    loading,
    startRun,
    inputs,
    hasAtLeastOneSource,
  ]);

  useEffect(() => {
    if (!autoScroll) return;
    const el = logScrollRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [events, autoScroll]);

  const updateLogOverflow = useCallback(() => {
    const el = logScrollRef.current;
    if (!el) {
      setLogHasOverflow(false);
      return;
    }
    setLogHasOverflow(el.scrollHeight > el.clientHeight + 1);
  }, []);

  useEffect(() => {
    updateLogOverflow();
  }, [events, updateLogOverflow]);

  useEffect(() => {
    const el = logScrollRef.current;
    if (!el) return;

    updateLogOverflow();

    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", updateLogOverflow);
      return () => window.removeEventListener("resize", updateLogOverflow);
    }

    const observer = new ResizeObserver(updateLogOverflow);
    observer.observe(el);
    return () => observer.disconnect();
  }, [updateLogOverflow]);

  // A run is actually being STARTED (no run id yet, auto-start requested) — the
  // only case where "Starting discovery run" is the truth.
  if (!runId && autoStartRequested && hasAtLeastOneSource) {
    return (
      <PageShell title="Discovery Run" description={pageDescription}>
        <LoadingPanel
          title="Starting discovery run"
          subtitle="Preparing the run and connecting the selected sources."
        />
      </PageShell>
    );
  }

  // Loading an EXISTING run (first load of this run id). This is not "starting"
  // anything — an already-complete run was showing the start copy, which read as
  // if it were running again. Skeleton mirrors the run layout below (status bar,
  // progress panel, summary + log grid) so it fills the same space.
  if (loading) {
    return (
      <PageShell title="Discovery Run" description={pageDescription}>
        <div aria-busy="true" aria-label="Loading discovery run">
          <Skeleton className="mb-5 h-16 w-full rounded-xl" />
          <Skeleton className="mb-4 h-64 w-full rounded-xl" />
          <div className="grid grid-cols-1 items-start gap-4 lg:grid-cols-3">
            <Skeleton className="h-96 w-full rounded-xl" />
            <Skeleton className="h-96 w-full rounded-xl lg:col-span-2" />
          </div>
        </div>
      </PageShell>
    );
  }

  if (!runId) {
    return (
      <PageShell title="Discovery Run" description={pageDescription}>
        <InfoPanel
          title="No Active Run"
          message="Start a new discovery run to continue."
          actionLabel={
            hasAtLeastOneSource ? "Start New Discovery Run" : "Go to Integration Hub"
          }
          onAction={
            hasAtLeastOneSource
              ? () => void startRun(inputs)
              : () => nav("/integration-hub")
          }
        >
          {!hasAtLeastOneSource && (
            <div className="mt-3 text-center text-sm font-medium text-muted">
              {DISCOVERY_SOURCE_REQUIREMENT_MESSAGE}
            </div>
          )}
        </InfoPanel>
      </PageShell>
    );
  }

  if (error) {
    return (
      <PageShell title="Discovery Run" description={pageDescription}>
        <div className="mx-auto max-w-3xl">
          <div className="rounded-xl border border-border bg-panel p-6">
            <div className="text-lg font-semibold">Discovery run failed</div>
            <div className="mt-2 text-sm text-red-300">{error}</div>
            <button
              className="mt-4 rounded-md border border-accent/20 bg-accent/5 px-3 py-2 text-sm font-medium text-accent transition-colors hover:border-accent/45 hover:bg-accent/10 focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40"
              onClick={() => {
                if (runId) refetch();
                else if (hasAtLeastOneSource) void startRun(inputs);
              }}
            >
              Retry
            </button>
          </div>
        </div>
      </PageShell>
    );
  }

  return (
    <PageShell
      title="Discovery Run"
      description={pageDescription}
      actions={
        <>
          <button
            className="rounded-md border border-accent/20 bg-accent/5 px-3 py-2 text-sm font-medium text-accent transition-colors hover:border-accent/45 hover:bg-accent/10 focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 disabled:cursor-not-allowed disabled:opacity-50"
            onClick={() => nav(runScopedPath("/source-intelligence"))}
            disabled={!started || !isMaterialized || computing}
            title={computing ? "Waiting for compute to finish..." : undefined}
          >
            {computing ? "Computing..." : "Next: Source Intelligence"}
          </button>
        </>
      }
    >
        {run && <TemplateRunNotice run={run} computing={computing} />}

        <div className="mb-5 rounded-xl border border-border bg-panel px-4 py-3">
            <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm text-muted">
              <span>
                Run ID:{" "}
                <span className="font-semibold text-text">
                  {run?.id ?? runId ?? "-"}
                </span>
              </span>
              <span className="text-muted">-</span>
              <span>Status:</span>
              <RunStatusPill
                computing={computing}
                isComplete={isComplete}
                displayPct={displayPct}
                status={run?.status}
              />
              {computing && <ComputingPill />}
              {!computing && isMaterialized && <SourceIntelligenceReadyPill />}
            </p>
            {run?.startedAt && (
              <p className="mt-1 text-xs text-muted">
                Started: {formatRunTimestamp(run.startedAt)}
              </p>
            )}
        </div>

        {/* CS-4 T5 AC4: Discovery progress step list — shown while computing.
            Replaces the generic ComputingPill spinner as the primary progress
            indicator. Hidden once the run materialises. */}
        {(computing || currentStep != null || isMaterialized) && (
          <div className="mb-4 rounded-xl border border-border bg-panel p-4">
            <div className="mb-4 text-lg font-semibold">Discovery Progress</div>
            <DiscoveryStepList
              currentStep={currentStep}
              runComplete={isMaterialized}
              salesforceProduct={salesforceProduct}
              salesforceProducts={salesforceProducts}
              failedSteps={failedSteps}
              connectedSources={progressConnectedSources}
            />
          </div>
        )}

        <div className="grid grid-cols-1 items-start gap-4 lg:grid-cols-3">
          <div className="flex h-[460px] min-h-0 flex-col rounded-xl border border-border bg-panel p-4 lg:h-[560px]">
            <div className="shrink-0 text-lg font-semibold">Run Summary</div>
            <div className="mt-3 min-h-0 flex-1 space-y-4 overflow-auto pr-1 text-sm text-muted">
              <div className="rounded-lg border border-border bg-bg/10 p-3">
                <div className="font-semibold text-text">Connected sources</div>
                <div className="mt-1 max-h-28 overflow-auto break-words pr-1">
                  {summaryInputs.connectedSources.length
                    ? summaryInputs.connectedSources.join(" - ")
                    : "None"}
                </div>
              </div>
              <div className="rounded-lg border border-border bg-bg/10 p-3">
                <div className="font-semibold text-text">Uploaded files</div>
                <div className="mt-1 max-h-28 overflow-auto break-words pr-1">
                  {summaryInputs.uploadedFiles.length
                    ? summaryInputs.uploadedFiles.join(" - ")
                    : "None"}
                </div>
              </div>
              {/* T41-8: Sample workspace panel removed */}
            </div>
          </div>

          <div className="flex h-[460px] min-h-0 flex-col rounded-xl border border-border bg-panel p-4 lg:col-span-2 lg:h-[560px]">
            <div className="flex shrink-0 items-center justify-between">
              <div className="flex items-center gap-5">
                <div className="text-lg font-semibold">Discovery Log</div>
                {logHasOverflow && (
                  <label className="flex items-center gap-2 text-sm text-text">
                    Auto-scroll
                    <input
                      type="checkbox"
                      checked={autoScroll}
                      onChange={(e) => setAutoScroll(e.target.checked)}
                      className="accent-accent cursor-pointer"
                    />
                  </label>
                )}
              </div>
              {/* Matches the Run Health dashboard's Refresh button (icon that
                  spins, busy label, disabled + aria-busy while in flight) so the
                  same action reads the same way on both pages. The busy state is
                  the context's `refreshing` — `loading` is deliberately suppressed
                  for a run already on screen, so it cannot report this. */}
              <button
                type="button"
                data-testid="refresh-run"
                onClick={() => refresh()}
                disabled={refreshing}
                aria-busy={refreshing}
                className="inline-flex items-center gap-2 rounded-lg border border-border bg-panel px-3 py-2 text-sm font-semibold text-muted shadow-sm hover:bg-bg/40 disabled:cursor-not-allowed disabled:opacity-60"
              >
                <RefreshCw className={`h-4 w-4 ${refreshing ? "animate-spin" : ""}`} aria-hidden="true" />
                {refreshing ? "Refreshing…" : "Refresh"}
              </button>
            </div>

            <div
              ref={logScrollRef}
              className="mt-3 min-h-0 flex-1 overflow-auto rounded-lg border border-border bg-bg/10 p-3"
            >
              {events.length === 0 ? (
                <div className="text-sm text-muted">No events yet.</div>
              ) : (
                <div className="space-y-2 text-sm">
                  {events.map((e, i) => (
                    <div key={e.id ?? i} className="grid gap-x-3 gap-y-1 text-sm leading-6 sm:grid-cols-[10rem_7rem_minmax(0,1fr)]">
                      <div className="text-muted">
                        {formatRunTimestamp(e.tsLabel ?? e.ts)}
                      </div>
                      <div className="font-normal normal-case tracking-normal text-muted">
                        {e.stage ?? ""}
                      </div>
                      <div className="min-w-0 whitespace-normal break-words text-text">{formatRunMessage(e.message)}</div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>
    </PageShell>
  );
}
