import React, { useEffect, useMemo, useState, useCallback } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Zap } from "lucide-react";
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
import LoadingPanel from "../components/common/LoadingPanel";
import ErrorPanel from "../components/common/ErrorPanel";
import { RunRequiredEmptyState } from "../components/common/RunRequiredEmptyState";
import { useAnalystReviewContext } from "../context/AnalystReviewContext";
import { useConnectorContext } from "../context/ConnectorContext";
import { useRunContext } from "../context/RunContext";
import { useToast } from "../components/common/Toast";

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
  const pageDescription =
    "Prioritize, approve, and understand automation opportunities from one review workspace.";

  const salesforceConnected = connectors.some(
    (c) => c.id === "salesforce" && c.status === "connected",
  );

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
    },
    [select],
  );

  useEffect(() => {
    if (filtered.length > 0) {
      const isCurrentValid = filtered.some((o) => o.id === selectedId);
      if (!selectedId || !isCurrentValid) {
        select(filtered[0].id);
      }
    }
  }, [filtered, selectedId, select]);

  useEffect(() => {
    if (!requestedOppId) return;
    if (!opportunities.some((o) => o.id === requestedOppId)) return;
    select(requestedOppId);
  }, [requestedOppId, opportunities, select]);

  const selected = useMemo(
    () => filtered.find((o) => o.id === selectedId) || null,
    [filtered, selectedId],
  );

  const blueprintAction = selected ? (
    <div data-testid="blueprint-button-container">
      {salesforceConnected ? (
        <button
          data-testid="blueprint-button-active"
          onClick={() => {
            select(selected.id);
            nav(
              `/agentforce-blueprint?oppId=${encodeURIComponent(selected.id)}`,
            );
          }}
          className="flex w-full items-center justify-center gap-2 rounded-md bg-accent px-4 py-3 text-sm font-medium text-white transition hover:opacity-90"
        >
          <Zap size={15} />
          View Agentforce Blueprint
        </button>
      ) : (
        <button
          data-testid="blueprint-button-disabled"
          disabled
          className="flex w-full cursor-not-allowed items-center justify-center gap-2 rounded-md border border-border bg-bg/30 px-4 py-3 text-sm font-medium text-muted opacity-60"
          title="Connect Salesforce on Integration Hub to enable Agentforce Blueprint"
        >
          <Zap size={15} />
          Agentforce Blueprint (connect Salesforce)
        </button>
      )}
    </div>
  ) : null;

  if (!runId) {
    return (
      <PageShell title="Opportunity Review" description={pageDescription}>
        <RunRequiredEmptyState onStart={() => nav("/discovery-run")} />
      </PageShell>
    );
  }

  if (loading) {
    return (
      <PageShell title="Opportunity Review" description={pageDescription}>
        <LoadingPanel title="Loading Opportunity Review..." />
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

        <div className="grid grid-cols-1 gap-4 lg:h-[620px] lg:grid-cols-[minmax(0,1.12fr)_minmax(380px,0.88fr)] lg:items-stretch">
          <OpportunityMatrix
            filtered={filtered}
            selectedId={selectedId}
            onSelect={handleSelect}
          />

          <OpportunityDetail
            opp={selected}
            audit={audit}
            suppressPermissions={true}
            footer={blueprintAction}
            onNavigate={() => {
              if (selected) {
                select(selected.id);
                nav("/executive-report");
              }
            }}
          />
        </div>

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
