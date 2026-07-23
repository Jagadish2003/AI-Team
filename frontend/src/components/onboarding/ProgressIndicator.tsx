/**
 * ProgressIndicator — animated step dots (● ○ ○ ○).
 *
 * The active step renders as an elongated accent pill; visited/upcoming steps are
 * muted dots. Width/colour transition smoothly (respecting reduced-motion via the
 * global transition rules). Presentational only — navigation is owned by the modal.
 */
import React from "react";

export default function ProgressIndicator({
  total,
  current,
  onSelect,
}: {
  total: number;
  current: number;
  /** Optional: jump to a step (used for accessible dot navigation). */
  onSelect?: (index: number) => void;
}) {
  return (
    <div
      className="flex items-center justify-center gap-2"
      role="tablist"
      aria-label="Onboarding progress"
    >
      {Array.from({ length: total }).map((_, i) => {
        const active = i === current;
        return (
          <button
            key={i}
            type="button"
            role="tab"
            aria-selected={active}
            aria-label={`Step ${i + 1} of ${total}`}
            tabIndex={onSelect ? 0 : -1}
            onClick={onSelect ? () => onSelect(i) : undefined}
            className={`h-2 rounded-full transition-all duration-300 ease-out focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 ${
              active
                ? "w-6 bg-accent"
                : "w-2 bg-muted/40 hover:bg-muted/70"
            } ${onSelect ? "cursor-pointer" : "cursor-default"}`}
          />
        );
      })}
    </div>
  );
}
