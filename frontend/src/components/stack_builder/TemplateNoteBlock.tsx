/**
 * TemplateNoteBlock — SB-7 Sprint 7
 *
 * Shown on Screen 1 when a template is selected.
 * Rendered below the template selector panel.
 *
 * Displays:
 *   - Clarifying copy: "Templates preselect systems and suggest a default focus.
 *     Your selected discovery focus still takes priority."
 *   - Suggested focus line: "✦ Public retirement suggests: Approvals / compliance"
 *
 * Visual:
 *   Left border: border-emerald-500/40 (teal)
 *   Background: bg-emerald-500/[0.05] (very subtle teal tint)
 *   Suggested focus text: text-emerald-600
 *   Consistent with the teal selection family used across the stack builder.
 *
 * Token note:
 *   v1.1 used border-accent/40 bg-accent/5 text-accent (blue).
 *   Corrected to emerald/teal — consistent with FocusCard, PillTag,
 *   and all other interactive elements in the stack builder.
 *   Wireframe Image 1 shows a teal left border and teal text on the
 *   suggested focus line.
 *
 * Visibility:
 *   Component only renders when templateId is provided.
 *   Parent (Screen 1) conditionally renders this component when
 *   state.templateId is non-null.
 *
 * Accessibility:
 *   Purely informational. No interactive elements.
 *   ✦ symbol is decorative — treated as text by screen readers.
 *   Consider aria-label on the block if screen reader UX is a concern.
 *
 * Props:
 *   templateId     — selected TemplateId — used to derive the template label
 *   suggestedFocus — FocusId suggested by the template
 *
 * Usage:
 *   const { state } = useSetupState();
 *   {state.templateId && (
 *     <TemplateNoteBlock
 *       templateId={state.templateId}
 *       suggestedFocus={TEMPLATE_SUGGESTED_FOCUS[state.templateId]}
 *     />
 *   )}
 */

import React from 'react';
import { TemplateId, FocusId } from '../../types/stack_builder';

const TEMPLATE_LABELS: Record<TemplateId, string> = {
  commercial_lending: 'Commercial lending',
  public_retirement:  'Public retirement',
  service_operations: 'Service operations',
  revenue_operations: 'Revenue operations',
};

const FOCUS_LABELS: Record<FocusId, string> = {
  member_customer_service:  'Member / customer service',
  core_operations:          'Core operations',
  approvals_compliance:     'Approvals / compliance',
  cross_system_handoffs:    'Cross-system handoffs',
  back_office_productivity: 'Back-office productivity',
  engineering_change:       'Engineering / change',
  enterprise_wide:          'Enterprise-wide discovery',
};

interface Props {
  templateId: TemplateId;
  suggestedFocus: FocusId;
}

export default function TemplateNoteBlock({ templateId, suggestedFocus }: Props) {
  return (
    <div className="mt-3 rounded-r-lg border-l-2 border-accent/60 bg-accent/10 px-3 py-2.5">
      <p className="text-xs text-muted leading-relaxed mb-1.5">
        Templates preselect systems and suggest a default focus.{' '}
        <span className="text-text">
          Your selected discovery focus still takes priority.
        </span>
      </p>
      <p className="text-xs text-blue-100 font-medium">
        ✦ {TEMPLATE_LABELS[templateId]} suggests:{' '}
        <span className="font-semibold">{FOCUS_LABELS[suggestedFocus]}</span>
      </p>
    </div>
  );
}
