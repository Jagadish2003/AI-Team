/**
 * FocusCard - SB-3 v1.1 Task 3 Sprint 7
 *
 * Discovery Focus selection card for Screen 1.
 * Rendered in a 2-column grid: 6 standard cards plus one full-width
 * Enterprise-Wide card (wide=true, col-span-2).
 *
 * Visual states:
 *   default  - border-border, bg-panel, muted icon, text-text title, muted subtext
 *   hover    - border-emerald-500/40 (no background change)
 *   selected - border-emerald-500, bg-emerald-500/[0.08], emerald icon, emerald title, emerald subtext/80
 *
 * Interaction:
 *   focus    - focus:ring-2 focus:ring-emerald-500/50
 *   keyboard - Enter and Space activate selection
 *
 * Layout variants:
 *   standard - icon above title and subtext (column)
 *   wide     - icon left of title and subtext (row), card spans full grid width (col-span-2)
 *
 * Token note:
 *   Selected state uses emerald-500 (teal family) throughout. The accent token
 *   (#0D55D7, blue) is correct for primary buttons and links in the app shell
 *   but is not the selection color used in the stack builder. All selected
 *   states across the stack builder use the emerald/teal family.
 *
 * Border radius:
 *   rounded-lg - consistent with SystemCard and DiscoveryConfidenceBar.
 *   This is the standard card radius for the stack builder.
 *
 * tabIndex prop:
 *   Defaults to 0. Pass tabIndex={-1} for non-active cards when implementing
 *   roving tabindex with arrow-key navigation at the parent (Screen 1, Sprint 8).
 *   Sprint 8 story: improve FocusCard group keyboard behavior with roving
 *   tabindex and arrow navigation.
 *
 * Accessibility:
 *   role="radio" - parent must have role="radiogroup" aria-label="Discovery focus".
 *   aria-checked reflects selection state.
 *   tabIndex prop - see above.
 *   Enter and Space toggle selection.
 *   Icon is aria-hidden.
 *
 * Props:
 *   card      - FocusCard type: { id, title, subtext, icon, wide? }
 *   selected  - whether this card is the currently selected focus
 *   onSelect  - called with FocusId when user selects the card
 *   tabIndex  - optional, defaults to 0. Pass -1 for roving tabindex pattern.
 *
 * Usage:
 *   const { state, setFocus } = useSetupState();
 *   <div role="radiogroup" aria-label="Discovery focus" className="grid grid-cols-2 gap-3">
 *     {FOCUS_CARDS.map(card => (
 *       <FocusCard
 *         key={card.id}
 *         card={card}
 *         selected={state.focusId === card.id}
 *         onSelect={setFocus}
 *       />
 *     ))}
 *   </div>
 */

import React from 'react';
import {
  GitBranch,
  Globe2,
  ListChecks,
  Settings,
  ShieldCheck,
  Shuffle,
  Users,
} from 'lucide-react';
import { FocusCard as FocusCardType, FocusId } from '../../types/stack_builder';

interface Props {
  card: FocusCardType;
  selected: boolean;
  onSelect: (id: FocusId) => void;
  /** Defaults to 0. Pass -1 for non-active cards in the roving tabindex pattern. */
  tabIndex?: number;
}

const FOCUS_ICONS: Record<FocusId, React.ElementType> = {
  member_customer_service: Users,
  core_operations: Settings,
  approvals_compliance: ShieldCheck,
  cross_system_handoffs: Shuffle,
  back_office_productivity: ListChecks,
  engineering_change: GitBranch,
  enterprise_wide: Globe2,
};

export default function FocusCard({ card, selected, onSelect, tabIndex = 0 }: Props) {
  const Icon = FOCUS_ICONS[card.id] ?? Settings;

  return (
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      tabIndex={tabIndex}
      onClick={() => onSelect(card.id)}
      className={[
        'w-full cursor-pointer rounded-lg border p-4 text-left transition-colors duration-150',
        'focus:outline-none focus:ring-2 focus:ring-accent/50',
        card.wide ? 'md:col-span-2' : '',
        selected
          ? 'border-accent bg-accent/10 shadow-sm shadow-black/10'
          : 'border-border bg-panel hover:border-accent/50 hover:bg-panel2',
      ].filter(Boolean).join(' ')}
    >
      {card.wide ? (
        <div className="flex items-start gap-4">
          <Icon size={20} strokeWidth={2.2} className={`mt-0.5 flex-shrink-0 ${selected ? 'text-accent' : 'text-muted'}`} aria-hidden="true" />
          <div>
            <div className={`text-sm font-medium mb-1 ${
              selected ? 'text-text' : 'text-text'
            }`}>
              {card.title}
            </div>
            <div className={`text-xs leading-relaxed ${
              selected ? 'text-blue-100' : 'text-muted'
            }`}>
              {card.subtext}
            </div>
          </div>
        </div>
      ) : (
        <div>
          <Icon size={20} strokeWidth={2.2} className={`mb-2 flex-shrink-0 ${selected ? 'text-accent' : 'text-muted'}`} aria-hidden="true" />
          <div className={`text-sm font-medium mb-1 ${
            selected ? 'text-text' : 'text-text'
          }`}>
            {card.title}
          </div>
          <div className={`text-xs leading-relaxed ${
            selected ? 'text-blue-100' : 'text-muted'
          }`}>
            {card.subtext}
          </div>
        </div>
      )}
    </button>
  );
}
