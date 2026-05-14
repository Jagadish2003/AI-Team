/**
 * TemplateNoteBlock — SB-1 Sprint 7
 *
 * Shown on Screen 1 when a template is selected.
 * Displays:
 *   - The clarifying copy: "Templates preselect systems and suggest a default focus.
 *     Your selected discovery focus still takes priority."
 *   - The suggested focus: "Public retirement suggests: Approvals / compliance"
 *
 * Visible only when templateId is non-null.
 */

import React from 'react';
import { TemplateId, FocusId } from '../../types/stack_builder';

const TEMPLATE_LABELS: Record<TemplateId, string> = {
  commercial_lending: 'Commercial lending',
  public_retirement: 'Public retirement',
  service_operations: 'Service operations',
  revenue_operations: 'Revenue operations',
};

const FOCUS_LABELS: Record<FocusId, string> = {
  member_customer_service: 'Member / customer service',
  core_operations: 'Core operations',
  approvals_compliance: 'Approvals / compliance',
  cross_system_handoffs: 'Cross-system handoffs',
  back_office_productivity: 'Back-office productivity',
  engineering_change: 'Engineering / change',
  enterprise_wide: 'Enterprise-wide discovery',
};

interface Props {
  templateId: TemplateId;
  suggestedFocus: FocusId;
}

export default function TemplateNoteBlock({ templateId, suggestedFocus }: Props) {
  return (
    <div className="mt-3 border-l-2 border-accent/40 bg-accent/5 rounded-r-lg px-3 py-2.5">
      <p className="text-xs text-muted leading-relaxed mb-1.5">
        Templates preselect systems and suggest a default focus.{' '}
        <span className="text-text">Your selected discovery focus still takes priority.</span>
      </p>
      <p className="text-xs text-accent font-medium">
        ✦ {TEMPLATE_LABELS[templateId]} suggests:{' '}
        <span className="font-semibold">{FOCUS_LABELS[suggestedFocus]}</span>
      </p>
    </div>
  );
}
