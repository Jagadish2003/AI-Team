/**
 * SystemWeightingCard — SB-1 v1.1 Task 5 Sprint 7
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
    role:      'bg-emerald-500/10 border-emerald-500/20 text-emerald-600',
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
    <div id={id} className="rounded-xl border border-border bg-panel overflow-hidden mb-3">

      {/* ── Collapsed header — always visible ── */}
      <button
        type="button"
        onClick={() => setExpanded(e => !e)}
        className={[
          'w-full flex items-center gap-3 px-4 py-3 text-left transition-colors',
          'focus:outline-none focus:ring-2 focus:ring-emerald-500/50 focus:ring-inset',
          expanded ? 'bg-emerald-500/[0.03]' : 'hover:bg-emerald-500/[0.04]',
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
          <span className="flex items-center gap-1 text-xs text-emerald-500 font-medium flex-shrink-0 mr-2">
            <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true">
              <path d="M2 6l3 3 5-5" stroke="currentColor" strokeWidth="1.5"
                strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
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
        <div className="border-t border-border bg-panel px-4 py-4 space-y-5">

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
                'inline-flex items-center gap-1.5 rounded-md px-4 py-2',
                'text-sm font-medium text-white',
                'bg-emerald-500 hover:bg-emerald-600 transition-colors',
                'focus:outline-none focus:ring-2 focus:ring-emerald-500/50',
              ].join(' ')}
            >
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
                <path d="M2.5 7l3 3 6-6" stroke="currentColor" strokeWidth="1.5"
                  strokeLinecap="round" strokeLinejoin="round"/>
              </svg>
              Confirm
            </button>
          </div>

        </div>
      )}
    </div>
  );
}
