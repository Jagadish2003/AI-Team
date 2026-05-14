/**
 * SystemWeightingCard — SB-1 Sprint 7
 *
 * Collapsed/expandable card for Screen 3 (Source Weighting).
 * Each selected system gets one card.
 *
 * Collapsed state shows:
 *   - Logo initials + name
 *   - Role badge (accent/blue)
 *   - Priority badge
 *   - Workflow focus summary (up to 3 tag names)
 *   - "Confirmed" badge (green) when user has reviewed
 *   - Chevron to expand
 *
 * Expanded state shows:
 *   - Role single-select (PillTag × 5)
 *   - Priority single-select (PillTag × 3)
 *   - Workflow focus multi-select (PillTag × 10, max 3)
 *   - Tag count: "N of 3 selected"
 *
 * Engineering / Change System role is passed as conditionally relevant
 * from the parent — when not relevant, it is rendered with reduced opacity.
 */

import React, { useState } from 'react';
import {
  SystemWeighting, SystemRole, SystemPriority,
  WorkflowFocusTag
} from '../../types/stack_builder';
import PillTag from './PillTag';

// ── Label maps ────────────────────────────────────────────────────────────────

const ROLE_LABELS: Record<SystemRole, string> = {
  system_of_record: 'System of record',
  workflow_system: 'Workflow system',
  operational_signal_source: 'Operational signal source',
  documentation_system: 'Documentation system',
  engineering_change_system: 'Engineering / change system',
};

const PRIORITY_LABELS: Record<SystemPriority, string> = {
  primary: 'Primary',
  secondary: 'Secondary',
  optional: 'Optional',
};

const WORKFLOW_LABELS: Record<WorkflowFocusTag, string> = {
  intake_requests: 'Intake & requests',
  service_casework: 'Service / casework',
  approvals: 'Approvals',
  backlog_work_queues: 'Backlog / work queues',
  compliance_risk: 'Compliance / risk',
  documents_knowledge: 'Documents / knowledge',
  handoffs_routing: 'Handoffs / routing',
  communications: 'Communications',
  change_release: 'Change & release',
  data_analytics: 'Data / analytics',
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

// ── Badge ─────────────────────────────────────────────────────────────────────

function WeightingBadge({ label, variant }: { label: string; variant: 'role' | 'priority' | 'focus' }) {
  const classes = {
    role: 'bg-accent/10 border-accent/20 text-accent',
    priority: 'bg-panel2 border-border text-muted',
    focus: 'bg-panel2 border-border text-muted',
  };
  return (
    <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs ${classes[variant]}`}>
      {label}
    </span>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

interface Props {
  systemName: string;
  logoInitials: string;
  logoColor: string;
  weighting: SystemWeighting;
  showEngineeringRole: boolean;       // false when no code/engineering systems selected
  onChange: (updated: SystemWeighting) => void;
  onConfirm: () => void;
}

export default function SystemWeightingCard({
  systemName, logoInitials, logoColor,
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
    <div className="rounded-xl border border-border bg-panel overflow-hidden mb-3">

      {/* Header — always visible */}
      <button
        type="button"
        onClick={() => setExpanded(e => !e)}
        className="w-full flex items-center gap-3 px-4 py-3 text-left hover:bg-panel2 transition-colors focus:outline-none focus:ring-2 focus:ring-accent/50 focus:ring-inset"
        aria-expanded={expanded}
      >
        {/* Logo */}
        <div className={`h-9 w-9 flex-shrink-0 rounded-lg ${logoColor} flex items-center justify-center`}>
          <span className="text-xs font-semibold text-text/80">{logoInitials}</span>
        </div>

        {/* Info */}
        <div className="flex-1 min-w-0">
          <div className="text-sm font-medium text-text mb-1">{systemName}</div>
          <div className="flex items-center gap-2 flex-wrap">
            <WeightingBadge label={PRIORITY_LABELS[weighting.priority]} variant="priority" />
            <WeightingBadge label={ROLE_LABELS[weighting.role]} variant="role" />
            {focusSummary && (
              <WeightingBadge label={focusSummary} variant="focus" />
            )}
          </div>
        </div>

        {/* Confirmed badge */}
        {weighting.confirmed && !expanded && (
          <span className="flex items-center gap-1 text-xs text-emerald-400 font-medium flex-shrink-0 mr-2">
            <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true">
              <path d="M2 6l3 3 5-5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
            Confirmed
          </span>
        )}

        {/* Chevron */}
        <svg
          className={`flex-shrink-0 text-muted transition-transform ${expanded ? 'rotate-180' : ''}`}
          width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
          <path d="M4 6l4 4 4-4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
        </svg>
      </button>

      {/* Body — shown when expanded */}
      {expanded && (
        <div className="border-t border-border bg-panel2 px-4 py-4 space-y-5">

          {/* Role */}
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

          {/* Priority */}
          <div>
            <div className="text-xs font-medium text-muted uppercase tracking-wide mb-2">
              Priority
            </div>
            <div className="flex flex-wrap gap-2">
              {ALL_PRIORITIES.map(p => (
                <PillTag
                  key={p}
                  label={PRIORITY_LABELS[p]}
                  selected={weighting.priority === p}
                  onToggle={() => setPriority(p)}
                  size="md"
                />
              ))}
            </div>
          </div>

          {/* Workflow focus */}
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
              className="inline-flex items-center gap-1.5 rounded-md bg-accent px-4 py-2 text-sm font-medium text-white hover:opacity-90 focus:outline-none focus:ring-2 focus:ring-accent/50"
            >
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
                <path d="M2.5 7l3 3 6-6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
              </svg>
              Confirm
            </button>
          </div>

        </div>
      )}
    </div>
  );
}
