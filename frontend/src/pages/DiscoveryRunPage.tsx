import React, { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { CheckCircle2, Circle, Info, Loader2 } from "lucide-react";
import { useLocation, useNavigate } from "react-router-dom";
import { InfoPanel } from "../components/common/InfoPanel";
import LoadingPanel from "../components/common/LoadingPanel";
import PageShell from "../components/common/PageShell";
import { useDiscoveryRunContext } from "../context/DiscoveryRunContext";
import { useConnectorContext } from "../context/ConnectorContext";
import { useSourceIntakeContext } from "../context/SourceIntakeContext";
import { useRunContext } from "../context/RunContext";
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
// (backend/discovery/runner.py): Salesforce CRM → ServiceNow → Jira → the
// second Salesforce pass (sf_ncino) → detect → enrich → complete. The second
// Salesforce pass is emitted AFTER Jira ingestion, so it is listed here after
// the Jira step — keeping the progress indicator consistent with the run log.
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
}: {
  currentStep: string | null;
  // True only once the discovery run has truly finished (100%). The backend can
  // emit the "complete" step while the run is still computing post-processing,
  // so the terminal step's green tick is gated on this flag, not on currentStep.
  runComplete?: boolean;
  // Declared Salesforce product id (e.g. "salesforce_sc"). Drives the label of
  // the second Salesforce pass so the run reflects the workspace's declaration.
  salesforceProduct?: string;
}) {
  const activeIdx =
    currentStep != null ? (STEP_INDEX[currentStep] ?? -1) : -1;
  const steps = resolveDiscoverySteps(salesforceProduct);

  return (
    <ol className="space-y-3">
      {steps.map((step, idx) => {
        // "complete" is the terminal step. It earns the green check only when the
        // run has actually finished (runComplete). While the run is still running
        // — even if the backend already emitted "complete" — it shows the spinner.
        const isTerminal = step.id === "complete";
        const isCompleted = isTerminal
          ? runComplete && activeIdx >= idx
          : activeIdx > idx;
        const isActive = !isCompleted && activeIdx === idx;

        return (
          <li key={step.id} className="flex items-start gap-3">
            {/* Icon */}
            <div className="mt-0.5 shrink-0">
              {isCompleted ? (
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
                  isCompleted
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
                  isCompleted || isActive ? "text-muted" : "text-muted/40"
                }`}
              >
                {step.subLabel}
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
    startRun,
    restartRun,
    refetch,
  } = useDiscoveryRunContext();

  // ---------------------------------------------------------------------------
  // CS-4 T5: Poll /api/runs/{runId}/status every 2 s while the run is active.
  // Reads current_step from the response and stops once complete or errored.
  // ---------------------------------------------------------------------------
  const [currentStep, setCurrentStep] = useState<string | null>(null);

  // Reset the step indicator whenever the active run changes. Without this, a
  // newly started run inherits the previous run's last step (e.g. "complete")
  // — the /status poll only overwrites currentStep once the backend has written
  // a non-null current_step, so until the first step lands the progress list
  // would show every step ticked while the backend is still ingesting. The new
  // run's real step is then re-applied from its /status response.
  useEffect(() => {
    setCurrentStep(null);
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

  useEffect(() => {
    if (!runId || !computing) return;

    let cancelled = false;

    const poll = async () => {
      try {
        const st = await apiGetRunScoped<{
          current_step?: string | null;
          status?: string;
        }>(runId, "/status");

        if (cancelled) return;

        if (st.current_step != null) {
          setCurrentStep(st.current_step);
        }

        // Stop polling once the step reaches complete or the run errors out.
        const done =
          st.current_step === "complete" ||
          (st.status != null &&
            !["running", "queued"].includes(st.status.toLowerCase()));
        if (done) {
          clearInterval(intervalId);
        }
      } catch {
        // Non-blocking: polling failures do not surface errors to the UI.
      }
    };

    void poll();
    const intervalId = setInterval(() => void poll(), 2000);

    return () => {
      cancelled = true;
      clearInterval(intervalId);
    };
  }, [runId, computing]);

  const TOTAL_STAGES = 10;

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
    const seen = new Set(events.map((e: any) => e.stage).filter(Boolean));
    return Math.min(Math.round((seen.size / TOTAL_STAGES) * 100), 99);
  }, [isComplete, computing, events]);

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
            disabled={!started || !isMaterialized || computing || loading}
            title={
              !isMaterialized
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
