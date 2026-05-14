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
import { FocusCard as FocusCardType, FocusId } from '../../types/stack_builder';

interface Props {
  card: FocusCardType;
  selected: boolean;
  onSelect: (id: FocusId) => void;
  /** Defaults to 0. Pass -1 for non-active cards in the roving tabindex pattern. */
  tabIndex?: number;
}

export default function FocusCard({ card, selected, onSelect, tabIndex = 0 }: Props) {
  const handleKey = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onSelect(card.id);
    }
  };

  return (
    <div
      role="radio"
      aria-checked={selected}
      tabIndex={tabIndex}
      onClick={() => onSelect(card.id)}
      onKeyDown={handleKey}
      className={[
        'cursor-pointer rounded-lg border p-4 transition-colors duration-150',
        'focus:outline-none focus:ring-2 focus:ring-emerald-500/50',
        card.wide ? 'col-span-2' : '',
        selected
          ? 'border-emerald-500 bg-emerald-500/[0.08]'
          : 'border-border bg-panel hover:border-emerald-500/40',
      ].filter(Boolean).join(' ')}
    >
      {card.wide ? (
        <div className="flex items-start gap-4">
          <i
            className={`ti ${card.icon} text-xl flex-shrink-0 mt-0.5 ${
              selected ? 'text-emerald-500' : 'text-muted'
            }`}
            aria-hidden="true"
          />
          <div>
            <div className={`text-sm font-medium mb-1 ${
              selected ? 'text-emerald-500' : 'text-text'
            }`}>
              {card.title}
            </div>
            <div className={`text-xs leading-relaxed ${
              selected ? 'text-emerald-500/80' : 'text-muted'
            }`}>
              {card.subtext}
            </div>
          </div>
        </div>
      ) : (
        <div>
          <i
            className={`ti ${card.icon} text-xl flex-shrink-0 block mb-2 ${
              selected ? 'text-emerald-500' : 'text-muted'
            }`}
            aria-hidden="true"
          />
          <div className={`text-sm font-medium mb-1 ${
            selected ? 'text-emerald-500' : 'text-text'
          }`}>
            {card.title}
          </div>
          <div className={`text-xs leading-relaxed ${
            selected ? 'text-emerald-500/80' : 'text-muted'
          }`}>
            {card.subtext}
          </div>
        </div>
      )}
    </div>
  );
}
