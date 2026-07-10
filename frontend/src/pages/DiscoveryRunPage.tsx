import React, { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { CheckCircle2, Circle, Info, Loader2, XCircle } from "lucide-react";
import { useLocation, useNavigate } from "react-router-dom";
import { InfoPanel } from "../components/common/InfoPanel";
import LoadingPanel from "../components/common/LoadingPanel";
import PageShell from "../components/common/PageShell";
import { useDiscoveryRunContext } from "../context/DiscoveryRunContext";
import { useConnectorContext } from "../context/ConnectorContext";
import { useSourceIntakeContext } from "../context/SourceIntakeContext";
import { useRunContext } from "../context/RunContext";
import { useAuthOptional } from "../context/AuthContext";
import { isViewerRole } from "../utils/roles";
import {
  DISCOVERY_SOURCE_REQUIREMENT_MESSAGE,
  isDiscoveryReadyConnector,
} from "../utils/sourceReadiness";
import { apiGet, apiGetRunScoped } from "../lib/apiClient";

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
    id: "slack",
    label: "Slack",
    subLabel: "Ingesting channel activity, escalation, and cross-reference signals",
  },
  {
    id: "sf_ncino",
    label: "nCino Lending",
    subLabel: "Ingesting nCino loan origination signals",
    infoTooltip: SALESFORCE_DUAL_EXTRACTION_TOOLTIP,
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
const SOURCE_STEP_IDS = new Set(["sf_crm", "sn", "jira", "sf_ncino", "slack"]);

// The pack-specific second Salesforce pass (labelled by the selected pack —
// Service Cloud / nCino / …). It is a source step, but it is rendered LAST among
// the source stages — after every connected source — so the progress list reads
// "all connected sources → the selected pack".
const PACK_STEP_IDS = new Set(["sf_ncino"]);

const STEP_SOURCE_TOKENS: Record<string, string[]> = {
  sf_crm: ["salesforce"],
  sf_ncino: ["salesforce"],
  sn: ["servicenow"],
  jira: ["jira"],
  slack: ["slack"],
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
// DiscoveryStepList — renders all steps with completed / active / pending state
// ---------------------------------------------------------------------------
export function DiscoveryStepList({
  currentStep,
  runComplete = false,
  salesforceProduct,
  failedSteps = [],
  connectedSources,
}: {
  currentStep: string | null;
  // True only once the discovery run has truly finished (100%). The backend can
  // emit the "complete" step while the run is still computing post-processing,
  // so the terminal step's green tick is gated on this flag, not on currentStep.
  runComplete?: boolean;
  // Declared Salesforce product id (e.g. "salesforce_sc"). Drives the label of
  // the second Salesforce pass so the run reflects the workspace's declaration.
  salesforceProduct?: string;
  // Step ids whose ingest failed (from the run status' failed_steps). A failed
  // step is rendered with an error icon, never as a completed green check — so
  // a failed stage is not misrepresented as successful (CS-4 / AT-313).
  failedSteps?: string[];
  // The sources actually connected for this run (connector ids/names — the same
  // set shown in the Discovery Log / Run Summary). When provided, the progress
  // list shows a stage for each connected source (plus the processing stages)
  // and omits unconnected sources. When omitted (undefined), every known source
  // stage is shown — the legacy fixed-pipeline behaviour.
  connectedSources?: string[];
}) {
  const activeIdx =
    currentStep != null ? (STEP_INDEX[currentStep] ?? -1) : -1;
  // Processing stages always finish before/at this boundary; a generic source
  // (one with no dedicated backend step) is considered ingested once the run has
  // reached detection.
  const detectIdx = STEP_INDEX["detect"];

  const canonical = resolveDiscoverySteps(salesforceProduct);
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

  // 2. Pack-specific second Salesforce pass — appended AFTER every connected
  //    source ("all connected sources → selected pack"). Shown only when
  //    Salesforce is connected (or in the legacy all-stages mode).
  const packSteps = canonical.filter(
    (s) => PACK_STEP_IDS.has(s.id) && salesforceConnected
  );

  // 3. Processing stages — always shown, in canonical order.
  const processingSteps = canonical.filter((s) => !SOURCE_STEP_IDS.has(s.id));

  const steps = [...sourceSteps, ...packSteps, ...processingSteps];

  return (
    <ol className="space-y-3">
      {steps.map((step) => {
        const isGeneric = step.id.startsWith("src:");
        // Known/processing stages map to a canonical backend index; generic
        // source stages do not (no backend step), so their state comes from the
        // detection boundary instead.
        const canonicalIdx = isGeneric ? -1 : (STEP_INDEX[step.id] ?? -1);

        // A failed ingest takes precedence over every other state: it must never
        // show the completed green check, even after the run advances past it.
        const isFailed = failedSteps.includes(step.id);
        // "complete" is the terminal step. It earns the green check only when the
        // run has actually finished (runComplete). While the run is still running
        // — even if the backend already emitted "complete" — it shows the spinner.
        const isTerminal = step.id === "complete";

        let isCompleted: boolean;
        let isActive: boolean;
        if (isGeneric) {
          // Ingested once the pipeline reaches detection (all sources read);
          // shows the spinner while source ingestion is still in progress.
          isCompleted = !isFailed && activeIdx >= detectIdx;
          isActive =
            !isFailed && !isCompleted && activeIdx >= 0 && activeIdx < detectIdx;
        } else {
          isCompleted =
            !isFailed &&
            (isTerminal
              ? runComplete && activeIdx >= canonicalIdx
              : activeIdx > canonicalIdx);
          isActive = !isFailed && !isCompleted && activeIdx === canonicalIdx;
        }

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
  // Replay re-triggers compute (POST /api/runs/{run_id}/replay is analyst+), so
  // viewers get a disabled Replay button — read-only access.
  const auth = useAuthOptional();
  const isViewer = isViewerRole(auth?.user?.role);

  const {
    run,
    events,
    loading,
    error,
    started,
    computing,
    startRun,
    restartRun,
    refetch,
  } = useDiscoveryRunContext();

  // ---------------------------------------------------------------------------
  // CS-4 T5: Poll /api/runs/{runId}/status every 2 s while the run is active.
  // Reads current_step from the response and stops once complete or errored.
  // ---------------------------------------------------------------------------
  const [currentStep, setCurrentStep] = useState<string | null>(null);
  const [failedSteps, setFailedSteps] = useState<string[]>([]);

  // Reset the step indicator whenever the active run changes. Without this, a
  // newly started run inherits the previous run's last step (e.g. "complete")
  // — the /status poll only overwrites currentStep once the backend has written
  // a non-null current_step, so until the first step lands the progress list
  // would show every step ticked while the backend is still ingesting. The new
  // run's real step is then re-applied from its /status response.
  useEffect(() => {
    setCurrentStep(null);
    setFailedSteps([]);
  }, [runId]);

  // CS-4: the declared Salesforce product (from Integration Hub) decides what
  // the second Salesforce discovery pass is labelled as. Single declaration
  // (radio), so the first declared id wins. Failure → undefined → default copy.
  const [salesforceProduct, setSalesforceProduct] = useState<string | undefined>(
    undefined
  );

  useEffect(() => {
    let cancelled = false;
    apiGet<{ ok: boolean; products: string[]; labels: string[] }>(
      "/api/connectors/salesforce/products"
    )
      .then((data) => {
        if (!cancelled) setSalesforceProduct(data?.products?.[0]);
      })
      .catch(() => {
        // Non-blocking: with no declaration the default nCino copy is used.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // CS-4 T5 + AT-313: poll the run status while the run is active. The base
  // cadence is 2 s (AC4), and it stays at 2 s while the step is actively
  // advancing. When the step is unchanged between polls (a long-running or
  // stalled stage) the interval backs off geometrically up to a cap, so a slow
  // run is not hammered with a fixed 2 s poll for minutes on end. The cadence
  // resets to the 2 s base as soon as the step advances again. Self-scheduling
  // setTimeout (not setInterval) so each delay can differ.
  useEffect(() => {
    if (!runId || !computing) return;

    const BASE_DELAY_MS = 2000;
    const MAX_DELAY_MS = 15000;
    const BACKOFF_FACTOR = 1.5;

    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let delay = BASE_DELAY_MS;
    // `undefined` = no poll yet; the first observation always counts as a change
    // so the cadence starts at the base even if current_step is still null.
    let lastStep: string | null | undefined = undefined;

    const schedule = () => {
      if (cancelled) return;
      timer = setTimeout(() => void tick(), delay);
    };

    const tick = async () => {
      try {
        const st = await apiGetRunScoped<{
          current_step?: string | null;
          status?: string;
          failed_steps?: string[];
        }>(runId, "/status");

        if (cancelled) return;

        if (st.current_step != null) setCurrentStep(st.current_step);
        setFailedSteps(Array.isArray(st.failed_steps) ? st.failed_steps : []);

        const step = st.current_step ?? null;
        if (step !== lastStep) {
          delay = BASE_DELAY_MS; // progress moved — stay responsive
        } else {
          delay = Math.min(delay * BACKOFF_FACTOR, MAX_DELAY_MS);
        }
        lastStep = step;

        // Stop polling once the step reaches complete or the run errors out.
        const done =
          step === "complete" ||
          (st.status != null &&
            !["running", "queued"].includes(st.status.toLowerCase()));
        if (done) return; // do not reschedule
      } catch {
        // Non-blocking: polling failures do not surface errors to the UI, but
        // do back off so a persistently failing endpoint is not hammered.
        if (cancelled) return;
        delay = Math.min(delay * BACKOFF_FACTOR, MAX_DELAY_MS);
      }
      schedule();
    };

    void tick(); // immediate first poll, then self-scheduled with backoff

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [runId, computing]);

  const status = run?.status?.toLowerCase();
  const isMaterialized =
    status === "complete" || status === "completed" || status === "partial";
  const isComplete = status === "complete" || status === "completed";
  const runScopedPath = (path: string) =>
    runId ? `${path}?runId=${runId}` : path;

  const [displayPct, setDisplayPct] = useState(0);
  const targetPct = useMemo(() => {
    if (isComplete) return 100;
    if (!computing) return 0;
    // Tie the percentage to the SAME current_step signal that drives the
    // Discovery Progress checklist, so the number and the green-checked steps
    // always agree — both reflect the backend's update_run_step() timing.
    // Each working step is one slice of the pipeline; "complete" maps to 100%.
    const idx = currentStep != null ? (STEP_INDEX[currentStep] ?? -1) : -1;
    if (idx < 0) return 0;
    const lastIdx = DISCOVERY_STEPS.length - 1; // index of the terminal "complete" step
    return Math.min(Math.round((idx / lastIdx) * 100), 99);
  }, [isComplete, computing, currentStep]);

  // FIX: Safe requestAnimationFrame implementation
  useEffect(() => {
    if (displayPct === targetPct) return;

    const id = requestAnimationFrame(() => {
      setDisplayPct((prev) => {
        if (prev < targetPct) return prev + 1;
        if (prev > targetPct) return prev - 1;
        return prev;
      });
    });

    return () => cancelAnimationFrame(id);
  }, [displayPct, targetPct]);

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
  const runSelectedSystems: string[] = (run as any)?.selectedSystemIds ?? [];
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

  if (loading || (!runId && autoStartRequested && hasAtLeastOneSource)) {
    return (
      <PageShell title="Discovery Run" description={pageDescription}>
        <LoadingPanel
          title="Starting discovery run"
          subtitle="Preparing the run and connecting the selected sources."
        />
      </PageShell>
    );
  }

  if (!runId) {
    return (
      <PageShell title="Discovery Run" description={pageDescription}>
        <InfoPanel
          title="No Active Run"
          message="Start a new discovery run to continue."
          actionLabel="Start New Discovery Run"
          actionDisabled={!hasAtLeastOneSource}
          onAction={() => void startRun(inputs)}
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
            onClick={() => void restartRun()}
            disabled={!started || !isMaterialized || computing || loading || isViewer}
            title={
              isViewer
                ? "Replay requires an analyst or owner role."
                : !isMaterialized
                  ? "Replay is available after this run finishes."
                  : undefined
            }
          >
            Replay Run
          </button>

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
        {(computing || currentStep != null) && (
          <div className="mb-4 rounded-xl border border-border bg-panel p-4">
            <div className="mb-4 text-lg font-semibold">Discovery Progress</div>
            <DiscoveryStepList
              currentStep={currentStep}
              runComplete={!computing}
              salesforceProduct={salesforceProduct}
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
              <button
                className="rounded-md border border-accent/20 bg-accent/5 px-3 py-2 text-sm font-semibold text-accent transition-colors hover:border-accent/45 hover:bg-accent/10 focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40"
                onClick={() => refetch()}
              >
                Refresh
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
