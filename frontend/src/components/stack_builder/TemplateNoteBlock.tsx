/**
 * TemplateNoteBlock — SB-7 Sprint 7
 *
 * Shown on Screen 1 when a template is selected.
 * Rendered below the template selector panel.
 *
 * Displays:
 *   - Clarifying copy: "Templates preselect systems and suggest a default focus.
 *     Your selected discovery focus still takes priority."
 *   - Suggested focus line: "✦ Commercial lending suggests: Approvals / compliance"
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

// R18-C1 T3: labels are registry-driven now — the template label comes from the
// backend template model (passed in by the caller) rather than a hardcoded
// TemplateId→label map, so a relabelled or newly-added template shows correct
// copy with no frontend change. The focus label map stays because the focus
// tiles are NOT registry-driven in T3; an unknown focus id falls back to itself.
const FOCUS_LABELS: Record<string, string> = {
  member_customer_service:  'Member / customer service',
  core_operations:          'Core operations',
  approvals_compliance:     'Approvals / compliance',
  cross_system_handoffs:    'Cross-system handoffs',
  back_office_productivity: 'Back-office productivity',
  engineering_change:       'Engineering / change',
  enterprise_wide:          'Enterprise-wide discovery',
};

interface Props {
  templateLabel: string;
  suggestedFocus: string;
}

export default function TemplateNoteBlock({ templateLabel, suggestedFocus }: Props) {
  return (
    <div className="mt-3 rounded-r-lg border-l-2 border-accent/60 bg-accent/10 px-3 py-2.5">
      <p className="text-xs text-muted leading-relaxed mb-1.5">
        Templates preselect systems, roles, and a focus as a starting point.{' '}
        <span className="text-text">
          Everything stays editable before you launch.
        </span>
      </p>
      <p className="text-xs text-blue-100 font-medium">
        ✦ {templateLabel} suggests:{' '}
        <span className="font-semibold">
          {FOCUS_LABELS[suggestedFocus] ?? suggestedFocus}
        </span>
      </p>
    </div>
  );
}
