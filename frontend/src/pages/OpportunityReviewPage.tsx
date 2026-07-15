import React, { useEffect, useMemo, useState, useCallback } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { ArrowRight, ChevronDown, Zap } from "lucide-react";
import PageShell from "../components/common/PageShell";
import OpportunityToolbar, {
  ConfidenceFilter,
  DecisionFilter,
  TierFilter,
} from "../components/opportunity_map/OpportunityToolbar";
import OpportunityMatrix from "../components/opportunity_map/OpportunityMatrix";
import TopQuickWins from "../components/opportunity_map/TopQuickWins";
import OpportunityRankedList from "../components/opportunity_map/OpportunityRankedList";
import OpportunityDetail from "../components/analyst_review/OpportunityDetail";
import ReasoningOverride from "../components/analyst_review/ReasoningOverride";
import { Skeleton } from "../components/common/Skeleton";
import ErrorPanel from "../components/common/ErrorPanel";
import { RunRequiredEmptyState } from "../components/common/RunRequiredEmptyState";
import { useAnalystReviewContext } from "../context/AnalystReviewContext";
import { useConnectorContext } from "../context/ConnectorContext";
import { useRunContext } from "../context/RunContext";
import { useToast } from "../components/common/Toast";
import {
  getBlueprintLabel,
  isSalesforceConnected,
} from "../utils/blueprintNaming";

export default function OpportunityReviewPage() {
  const {
    opportunities,
    selectedId,
    select,
    audit,
    setDecision,
    saveOverride,
    loading,
    error,
    refetch,
  } = useAnalystReviewContext();

  const { all: connectors } = useConnectorContext();
  const { runId } = useRunContext();
  const { push } = useToast();
  const nav = useNavigate();
  const [searchParams] = useSearchParams();
  const requestedOppId = searchParams.get("oppId");

  const [q, setQ] = useState("");
  const [tier, setTier] = useState<TierFilter>("All");
  const [conf, setConf] = useState<ConfidenceFilter>("All");
  const [decisionF, setDecisionF] = useState<DecisionFilter>("All");
  const [detailPanelOpen, setDetailPanelOpen] = useState(true);
  const pageDescription =
    "Prioritize, approve, and understand automation opportunities from one review workspace.";

  const salesforceConnected = isSalesforceConnected(connectors);
  const blueprintLabel = getBlueprintLabel(salesforceConnected);

  const filtered = useMemo(() => {
    const query = q.trim().toLowerCase();
    return opportunities
      .filter((o) => tier === "All" || o.tier === tier)
      .filter((o) => conf === "All" || o.confidence === conf)
      .filter((o) => decisionF === "All" || o.decision === decisionF)
      .filter(
        (o) =>
          !query ||
          o.title.toLowerCase().includes(query) ||
          o.category.toLowerCase().includes(query),
      );
  }, [opportunities, q, tier, conf, decisionF]);

  const ranked = useMemo(
    () =>
      filtered
        .slice()
        .sort(
          (a, b) =>
            b.impact - b.effort - (a.impact - a.effort) || b.impact - a.impact,
        ),
    [filtered],
  );

  const quickWins = useMemo(
    () => ranked.filter((o) => o.tier === "Quick Win"),
    [ranked],
  );

  const handleSelect = useCallback(
    (id: string) => {
      select(id);
      setDetailPanelOpen(true);
    },
    [select],
  );

  useEffect(() => {
    if (selectedId && !filtered.some((o) => o.id === selectedId)) {
      setDetailPanelOpen(false);
    }
  }, [filtered, selectedId]);

  useEffect(() => {
    if (!requestedOppId) return;
    if (!opportunities.some((o) => o.id === requestedOppId)) return;
    select(requestedOppId);
    setDetailPanelOpen(true);
  }, [requestedOppId, opportunities, select]);

  const selected = useMemo(
    () => filtered.find((o) => o.id === selectedId) || null,
    [filtered, selectedId],
  );

  const blueprintAction = selected ? (
    <div
      data-testid="blueprint-button-container"
      className="flex justify-end"
    >
      {salesforceConnected ? (
        <button
          data-testid="blueprint-button-active"
          onClick={() => {
            select(selected.id);
            nav(
              `/agentforce-blueprint?oppId=${encodeURIComponent(selected.id)}`,
            );
          }}
          className="inline-flex items-center justify-center gap-2 rounded-lg border border-accent/30 bg-accent/10 px-4 py-2.5 text-sm font-semibold text-accent transition-colors hover:border-accent/50 hover:bg-accent/15 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
        >
          <Zap size={15} />
          View {blueprintLabel}
        </button>
      ) : (
        <button
          data-testid="blueprint-button-disabled"
          disabled
          className="inline-flex cursor-not-allowed items-center justify-center gap-2 rounded-lg border border-border bg-bg/30 px-4 py-2.5 text-sm font-medium text-muted opacity-60"
          title="Connect Salesforce on Integration Hub to enable the Agent Blueprint"
        >
          <Zap size={15} />
          {blueprintLabel} (connect Salesforce)
        </button>
      )}
    </div>
  ) : null;

  if (!runId) {
    return (
      <PageShell title="Opportunity Review" description={pageDescription}>
        <RunRequiredEmptyState onStart={() => nav("/integration-hub")} />
      </PageShell>
    );
  }

  if (loading) {
    return (
      <PageShell title="Opportunity Review" description={pageDescription}>
        {/* Skeleton mirrors the toolbar + matrix box (same height) so the real
            content fills the same space with no layout shift. */}
        <div aria-busy="true" aria-label="Loading Opportunity Review">
          <div className="flex flex-wrap items-center gap-3">
            <Skeleton className="h-9 w-64" />
            <Skeleton className="h-9 w-28" />
            <Skeleton className="h-9 w-28" />
            <Skeleton className="h-9 w-28" />
          </div>
          <Skeleton className="mt-4 h-[560px] w-full lg:h-[720px]" />
        </div>
      </PageShell>
    );
  }

  if (error) {
    return (
      <PageShell title="Opportunity Review" description={pageDescription}>
        <ErrorPanel message={error} onRetry={refetch} />
      </PageShell>
    );
  }

  return (
    <PageShell title="Opportunity Review" description={pageDescription}>
        <OpportunityToolbar
          q={q}
          onQ={setQ}
          tier={tier}
          onTier={setTier}
          conf={conf}
          onConf={setConf}
          decision={decisionF}
          onDecision={setDecisionF}
          totalShown={filtered.length}
        />

        <div className="mt-4 h-[560px] lg:h-[720px]">
          <OpportunityMatrix
            filtered={filtered}
            selectedId={selectedId}
            onSelect={handleSelect}
          />
        </div>

        {selected && (
          <section className="mt-4 overflow-hidden rounded-xl border border-border bg-panel">
            <div className="flex items-center justify-between gap-3 px-5 py-4">
              <div className="min-w-0">
                <div className="text-xs font-semibold uppercase tracking-wide text-muted">
                  Selected Opportunity Details
                </div>
                <div className="mt-1 truncate text-lg font-semibold leading-tight text-text">
                  {selected.title}
                </div>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <button
                  type="button"
                  onClick={() => {
                    select(selected.id);
                    nav("/executive-report");
                  }}
                  className="group relative flex h-10 w-10 items-center justify-center rounded-lg border border-accent/25 bg-accent/10 text-accent transition hover:-translate-y-0.5 hover:border-accent/45 hover:bg-accent/15 hover:text-accent focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
                  aria-label="Open opportunity report"
                >
                  <ArrowRight size={17} strokeWidth={2.4} />
                  <span className="pointer-events-none absolute right-0 top-full z-20 mt-2 hidden whitespace-nowrap rounded-md border border-border bg-panel px-2 py-1 text-[11px] font-medium text-text group-hover:block">
                    Open report
                  </span>
                </button>
                <button
                  type="button"
                  onClick={() => setDetailPanelOpen((open) => !open)}
                  aria-expanded={detailPanelOpen}
                  className="group relative flex h-10 w-10 items-center justify-center rounded-lg border border-accent/25 bg-accent/10 text-accent transition hover:-translate-y-0.5 hover:border-accent/45 hover:bg-accent/15 hover:text-accent focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
                  aria-label={detailPanelOpen ? "Collapse selected opportunity details" : "Expand selected opportunity details"}
                >
                  <ChevronDown
                    size={18}
                    strokeWidth={2.4}
                    className={`transition-transform ${detailPanelOpen ? "rotate-180" : ""}`}
                  />
                  <span className="pointer-events-none absolute right-0 top-full z-20 mt-2 hidden whitespace-nowrap rounded-md border border-border bg-panel px-2 py-1 text-[11px] font-medium text-text group-hover:block">
                    {detailPanelOpen ? "Click to collapse" : "Click to expand"}
                  </span>
                </button>
              </div>
            </div>

            {detailPanelOpen && (
              <div className="h-[560px] border-t border-border">
                <OpportunityDetail
                  opp={selected}
                  audit={audit}
                  hideTitleBar={true}
                  suppressPermissions={true}
                  footer={blueprintAction}
                  onNavigate={() => {
                    select(selected.id);
                    nav("/executive-report");
                  }}
                />
              </div>
            )}
          </section>
        )}

        <div className="mt-4 grid grid-cols-1 gap-4 lg:h-[460px] lg:grid-cols-3 lg:items-stretch">
          <TopQuickWins
            quickWins={quickWins}
            selectedId={selectedId}
            onSelect={handleSelect}
          />

          <OpportunityRankedList
            ranked={ranked}
            selectedId={selectedId}
            onSelect={handleSelect}
          />

          <ReasoningOverride
            opp={selected}
            audit={audit}
            onSave={async (rationaleOverride, overrideReason, isLocked) => {
              if (!selectedId) return;
              const r = await saveOverride(
                selectedId,
                rationaleOverride,
                overrideReason,
                isLocked,
              );
              if (!r.ok) push(r.error || "Unable to save override.");
              else push("Override saved.");
            }}
            onViewEvidence={() => {
              if (selected) {
                select(selected.id);
                nav("/partial-results");
              }
            }}
            onDecision={async (d) => {
              if (!selectedId) return;
              const result = await setDecision(selectedId, d);
              if (!result.ok)
                push(result.error || "Unable to update decision.");
              else push(`Decision set to ${d}.`);
            }}
          />
        </div>

    </PageShell>
  );
}
