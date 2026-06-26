/**
 * FocusCard — SB-3 Sprint 7
 *
 * Discovery Focus selection card for Screen 1.
 * Rendered in a 2-column grid: 6 standard cards plus one full-width
 * Enterprise-Wide card (wide=true, col-span-2).
 *
 * Visual states:
 *   default  - border-border, bg-panel, text-text title, muted subtext
 *   hover    - blue accent border with a very light blue background
 *   selected - blue accent border, blue-tinted background, accent title/subtext
 *
 * Interaction:
 *   focus    - focus:ring-2 focus:ring-accent/35
 *   keyboard - Enter and Space activate selection
 *
 * Layout variants:
 *   standard - title, subtext, and optional boundary copy.
 *   wide     - same top-aligned content, spanning the full grid width (col-span-2).
 *
 * Token note:
 *   Discovery-focus cards follow the same blue accent selection treatment as
 *   the industry/template pills in this panel.
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

import type React from 'react';
import { FocusCard as FocusCardType, FocusId } from '../../types/stack_builder';

interface Props {
  card: FocusCardType;
  selected: boolean;
  onSelect: (id: FocusId) => void;
  /** Defaults to 0. Pass -1 for non-active cards in the roving tabindex pattern. */
  tabIndex?: number;
}

export default function FocusCard({ card, selected, onSelect, tabIndex = 0 }: Props) {
  const titleClass = selected ? 'text-accent' : 'text-text';
  const subtextClass = selected ? 'text-accent' : 'text-muted';
  const boundaryLabelBase = 'shrink-0 font-medium';
  const hasBoundaryCopy = Boolean(card.useWhen || card.notWhen);

  function handleKeyDown(event: React.KeyboardEvent<HTMLButtonElement>) {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      onSelect(card.id);
    }
  }

  const boundaryCopy = hasBoundaryCopy ? (
    <div className="mt-3 space-y-2 border-t border-border/60 pt-3 text-xs leading-relaxed">
      {card.useWhen && (
        <p className={`grid grid-cols-[max-content_1fr] gap-x-1.5 ${subtextClass}`}>
          <span className={`${boundaryLabelBase} text-emerald-300`}>Use when:</span>
          <span>{card.useWhen}</span>
        </p>
      )}
      {card.notWhen && (
        <p className={`grid grid-cols-[max-content_1fr] gap-x-1.5 ${subtextClass}`}>
          <span className={`${boundaryLabelBase} text-red-400`}>Not when:</span>
          <span>{card.notWhen}</span>
        </p>
      )}
    </div>
  ) : null;

  return (
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      tabIndex={tabIndex}
      onClick={() => onSelect(card.id)}
      onKeyDown={handleKeyDown}
      className={[
        'flex h-full w-full cursor-pointer items-start justify-start rounded-lg border p-4 text-left transition-colors duration-150',
        'focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/35',
        card.wide ? 'col-span-2' : '',
        selected
          ? 'border-accent/60 bg-accent/15'
          : 'border-border bg-panel hover:border-accent/50 hover:bg-accent/5',
      ].filter(Boolean).join(' ')}
    >
      <div className="w-full">
        <div className={`mb-1 text-sm font-medium ${titleClass}`}>
          {card.title}
        </div>
        <div className={`text-xs leading-relaxed ${subtextClass}`}>
          {card.subtext}
        </div>
        {boundaryCopy}
      </div>
    </button>
  );
}
