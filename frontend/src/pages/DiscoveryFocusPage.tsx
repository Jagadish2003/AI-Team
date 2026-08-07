import React from 'react';
import { Info, MoveRight } from 'lucide-react';
import Button from '../components/common/Button';
import InlineError from '../components/common/InlineError';
import { Skeleton } from '../components/common/Skeleton';
import {
  FocusCard as FocusCardType,
  IndustryListItem,
  TemplateListItem,
  SystemDefaultItem,
} from '../types/stack_builder';
import {
  FocusCard,
  PillTag,
  TemplateNoteBlock,
} from '../components/stack_builder';
import { useSetupState } from '../components/stack_builder';

// R16-C2 T5 — Tile boundary copy (Section 3).
// Each subtext now states what the focus EMPHASISES and draws an explicit
// boundary against the adjacent tile it is most often confused with. This is
// honest copy: after R16-C2 T1–T4 each focus genuinely re-ranks discovery
// toward the detector affinities listed in backend/discovery/packs/
// focus_affinity.py (emphasis, not exclusion), so the boundary language
// describes real, differentiated behaviour rather than overlapping marketing.
// Keep these crisp and scannable — they are selection cards, not docs.
// Exported so the AC6 differentiation can be asserted in tests.
//
// NOTE (R18-C1 T3): focus tiles are NOT registry-driven — the focus lens is a
// fixed AgentIQ concept wired to focus_affinity.py, not a customer template.
// Industries and templates, by contrast, now come from the backend registry
// (see StackBuilderPage → GET /api/stack-builder/industries · /templates); the
// hardcoded INDUSTRIES / TEMPLATES arrays that used to live here are gone.
export const FOCUS_CARDS: FocusCardType[] = [
  {
    id: 'member_customer_service',
    title: 'Member / customer service',
    subtext: 'Emphasises front-line service quality — repetitive case handling, agent knowledge gaps, and stalled member or customer requests. Use when outcomes for the person being served are at stake; for internal staff effort, choose Back-office productivity.',
    useWhen: 'the pain shows up in cases, requests, escalations, or customer-facing follow-up.',
    notWhen: 'the blocker is primarily an approval gate, compliance control, or internal queue.',
    icon: 'ti-users',
  },
  {
    id: 'core_operations',
    title: 'Core operations',
    subtext: 'Emphasises throughput in the main operating workflow — queue backlogs, volume surges, and slow resolution. Use when work piles up in one place; not blocked at an approval gate (Approvals / compliance) or stuck between systems (Cross-system handoffs).',
    useWhen: 'volume, backlog, throughput, or processing speed is the main operating concern.',
    notWhen: 'a specific regulatory gate or cross-system ownership break is the main bottleneck.',
    icon: 'ti-settings',
  },
  {
    id: 'approvals_compliance',
    title: 'Approvals / compliance',
    subtext: 'Emphasises work blocked at a gate — approval and permission bottlenecks, SLA breaches, compliance deadlines, and regulatory control points. Use when a stuck approval gate is the bottleneck; for work stuck moving between teams, choose Cross-system handoffs.',
    useWhen: 'a gate, deadline, audit trail, permission, or regulatory control is slowing work.',
    notWhen: 'the issue is ordinary queue volume or a handoff between teams and tools.',
    icon: 'ti-shield',
  },
  {
    id: 'cross_system_handoffs',
    title: 'Cross-system handoffs',
    subtext: 'Emphasises work lost moving between teams and tools — ownership friction, dropped handoffs, cross-system echo, and integration-point failures. Use when work stalls in the gap between systems; if it is held at a gate, choose Approvals / compliance.',
    useWhen: 'work is lost between teams, duplicated across tools, or waiting on ownership transfer.',
    notWhen: 'one approval or compliance gate is the bottleneck inside a single workflow.',
    icon: 'ti-switch-horizontal',
  },
  {
    id: 'back_office_productivity',
    title: 'Back-office productivity',
    subtext: 'Emphasises internal staff effort — repetitive manual work, checklist stalls, and admin-heavy prep with clear automation upside. Use when the cost is back-office time; for front-line member or customer outcomes, choose Member / customer service.',
    useWhen: 'manual admin, document prep, checklists, or data clean-up consume staff time.',
    notWhen: 'front-line service outcomes or engineering release health are the main lens.',
    icon: 'ti-list-check',
  },
  {
    id: 'engineering_change',
    title: 'Engineering / change',
    subtext: 'Emphasises delivery and release health — pull-request review bottlenecks, commit concentration risk, stale branches, and change-driven incidents. Use when engineering change flow is the friction; for operational queues, choose Core operations.',
    useWhen: 'PR review, release flow, change incidents, branches, or engineering backlog drive risk.',
    notWhen: 'business approvals, service queues, or back-office admin are the main concern.',
    icon: 'ti-git-branch',
  },
  {
    id: 'enterprise_wide',
    title: 'Enterprise-wide discovery',
    subtext: 'No lens applied — the unbiased, full view across every connected system. Use this when you do not want to pre-commit to a focus: AgentIQ ranks all findings on merit and emphasises nothing over anything else. Not a specialised focus.',
    useWhen: 'you want the unbiased full view across the selected systems and active pack.',
    notWhen: 'you want one operational lens boosted ahead of the rest from the start.',
    icon: 'ti-world',
    wide: true,
  },
];

interface Props {
  setupState: ReturnType<typeof useSetupState>;
  // Registry data (R18-C1 T3) — supplied by StackBuilderPage from the backend
  // registry / template model. The picker renders exactly what the backend
  // returns; there is no local INDUSTRIES / TEMPLATES fallback (AC7/AC8/AC10).
  industries: IndustryListItem[];
  templates: TemplateListItem[];
  registryLoading: boolean;
  registryError: string | null;
  onRetryRegistry: () => void;
  fetchSystemDefaults: (industryId: string) => Promise<SystemDefaultItem[]>;
  // Optional guidance panel for the selected template, rendered BELOW the
  // Industry / template pickers. It sits here rather than above the step because
  // the template that produces it is chosen in the section directly above — a
  // guide rendered at the top of the page appeared off-screen from its own
  // trigger, so selecting a template meant scrolling back up to read the result.
  guide?: React.ReactNode;
}

// Compact inline "load failed → retry" block for the industry / template
// pickers. On failure we render THIS, never a stale hardcoded list — showing old
// local data would falsely imply the user is seeing current backend config
// (R18-C1 T3 / AC10).
function RegistryRetry({ message, onRetry }: { message: string; onRetry: () => void }) {
  return <InlineError title="Couldn't load from the registry" message={message} onRetry={onRetry} />;
}

// Pill-shaped skeletons that mirror the final PillTag option row, so the section
// reserves its space and reads as "options loading" instead of flashing a tiny
// spinner and then popping the pills in (which made selection feel glitchy).
function SkeletonPills({ label }: { label: string }) {
  const widths = ['w-24', 'w-32', 'w-20', 'w-28', 'w-24', 'w-32'];
  return (
    <div className="flex flex-wrap gap-2" aria-busy="true" aria-label={label}>
      {widths.map((w, i) => (
        <Skeleton key={i} className={`h-8 ${w} rounded-full`} />
      ))}
    </div>
  );
}

export default function DiscoveryFocusPage({
  setupState,
  industries,
  templates,
  registryLoading,
  registryError,
  onRetryRegistry,
  fetchSystemDefaults,
  guide,
}: Props) {
  const {
    state,
    setFocus,
    setIndustry,
    applyTemplate,
    applyIndustryDefaults,
    goTo,
    canProceedFromStep1,
  } = setupState;

  const selectedTemplateIds = state.templateIds?.length
    ? state.templateIds
    : (state.templateId ? [state.templateId] : []);
  const selectedTemplates = templates.filter(template =>
    selectedTemplateIds.includes(template.template_id),
  );

  async function handleIndustrySelect(industryId: string) {
    const next = state.industryId === industryId ? null : industryId;
    setIndustry(next);
    if (!next) return;

    // Every industry fetches its defaults. There is deliberately no "unwired
    // industry" skip list here: the anchor-on-shipped rule moved unshipped
    // anchors (SAP/D365 for manufacturing and logistics, GitLab for technology)
    // into `roadmap_systems`, and it left every one of those industries with
    // real shipped defaults — so skipping the fetch suppressed pre-population
    // that the registry genuinely had, with no visible reason. The backend is
    // the authority on what is connectable, so a roadmap system can never
    // arrive through this path.
    // AC9: choosing an industry applies its registry-calibrated system defaults
    // through the API path (editable, never confirmed here). Failure to fetch
    // them is non-blocking — the industry stays selected.
    try {
      const defaults = await fetchSystemDefaults(next);
      applyIndustryDefaults(defaults);
    } catch {
      // Industry defaults are an accelerator, not a gate.
    }
  }

  function handleTemplateSelect(template: TemplateListItem) {
    // The hook toggles this registry template while retaining any other selected
    // template, allowing compatible packs to share one discovery run.
    applyTemplate(template);
  }

  function handleContinue() {
    if (!canProceedFromStep1) return;
    goTo(2);
  }

  return (
    <div className="space-y-5">
      <section className="rounded-xl border border-border bg-panel p-5 shadow-sm">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="text-lg font-semibold text-text">Discovery focus</h2>
            <p className="mt-1 text-sm leading-relaxed text-muted">
              Select the operating lens AgentIQ should prioritize first.
            </p>
          </div>
          <span className="rounded-full border border-red-500/30 bg-red-500/10 px-2.5 py-1 text-xs font-medium text-red-200">
            Required
          </span>
        </div>

        <div
          role="radiogroup"
          aria-label="Discovery focus"
          className="grid grid-cols-1 gap-3 md:grid-cols-2"
        >
          {FOCUS_CARDS.map(card => (
            <FocusCard
              key={card.id}
              card={card}
              selected={state.focusId === card.id}
              onSelect={setFocus}
            />
          ))}
        </div>
      </section>

      <div className="grid gap-4 lg:grid-cols-2">
        <section className="rounded-xl border border-border bg-panel p-5 shadow-sm">
          <div className="mb-1 flex items-center gap-2">
            <h2 className="text-sm font-semibold text-text">Industry</h2>
            <span className="text-xs text-muted">Optional</span>
          </div>
          <p className="mb-4 text-xs leading-relaxed text-muted">
            Helps AgentIQ surface relevant systems and adapt signal language to your
            operating context.
          </p>
          {registryLoading ? (
            <SkeletonPills label="Loading industries…" />
          ) : registryError ? (
            <RegistryRetry message={registryError} onRetry={onRetryRegistry} />
          ) : (
            <div
              role="group"
              aria-label="Industry"
              className="flex flex-wrap gap-2"
            >
              {industries.map(ind => (
                <PillTag
                  key={ind.industry_id}
                  label={ind.label}
                  selected={state.industryId === ind.industry_id}
                  onToggle={() => handleIndustrySelect(ind.industry_id)}
                />
              ))}
            </div>
          )}
        </section>

        <section className="rounded-xl border border-border bg-panel p-5 shadow-sm">
          <div className="mb-1 flex items-center gap-2">
            <h2 className="text-sm font-semibold text-text">Start from a template</h2>
            <span className="text-xs text-muted">Optional</span>
          </div>
          <p className="mb-4 text-xs leading-relaxed text-muted">
            Choose one or more templates to pre-populate systems, roles, packs, and
            a focus for common operating models. Everything stays editable.
          </p>
          {registryLoading ? (
            <SkeletonPills label="Loading templates…" />
          ) : registryError ? (
            <RegistryRetry message={registryError} onRetry={onRetryRegistry} />
          ) : (
            <>
              <div
                role="group"
                aria-label="Start from a template"
                className="flex flex-wrap gap-2"
              >
                {templates.map(tmpl => (
                  <PillTag
                    key={tmpl.template_id}
                    label={tmpl.label}
                    selected={selectedTemplateIds.includes(tmpl.template_id)}
                    onToggle={() => handleTemplateSelect(tmpl)}
                  />
                ))}
              </div>

              {selectedTemplates.map(selectedTemplate => (
                <TemplateNoteBlock
                  key={selectedTemplate.template_id}
                  templateLabel={selectedTemplate.label}
                  suggestedFocus={selectedTemplate.focus_defaults.focus_id}
                />
              ))}
            </>
          )}
        </section>
      </div>

      {/* Template guidance — directly below the template picker that produces it,
          and above the Continue footer so it is read before moving on. */}
      {guide}

      <div className="rounded-xl border border-border bg-panel p-4 shadow-sm">
        <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
          <div className="flex items-start gap-2 text-sm text-muted">
            <Info size={16} className="mt-0.5 shrink-0 text-accent" aria-hidden="true" />
            <span>Discovery focus is required. Industry and template are optional accelerators.</span>
          </div>

          <Button
            variant="tertiary"
            onClick={handleContinue}
            disabled={!canProceedFromStep1}
            className="gap-2"
          >
            Continue to your systems
            <MoveRight size={16} strokeWidth={2.2} aria-hidden="true" />
          </Button>
        </div>
      </div>
    </div>
  );
}
