import React from 'react';
import { useEffect, useState } from 'react';
import PackCertificationBadge from '../components/common/PackCertificationBadge';
import {
  PackDeprecationBadge,
  PackDeprecationDetail,
} from '../components/common/PackDeprecationNotice';
import PackMigrationAssist from '../components/common/PackMigrationAssist';
import {
  certificationsByPackId,
  deprecationsByPackId,
  isCertificationPolicyIndeterminate,
  fetchPackStates,
} from '../api/packStateApi';
import type { PackCertification } from '../types/packCertification';
import type { PackDeprecationNotice } from '../types/packDeprecation';
import type { PackCertificationPolicy, PackStateItem } from '../api/packStateApi';
import {
  ArrowLeft,
  Clock3,
  FileText,
  Grid2X2,
  LineChart,
  MessageSquare,
  Play,
} from 'lucide-react';
import Button from '../components/common/Button';
import {
  QualityRow,
  QualityLevel,
  RecommendedAddition,
  SystemRole,
  FocusId,
  IndustryId,
  IndustryListItem,
  TemplateListItem,
} from '../types/stack_builder';
import { useSetupState } from '../components/stack_builder';
import type { LendingGuideLaunchState } from '../components/stack_builder';
import {
  ANALYSIS_PACKS,
  analysisPackLabelFor,
  resolveAnalysisPackId,
} from '../data/analysisPacks';

const SYSTEM_META: Record<string, { name: string }> = {
  sap: { name: 'SAP' },
  oracle_ebs: { name: 'Oracle EBS' },
  workday: { name: 'Workday' },
  dynamics365: { name: 'Dynamics 365' },
  salesforce: { name: 'Salesforce' },
  neospin: { name: 'Neospin' },
  vitech: { name: 'Vitech' },
  salesforce_pss: { name: 'Salesforce PSS' },
  salesforce_sc: { name: 'Salesforce SC' },
  salesforce_ncino: { name: 'nCino' },
  salesforce_fsc: { name: 'Salesforce FSC' },
  salesforce_rc: { name: 'Salesforce RC' },
  salesforce_hc: { name: 'Salesforce HC' },
  jira: { name: 'Jira' },
  servicenow: { name: 'ServiceNow' },
  azure_devops: { name: 'Azure DevOps' },
  linear: { name: 'Linear' },
  zendesk: { name: 'Zendesk' },
  slack: { name: 'Slack' },
  teams: { name: 'Microsoft Teams' },
  confluence: { name: 'Confluence' },
  sharepoint: { name: 'SharePoint' },
  notion: { name: 'Notion' },
  github: { name: 'GitHub' },
  gitlab: { name: 'GitLab' },
  bitbucket: { name: 'Bitbucket' },
  azure_repos: { name: 'Azure Repos' },
  postgresql: { name: 'PostgreSQL' },
  sql_server: { name: 'SQL Server' },
  oracle_db: { name: 'Oracle DB' },
  databricks: { name: 'Databricks' },
  snowflake: { name: 'Snowflake' },
  dbt: { name: 'dbt' },
};

const FOCUS_LABELS: Record<string, string> = {
  member_customer_service: 'Member / customer service',
  core_operations: 'Core operations',
  approvals_compliance: 'Approvals / compliance',
  cross_system_handoffs: 'Cross-system handoffs',
  back_office_productivity: 'Back-office productivity',
  engineering_change: 'Engineering / change',
  enterprise_wide: 'Enterprise-wide discovery',
};

function getSystemName(id: string) {
  return SYSTEM_META[id]?.name ?? id;
}


function calcQualityRows(
  selectedIds: string[],
  weightings: Record<string, { role: SystemRole; priority?: string; workflowFocus: string[] }>,
): QualityRow[] {
  const hasPrimary = selectedIds.some(id =>
    weightings[id]?.priority === 'primary',
  );

  const signalSources = selectedIds.filter(id =>
    weightings[id]?.role === 'operational_signal_source',
  );

  const hasDoc = selectedIds.some(id =>
    weightings[id]?.role === 'documentation_system',
  );

  const hasCompliance = selectedIds.some(id =>
    weightings[id]?.workflowFocus?.includes('compliance_risk') ||
    weightings[id]?.workflowFocus?.includes('approvals'),
  );

  const hasEngineering = selectedIds.some(id =>
    weightings[id]?.role === 'engineering_change_system',
  );

  const signalLevel = (n: number): QualityLevel =>
    n >= 2 ? 'strong' : n === 1 ? 'moderate' : 'limited';

  return [
    {
      label: 'Workflow bottleneck detection',
      level: hasPrimary ? 'strong' : 'limited',
      descriptor: hasPrimary
        ? 'Strong - primary source priority confirmed'
        : 'Limited - no primary source selected',
    },
    {
      label: 'Cross-system corroboration',
      level: signalLevel(signalSources.length),
      descriptor: signalSources.length >= 2
        ? `Strong - ${signalSources.length} operational signal sources`
        : signalSources.length === 1
          ? 'Moderate - 1 operational signal source'
          : 'Limited - no operational signal sources selected',
    },
    {
      label: 'Compliance-sensitive workflows',
      level: hasCompliance ? 'strong' : 'limited',
      descriptor: hasCompliance
        ? 'Strong - compliance / risk focus confirmed'
        : 'Limited - no compliance focus configured',
    },
    {
      label: 'Documentation-driven interpretation',
      level: hasDoc ? 'moderate' : 'limited',
      descriptor: hasDoc
        ? 'Moderate - one documentation source'
        : 'Limited - no documentation system selected',
    },
    {
      label: 'Engineering / change signals',
      level: hasEngineering ? 'moderate' : 'limited',
      descriptor: hasEngineering
        ? 'Moderate - engineering system configured'
        : 'Limited - no code or engineering system selected',
    },
  ];
}

function calcRecommendedAdditions(
  selectedIds: string[],
  focusId: FocusId | null,
  industryId: IndustryId | null,
): RecommendedAddition[] {
  const additions: RecommendedAddition[] = [];

  const hasSlack = selectedIds.includes('slack') || selectedIds.includes('teams');
  const hasDoc = selectedIds.some(id => ['confluence', 'sharepoint', 'notion'].includes(id));
  const hasSignal = selectedIds.some(id => ['jira', 'servicenow', 'zendesk', 'linear', 'azure_devops'].includes(id));

  if (!hasSlack) {
    const reason = focusId === 'member_customer_service'
      ? 'Add communication signals for service escalation threads, team coordination delays, and cross-team handoff friction.'
      : focusId === 'cross_system_handoffs'
        ? 'Add communication signals for escalation threads and coordination delays that indicate handoff friction.'
        : 'Add communication signals from escalation threads, team coordination delays, and operational discussions.';
    additions.push({ systemId: 'slack', systemName: 'Slack', reason });
  }

  if (!hasDoc) {
    const reason = industryId === 'public_sector'
      ? 'Add policy documents, approval workflows, and regulatory reference materials common in public sector operating environments.'
      : focusId === 'approvals_compliance'
        ? 'Add policy documents and approval workflow documentation to strengthen compliance evidence.'
        : 'Add knowledge base articles, SOPs, and workflow documentation to strengthen process interpretation.';
    additions.push({ systemId: 'sharepoint', systemName: 'SharePoint', reason });
  }

  if (!hasSignal) {
    additions.push({
      systemId: 'jira',
      systemName: 'Jira',
      reason: 'Add ticket backlogs, work queue patterns, and process friction visible in issue tracking data.',
    });
  }

  return additions.slice(0, 3);
}

function QualityDot({ level }: { level: QualityLevel }) {
  const classes: Record<QualityLevel, string> = {
    strong: 'bg-emerald-500',
    moderate: 'bg-amber-400',
    limited: 'bg-slate-300',
  };

  return (
    <div
      className={`mt-1 h-2 w-2 flex-shrink-0 rounded-full ${classes[level]}`}
      aria-label={level}
      role="img"
    />
  );
}

type ChipVariant = 'primary' | 'signal';

function SystemChip({ name, variant }: { name: string; variant: ChipVariant }) {
  const classes: Record<ChipVariant, string> = {
    primary: 'border-accent bg-accent/15 text-blue-100',
    signal: 'border-border bg-bg/20 text-muted',
  };

  return (
    <span
      role="listitem"
      className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium ${classes[variant]}`}
    >
      <span
        className={`h-1.5 w-1.5 flex-shrink-0 rounded-full ${
          variant === 'primary' ? 'bg-accent' : 'bg-muted'
        }`}
        aria-hidden="true"
      />
      {name}
    </span>
  );
}

function AdditionIcon({ systemId }: { systemId: string }) {
  const Icon = ['slack', 'teams'].includes(systemId) ? MessageSquare : FileText;
  return <Icon size={16} className="mt-0.5 flex-shrink-0 text-accent" aria-hidden="true" />;
}

function SummaryRow({
  label,
  value,
  valueClass = 'text-text',
}: {
  label: string;
  value: string;
  valueClass?: string;
}) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-b border-border/70 py-2 last:border-0">
      <span className="text-xs text-muted">{label}</span>
      <span className={`text-right text-xs font-medium ${valueClass}`}>{value}</span>
    </div>
  );
}

interface Props {
  setupState: ReturnType<typeof useSetupState>;
  industries: IndustryListItem[];
  templates: TemplateListItem[];
  activePackId: string;
  // R191-P1: the FULL set of packs this run will activate (order-preserving) —
  // the union of the fixed Salesforce packs and the chosen analysis packs.
  activePackIds?: string[];
  // R191-P1: the SALESFORCE packs, fixed by the Integration Hub product
  // declaration. Shown read-only here — they are NOT chosen in the Discovery Plan.
  salesforcePacks?: string[];
  onLaunch: () => void;
  launchState?: LendingGuideLaunchState;
}

export default function DiscoveryPlanPage({
  setupState,
  industries,
  templates,
  activePackId,
  activePackIds = [activePackId],
  salesforcePacks = [],
  onLaunch,
  launchState = 'ready',
}: Props) {
  const { state, confidence } = setupState;

  // 2.0-C2 T3 (AT-833 / AC2): certification levels for the packs this run may
  // activate, so the level is visible AT SELECTION rather than only after a run.
  // Fail-soft: a failed read leaves the badges absent — a pack picker must still
  // work, and an unresolved badge is never rendered as a level.
  const [packCertifications, setPackCertifications] = useState<
    Record<string, PackCertification>
  >({});
  // 2.0-C2 T4 (AT-834): the org's activation floor, and which packs it blocks.
  // Shown BEFORE launch so a restricted org sees the rule at the moment it picks a
  // pack, rather than a 409 after the whole run is configured.
  const [certificationPolicy, setCertificationPolicy] =
    useState<PackCertificationPolicy | null>(null);
  const [blockedPacks, setBlockedPacks] = useState<Record<string, string>>({});
  // The policy read can FAIL, and that is not the same as "nothing is blocked".
  // The annotation is fail-soft while the activation gate fails closed, so without
  // this every pack rendered activatable and the launch then returned 503 with
  // nothing on screen explaining why. Indeterminate is shown as indeterminate.
  const [policyUnavailable, setPolicyUnavailable] = useState(false);
  // 2.0-C4 T2 (AT-843 / AC1): deprecation notices for the packs this run may
  // activate. Run configuration is the surface that matters most for a deprecation
  // — it is the moment someone is about to build a run on a pack that is going
  // away, and the only moment the warning can still change their mind. Fail-soft
  // in the same direction as the badges above: no notice, never an invented one.
  const [packDeprecations, setPackDeprecations] = useState<
    Record<string, PackDeprecationNotice>
  >({});
  useEffect(() => {
    let cancelled = false;
    fetchPackStates()
      .then(response => {
        if (cancelled) return;
        setPackCertifications(certificationsByPackId(response));
        setPackDeprecations(deprecationsByPackId(response));
        setCertificationPolicy(response.certificationPolicy ?? null);
        setPolicyUnavailable(isCertificationPolicyIndeterminate(response));
        setBlockedPacks(
          Object.fromEntries(
            (response.packs ?? [])
              .filter((pack: PackStateItem) => pack.activationBlocked)
              .map((pack: PackStateItem) => [
                pack.packId,
                pack.activationBlockedReason ?? 'Blocked by this organisation’s certification policy',
              ]),
          ),
        );
      })
      .catch(() => {
        if (cancelled) return;
        setPackCertifications({});
        setPackDeprecations({});
        setCertificationPolicy(null);
        // Cleared rather than set: this branch is the whole request failing, which
        // is a broader and more visible failure than the policy store alone being
        // unreadable. Clearing also stops a stale advisory surviving a retry.
        setPolicyUnavailable(false);
        setBlockedPacks({});
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const qualityRows = calcQualityRows(
    state.selectedSystemIds,
    state.weightings,
  );

  const recommendations = calcRecommendedAdditions(
    state.selectedSystemIds,
    state.focusId,
    state.industryId,
  );

  const primaryIds = state.selectedSystemIds.filter(id =>
    state.weightings[id]?.priority === 'primary',
  );

  const signalIds = state.selectedSystemIds.filter(id => !primaryIds.includes(id));

  const operationalSources = state.selectedSystemIds.filter(id =>
    state.weightings[id]?.role === 'operational_signal_source',
  );

  const docSystems = state.selectedSystemIds.filter(id =>
    state.weightings[id]?.role === 'documentation_system',
  );

  const confidenceLabel = confidence.level.charAt(0).toUpperCase() + confidence.level.slice(1);
  const industryLabel = state.industryId
    ? industries.find(item => item.industry_id === state.industryId)?.label
      ?? state.industryId
    : '-';
  const selectedTemplateIds = state.templateIds?.length
    ? state.templateIds
    : (state.templateId ? [state.templateId] : []);
  const templateLabel = selectedTemplateIds.length
    ? selectedTemplateIds
        .map(id => templates.find(item => item.template_id === id)?.label ?? id)
        .join(' + ')
    : '-';
  // Salesforce packs are FIXED by the Integration Hub product declaration — shown
  // read-only; they are not chosen here.
  void activePackId; void activePackIds;
  const salesforcePackLabel =
    salesforcePacks.length > 0
      ? salesforcePacks.map(analysisPackLabelFor).join(', ')
      : '-';
  // The analysis pack is chosen per run in this panel — ONE pack at most. It is a
  // single-select over the ANALYSIS_PACKS options only, so the dropdown never has
  // to represent a pack it does not offer:
  //   • the fixed Salesforce packs (declared in the Integration Hub) are shown in
  //     the read-only row above and are never dropped by a change here, and
  //   • a pack a TEMPLATE contributed that is not an offered analysis option
  //     (e.g. 'ncino') stays on state.packIds untouched.
  // Both live on state.packIds alongside the chosen analysis pack, and
  // resolvePackIds unions them for the run — unchanged by this control.
  //
  // The displayed value comes from resolveAnalysisPackId, the SAME helper
  // resolvePackIds uses to build the launch payload, so the menu always shows the
  // pack the run will actually activate. That includes the cloud-events default:
  // selecting AWS/Azure Events on Step 2 pre-selects Cloud Ops here (the runner
  // only polls those connectors when a cloud_ops pack is selected). Choosing
  // anything here — None included — marks the slot touched and wins from then on.
  const selectedAnalysisId = resolveAnalysisPackId(
    state.packIds,
    state.selectedSystemIds,
    state.analysisPackTouched,
  );
  const selectAnalysisPack = (packId: string) => {
    setupState.setAnalysisPack(packId);
  };
  const selectedAnalysisPack = ANALYSIS_PACKS.find(
    pack => pack.id === selectedAnalysisId,
  );

  return (
    <div className="space-y-5">
      <div className="grid gap-4 xl:grid-cols-2">
        <section className="rounded-xl border border-border bg-panel p-5 shadow-sm">
          <div className="mb-3 flex items-center gap-2">
            <FileText size={16} className="text-accent" aria-hidden="true" />
            <h2 className="text-sm font-semibold text-text">Configuration summary</h2>
          </div>
          <div>
            <SummaryRow
              label="Discovery focus"
              value={state.focusId ? FOCUS_LABELS[state.focusId] : '-'}
              valueClass="text-blue-100"
            />
            <SummaryRow
              label="Industry"
              value={industryLabel}
            />
            <SummaryRow
              label="Template"
              value={templateLabel}
            />
            {policyUnavailable && (
              <p
                data-testid="certification-policy-unavailable"
                className="mb-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-2.5 py-1.5 text-[11px] leading-relaxed text-amber-700"
              >
                This organisation’s pack certification policy could not be read, so
                pack eligibility cannot be shown. Launching may be refused until the
                policy is available again.
              </p>
            )}
            {certificationPolicy?.restricted && (
              <p
                data-testid="certification-policy-banner"
                className="mb-2 rounded-lg border border-blue-500/30 bg-blue-500/10 px-2.5 py-1.5 text-[11px] leading-relaxed text-blue-700"
              >
                This organisation only activates packs certified{' '}
                {certificationPolicy.label.toLowerCase()}.
                {certificationPolicy.reason ? ` ${certificationPolicy.reason}.` : ''}
              </p>
            )}
            {salesforcePacks.length > 0 && (
              <>
                <SummaryRow
                  label="Salesforce packs"
                  value={salesforcePackLabel}
                />
                <div data-testid="salesforce-pack-certifications" className="flex flex-wrap justify-end gap-1.5 pb-2">
                  {salesforcePacks.map(packId => (
                    <React.Fragment key={packId}>
                      <PackCertificationBadge
                        level={packCertifications[packId]?.level}
                        label={packCertifications[packId]?.label}
                        reviewDue={packCertifications[packId]?.reviewDue}
                        testId={`selection-pack-certification-${packId}`}
                      />
                      {/* 2.0-C4 T2 (AT-843 / AC1): a superseded pack says so where
                          it is SELECTED, beside its certification pill. */}
                      <PackDeprecationBadge
                        phase={packDeprecations[packId]?.phase}
                        label={packDeprecations[packId]?.statusLabel}
                        notice={packDeprecations[packId]?.summary}
                        testId={`selection-pack-deprecation-${packId}`}
                      />
                    </React.Fragment>
                  ))}
                </div>
                {salesforcePacks
                  .filter(packId => packDeprecations[packId])
                  .map(packId => (
                    <PackDeprecationDetail
                      key={packId}
                      phase={packDeprecations[packId].phase}
                      notice={packDeprecations[packId].summary}
                      graceEndsOn={packDeprecations[packId].graceEndsOn}
                      replacementLabel={packDeprecations[packId].replacementLabel}
                      daysRemaining={packDeprecations[packId].daysRemaining}
                      testId={`selection-pack-deprecation-detail-${packId}`}
                    />
                  ))}
              </>
            )}
            {/* Analysis pack — chosen per run (non-Salesforce). SINGLE-select
                dropdown, defaulting to None. */}
            <div className="border-b border-border/70 py-2">
              <div className="flex items-center justify-between gap-3">
                <label htmlFor="analysis-pack-select" className="text-xs text-muted">
                  Analysis pack
                </label>
                <select
                  id="analysis-pack-select"
                  value={selectedAnalysisId}
                  onChange={e => selectAnalysisPack(e.target.value)}
                  className="max-w-[60%] cursor-pointer truncate rounded-lg border border-border bg-panel px-2.5 py-1.5 text-xs text-text focus:outline-none focus:ring-1 focus:ring-accent"
                >
                  <option value="">None</option>
                  {ANALYSIS_PACKS.map(pack => (
                    <option key={pack.id} value={pack.id}>
                      {pack.label}
                    </option>
                  ))}
                </select>
              </div>
              {selectedAnalysisPack && (
                <div className="mt-1.5 flex flex-wrap items-center justify-between gap-2">
                  <p className="text-[11px] leading-relaxed text-muted">
                    {selectedAnalysisPack.description}
                  </p>
                  <div className="flex flex-wrap items-center gap-1.5">
                    <PackCertificationBadge
                      level={packCertifications[selectedAnalysisPack.id]?.level}
                      label={packCertifications[selectedAnalysisPack.id]?.label}
                      reviewDue={packCertifications[selectedAnalysisPack.id]?.reviewDue}
                      reviewDueDetail={packCertifications[selectedAnalysisPack.id]?.reviewDueDetail}
                      testId={`selection-pack-certification-${selectedAnalysisPack.id}`}
                    />
                    <PackDeprecationBadge
                      phase={packDeprecations[selectedAnalysisPack.id]?.phase}
                      label={packDeprecations[selectedAnalysisPack.id]?.statusLabel}
                      notice={packDeprecations[selectedAnalysisPack.id]?.summary}
                      testId={`selection-pack-deprecation-${selectedAnalysisPack.id}`}
                    />
                  </div>
                </div>
              )}
              {/* The full notice, with the date support ends and the replacement
                  spelled out — this is the moment the customer can still act on it. */}
              {selectedAnalysisPack && packDeprecations[selectedAnalysisPack.id] && (
                <div className="mt-1.5">
                  <PackDeprecationDetail
                    phase={packDeprecations[selectedAnalysisPack.id].phase}
                    notice={packDeprecations[selectedAnalysisPack.id].summary}
                    graceEndsOn={packDeprecations[selectedAnalysisPack.id].graceEndsOn}
                    replacementLabel={packDeprecations[selectedAnalysisPack.id].replacementLabel}
                    daysRemaining={packDeprecations[selectedAnalysisPack.id].daysRemaining}
                    testId="analysis-pack-deprecation"
                  />
                  {/* 2.0-C4 T3 (AT-844 / AC2): the PATH, beside the notice that
                      announced the problem. Renders nothing unless a replacement is
                      declared AND this org's saved configuration actually selects
                      the pack — see PackMigrationAssist. */}
                  <PackMigrationAssist
                    packId={selectedAnalysisPack.id}
                    testId="analysis-pack-migration"
                  />
                </div>
              )}
              {selectedAnalysisPack && blockedPacks[selectedAnalysisPack.id] && (
                <p
                  data-testid="analysis-pack-blocked"
                  className="mt-1.5 rounded-lg border border-amber-500/30 bg-amber-500/10 px-2.5 py-1.5 text-[11px] leading-relaxed text-amber-700"
                >
                  {blockedPacks[selectedAnalysisPack.id]}. This run cannot start
                  while it is selected.
                </p>
              )}
            </div>
            <SummaryRow
              label="Total systems"
              value={String(state.selectedSystemIds.length)}
            />
            <SummaryRow
              label="Primary systems"
              value={primaryIds.length > 0
                ? `${primaryIds.length} - ${primaryIds.map(getSystemName).join(', ')}`
                : '-'}
            />
            <SummaryRow
              label="Operational signal sources"
              value={operationalSources.length > 0
                ? `${operationalSources.length} - ${operationalSources.map(getSystemName).join(', ')}`
                : '-'}
            />
            <SummaryRow
              label="Documentation systems"
              value={docSystems.length > 0
                ? `${docSystems.length} - ${docSystems.map(getSystemName).join(', ')}`
                : '-'}
            />
          </div>
        </section>

        <section className="rounded-xl border border-border bg-panel p-5 shadow-sm">
          <div className="mb-3 flex items-center gap-2">
            <LineChart size={16} className="text-accent" aria-hidden="true" />
            <h2 className="text-sm font-semibold text-text">Expected discovery quality</h2>
          </div>
          <div className="space-y-3">
            {qualityRows.map(row => (
              <div key={row.label} className="flex items-start gap-2">
                <QualityDot level={row.level} />
                <div>
                  <div className="mb-0.5 text-xs font-medium leading-tight text-text">
                    {row.label}
                  </div>
                  <div className="text-xs leading-relaxed text-muted">
                    {row.descriptor}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </section>
      </div>

      <section className="rounded-xl border border-border bg-panel p-5 shadow-sm">
        <div className="mb-3 flex items-center gap-2">
          <Grid2X2 size={16} className="text-accent" aria-hidden="true" />
          <h2 className="text-sm font-semibold text-text">Selected systems</h2>
        </div>

        <div role="list" className="mb-3 flex flex-wrap gap-2">
          {primaryIds.map(id => (
            <SystemChip key={id} name={getSystemName(id)} variant="primary" />
          ))}
          {signalIds.map(id => (
            <SystemChip key={id} name={getSystemName(id)} variant="signal" />
          ))}
        </div>

        <div className="flex flex-wrap items-center gap-4">
          <div className="flex items-center gap-1.5">
            <span className="h-2 w-2 flex-shrink-0 rounded-full bg-accent" aria-hidden="true" />
            <span className="text-xs text-muted">Primary priority</span>
          </div>
          <div className="flex items-center gap-1.5">
            <span className="h-2 w-2 flex-shrink-0 rounded-full bg-muted" aria-hidden="true" />
            <span className="text-xs text-muted">Operational signal source / documentation</span>
          </div>
        </div>
      </section>

      {/* ENG-SB-1 Sprint 9 (0-pt sub-task): Recommended additions section removed.
           Screen 2 (YourSystemsPage) now shows missing category prompts.
           Screen 4 is summary and launch only. */}

      <section className="rounded-xl border border-accent/30 bg-accent/10 p-5 shadow-sm">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
          <div>
            <h2 className="text-sm font-semibold text-text">Ready to discover</h2>
            <p className="mt-1 text-sm leading-relaxed text-muted">
              {state.selectedSystemIds.length} system{state.selectedSystemIds.length !== 1 ? 's' : ''}
              {state.industryId ? ` / ${industryLabel}` : ''}
              {state.focusId ? ` / ${FOCUS_LABELS[state.focusId]} focus` : ''}
              {' / '}Confidence: {confidenceLabel}
            </p>
          </div>

          <Button
            variant="tertiary"
            onClick={onLaunch}
            disabled={launchState === 'launching'}
            ariaLabel={`Start discovery - confidence ${confidence.level}`}
            className="gap-2"
          >
            {launchState === 'launching' ? (
              <Clock3 size={16} className="animate-spin" aria-hidden="true" />
            ) : (
              <Play size={16} fill="currentColor" strokeWidth={2.2} aria-hidden="true" />
            )}
            {launchState === 'launching' ? 'Starting discovery...' : 'Start discovery'}
          </Button>
        </div>
      </section>

      <div className="rounded-xl border border-border bg-panel p-4 shadow-sm">
        <Button variant="tertiary" onClick={() => setupState.goTo(3)} className="gap-2">
          <ArrowLeft size={16} strokeWidth={2.2} aria-hidden="true" />
          Back
        </Button>
      </div>
    </div>
  );
}
