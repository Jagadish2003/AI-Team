/**
 * SystemWeightingCard — SB-5 Sprint 7
 *
 * Collapsed / expandable weighting card for Screen 3 (Source Weighting).
 * One card per selected system. Stacked vertically.
 *
 * Collapsed state shows:
 *   - Logo initials block + system name
 *   - Priority badge (muted pill)
 *   - Role badge (teal pill — role is the primary classification)
 *   - Workflow focus summary tags (muted pills, dot-separated)
 *   - "✓ Confirmed" label when weighting.confirmed is true
 *   - Chevron toggle
 *
 * Expanded state shows:
 *   - PRIMARY ROLE — single-select PillTag × 5
 *     Engineering / change system faded when showEngineeringRole=false
 *   - PRIORITY — single-select PillTag × 3
 *   - WORKFLOW FOCUS — select up to 3 — PillTag × 10, disabled at max
 *     "N of 3 selected" count below
 *   - Confirm button — emerald, closes expand and marks confirmed
 *
 * Border radius:
 *   rounded-xl — this is a larger container card, not a selection card.
 *   Consistent with the wireframe. Selection cards (FocusCard, SystemCard)
 *   use rounded-lg.
 *
 * Token note:
 *   Role badge uses emerald teal — it is the primary classification signal.
 *   Priority and focus summary badges use muted/border — secondary information.
 *   Confirm button uses emerald-500 — consistent with the emerald action
 *   colour family used across the stack builder.
 *   accent token (#0D55D7, blue) is not used in this component.
 *
 * PillTag note:
 *   PillTag selected state currently uses accent (blue) per SB-1 v1.1.
 *   PillTag colour correction is tracked as a separate task (Task 6).
 *   This component is correct — PillTag will be corrected independently.
 *
 * Accessibility:
 *   Header is a <button> — keyboard focusable, Enter/Space toggles expand.
 *   aria-expanded reflects expansion state.
 *   Section labels use uppercase tracking-wide — visual only, readable by SR.
 *   Logo block aria-hidden — decorative.
 *   Confirm button is a <button type="button">.
 *
 * Props:
 *   id                 — DOM id for scroll targeting. Set to `weighting-card-${systemId}`.
 *                        Used by SourceWeightingScreen.onConfirm to scroll to next card.
 *   systemName         — display name of the system
 *   logoInitials       — 2-letter abbreviation e.g. "SF", "JR"
 *   logoColor          — Tailwind bg class e.g. "bg-teal-600"
 *   weighting          — SystemWeighting from state
 *   showEngineeringRole — false when no code/engineering systems selected
 *   onChange           — called with updated SystemWeighting on any field change
 *   onConfirm          — called after confirm button — parent can scroll to next card
 *
 * Usage:
 *   const { state, updateWeighting } = useSetupState();
 *   {state.selectedSystemIds.map(id => (
 *     <SystemWeightingCard
 *       key={id}
 *       systemName={SYSTEMS[id].name}
 *       logoInitials={SYSTEMS[id].logoInitials}
 *       logoColor={SYSTEMS[id].logoColor}
 *       weighting={state.weightings[id]}
 *       showEngineeringRole={showEngineeringRole}
 *       onChange={updated => updateWeighting(id, updated)}
 *       onConfirm={() => {}}
 *     />
 *   ))}
 */

import React, { useState } from 'react';
import { CircleCheck } from 'lucide-react';
import {
  SystemWeighting, SystemRole, SystemPriority, WorkflowFocusTag
} from '../../types/stack_builder';
import PillTag from './PillTag';

// ── Label maps ────────────────────────────────────────────────────────────────

const ROLE_LABELS: Record<SystemRole, string> = {
  system_of_record:          'System of record',
  workflow_system:           'Workflow system',
  operational_signal_source: 'Operational signal source',
  documentation_system:      'Documentation system',
  engineering_change_system: 'Engineering / change system',
};

const PRIORITY_LABELS: Record<SystemPriority, string> = {
  primary:   'Primary',
  secondary: 'Secondary',
  optional:  'Optional',
};

const WORKFLOW_LABELS: Record<WorkflowFocusTag, string> = {
  intake_requests:    'Intake & requests',
  service_casework:   'Service / casework',
  approvals:          'Approvals',
  backlog_work_queues:'Backlog / work queues',
  compliance_risk:    'Compliance / risk',
  documents_knowledge:'Documents / knowledge',
  handoffs_routing:   'Handoffs / routing',
  communications:     'Communications',
  change_release:     'Change & release',
  data_analytics:     'Data / analytics',
};

const ALL_WORKFLOW_TAGS: WorkflowFocusTag[] = [
  'intake_requests', 'service_casework', 'approvals',
  'backlog_work_queues', 'compliance_risk', 'documents_knowledge',
  'handoffs_routing', 'communications', 'change_release', 'data_analytics',
];

const ALL_ROLES: SystemRole[] = [
  'system_of_record', 'workflow_system', 'operational_signal_source',
  'documentation_system', 'engineering_change_system',
];

const ALL_PRIORITIES: SystemPriority[] = ['primary', 'secondary', 'optional'];

// ── Collapsed header badge ────────────────────────────────────────────────────
// Role badge: teal — primary classification signal
// Priority and focus badges: muted — secondary information

type BadgeVariant = 'role' | 'secondary';

function WeightingBadge({ label, variant }: { label: string; variant: BadgeVariant }) {
  const classes: Record<BadgeVariant, string> = {
    role:      'border-accent/30 bg-accent/15 text-blue-100',
    secondary: 'bg-panel border-border text-muted',
  };
  return (
    <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium ${classes[variant]}`}>
      {label}
    </span>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

interface Props {
  /** DOM id for scroll targeting — set to `weighting-card-${systemId}` by parent. */
  id: string;
  systemName: string;
  logoInitials: string;
  logoColor: string;
  weighting: SystemWeighting;
  showEngineeringRole: boolean;
  onChange: (updated: SystemWeighting) => void;
  onConfirm: () => void;
}

export default function SystemWeightingCard({
  id, systemName, logoInitials, logoColor,
  weighting, showEngineeringRole, onChange, onConfirm,
}: Props) {
  const [expanded, setExpanded] = useState(false);

  const workflowAtMax = weighting.workflowFocus.length >= 3;

  function setRole(role: SystemRole) {
    onChange({ ...weighting, role, confirmed: false });
  }

  function setPriority(priority: SystemPriority) {
    onChange({ ...weighting, priority, confirmed: false });
  }

  function toggleWorkflow(tag: WorkflowFocusTag) {
    const current = weighting.workflowFocus;
    const next = current.includes(tag)
      ? current.filter(t => t !== tag)
      : current.length < 3
      ? [...current, tag]
      : current;
    onChange({ ...weighting, workflowFocus: next, confirmed: false });
  }

  function handleConfirm() {
    onChange({ ...weighting, confirmed: true });
    setExpanded(false);
    onConfirm();
  }

  const focusSummary = weighting.workflowFocus
    .map(t => WORKFLOW_LABELS[t])
    .join(' · ');

  return (
    <div
      id={id}
      className={[
        'mb-3 rounded-xl border bg-panel transition-[border-color,box-shadow] duration-150',
        expanded
          ? 'border-accent shadow-[0_8px_22px_rgba(13,85,215,0.14)]'
          : weighting.confirmed
          ? 'border-emerald-500/45 shadow-sm'
          : 'border-border shadow-sm',
      ].join(' ')}
    >

      {/* ── Collapsed header — always visible ── */}
      <button
        type="button"
        onClick={() => setExpanded(e => !e)}
        className={[
          'w-full flex items-center gap-3 rounded-t-xl px-4 py-3 text-left transition-colors',
          'focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/35 focus-visible:ring-inset',
          expanded ? 'bg-accent/10' : 'hover:bg-panel2',
        ].join(' ')}
        aria-expanded={expanded}
      >
        {/* Logo block — decorative */}
        <div
          className={`h-9 w-9 flex-shrink-0 rounded-lg ${logoColor} flex items-center justify-center`}
          aria-hidden="true"
        >
          <span className="text-xs font-semibold text-white/80">{logoInitials}</span>
        </div>

        {/* System name + badge row */}
        <div className="flex-1 min-w-0">
          <div className="text-sm font-medium text-text mb-1.5">{systemName}</div>
          <div className="flex items-center gap-1.5 flex-wrap">
            <WeightingBadge label={PRIORITY_LABELS[weighting.priority]} variant="secondary" />
            <WeightingBadge label={ROLE_LABELS[weighting.role]} variant="role" />
            {focusSummary && (
              <WeightingBadge label={focusSummary} variant="secondary" />
            )}
          </div>
        </div>

        {/* Confirmed label */}
        {weighting.confirmed && !expanded && (
          <span className="mr-2 inline-flex flex-shrink-0 items-center gap-1.5 rounded-full border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 text-xs font-medium text-emerald-300 shadow-sm">
            <CircleCheck size={13} strokeWidth={2.2} aria-hidden="true" />
            Confirmed
          </span>
        )}

        {/* Chevron */}
        <svg
          className={`flex-shrink-0 text-muted transition-transform duration-200 ${expanded ? 'rotate-180' : ''}`}
          width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"
        >
          <path d="M4 6l4 4 4-4" stroke="currentColor" strokeWidth="1.5"
            strokeLinecap="round" strokeLinejoin="round"/>
        </svg>
      </button>

      {/* ── Expanded body ── */}
      {expanded && (
        <div className="space-y-5 rounded-b-xl border-t border-border bg-panel px-4 py-4">

          {/* PRIMARY ROLE */}
          <div>
            <div className="text-xs font-medium text-muted uppercase tracking-wide mb-2">
              Primary role
            </div>
            <div className="flex flex-wrap gap-2">
              {ALL_ROLES.map(role => {
                const isEngRole = role === 'engineering_change_system';
                const faded = isEngRole && !showEngineeringRole;
                return (
                  <div key={role} className={faded ? 'opacity-40' : ''}>
                    <PillTag
                      label={ROLE_LABELS[role]}
                      selected={weighting.role === role}
                      disabled={faded}
                      onToggle={() => !faded && setRole(role)}
                      size="md"
                    />
                  </div>
                );
              })}
            </div>
          </div>

          {/* PRIORITY */}
          <div>
            <div className="text-xs font-medium text-muted uppercase tracking-wide mb-2">
              Priority
            </div>
            <div className="flex flex-wrap gap-2">
              {ALL_PRIORITIES.map(priority => (
                <PillTag
                  key={priority}
                  label={PRIORITY_LABELS[priority]}
                  selected={weighting.priority === priority}
                  onToggle={() => setPriority(priority)}
                  size="md"
                />
              ))}
            </div>
          </div>

          {/* WORKFLOW FOCUS */}
          <div>
            <div className="flex items-center gap-2 mb-2">
              <div className="text-xs font-medium text-muted uppercase tracking-wide">
                Workflow focus
              </div>
              <span className="text-xs text-muted">— select up to 3</span>
            </div>
            <div className="flex flex-wrap gap-2 mb-2">
              {ALL_WORKFLOW_TAGS.map(tag => {
                const isSelected = weighting.workflowFocus.includes(tag);
                const isDisabled = workflowAtMax && !isSelected;
                return (
                  <PillTag
                    key={tag}
                    label={WORKFLOW_LABELS[tag]}
                    selected={isSelected}
                    disabled={isDisabled}
                    onToggle={() => toggleWorkflow(tag)}
                  />
                );
              })}
            </div>
            <div className="text-xs text-muted">
              {weighting.workflowFocus.length} of 3 selected
            </div>
          </div>

          {/* Confirm button */}
          <div className="flex justify-end pt-1">
            <button
              type="button"
              onClick={handleConfirm}
              className={[
                'inline-flex items-center rounded-md px-4 py-2',
                'border border-accent/20 bg-accent/5 text-sm font-medium text-accent',
                'transition-colors hover:border-accent/45 hover:bg-accent/10',
                'focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40',
              ].join(' ')}
            >
              Confirm
            </button>
          </div>

        </div>
      )}
    </div>
  );
}
