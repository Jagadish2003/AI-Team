/**
 * FocusCard — SB-1 Sprint 7
 *
 * Discovery Focus selection card. Used in 2-column grid on Screen 1.
 * The Enterprise-Wide card uses wide=true and spans full width (grid-col-span-2).
 *
 * States: default, hover, selected.
 * Selected: accent border, panel2 background, accent icon.
 *
 * Accessibility: role="radio" inside a radiogroup, keyboard navigable.
 */

import React from 'react';
import { FocusCard as FocusCardType } from '../../types/stack_builder';

interface Props {
  card: FocusCardType;
  selected: boolean;
  onSelect: (id: string) => void;
}

export default function FocusCard({ card, selected, onSelect }: Props) {
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
      tabIndex={0}
      onClick={() => onSelect(card.id)}
      onKeyDown={handleKey}
      className={[
        'cursor-pointer rounded-xl border p-4 transition-colors focus:outline-none focus:ring-2 focus:ring-accent/50',
        card.wide ? 'col-span-2' : '',
        selected
          ? 'border-accent bg-panel2'
          : 'border-border bg-panel hover:border-accent/50',
      ].join(' ')}
    >
      <div className={card.wide ? 'flex items-center gap-4' : ''}>
        <i
          className={`ti ${card.icon} text-xl flex-shrink-0 mb-2 ${card.wide ? 'mb-0' : ''} ${selected ? 'text-accent' : 'text-muted'}`}
          aria-hidden="true"
        />
        <div>
          <div className={`text-sm font-medium mb-1 ${selected ? 'text-text' : 'text-text'}`}>
            {card.title}
          </div>
          <div className="text-xs text-muted leading-relaxed">
            {card.subtext}
          </div>
        </div>
      </div>
    </div>
  );
}
