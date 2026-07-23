/**
 * OnboardingSlide — presentational layout for a single tour step.
 *
 * Illustration, heading, supporting copy, and optional platform chips. The
 * content fades/lifts in on each step change via `.onboarding-anim-content`
 * (keyed by the parent so it re-triggers per slide).
 */
import React from "react";

import type { SlideDef } from "./slides";

export default function OnboardingSlide({
  slide,
  name,
}: {
  slide: SlideDef;
  name: string | null;
}) {
  return (
    <div className="onboarding-anim-content flex flex-col items-center text-center">
      <div className="mb-9">{slide.illustration}</div>

      <h2
        id="onboarding-title"
        className="text-balance text-xl font-semibold tracking-tight text-text sm:text-2xl"
      >
        {slide.title(name)}
      </h2>

      <p className="mt-4 max-w-md text-pretty text-sm leading-7 text-muted sm:text-[15px]">
        {slide.description}
      </p>

      {slide.chips && slide.chips.length > 0 && (
        <ul className="mt-7 flex flex-wrap items-center justify-center gap-2.5">
          {slide.chips.map((chip) => (
            <li
              key={chip}
              className="rounded-full border border-border bg-panel/60 px-3 py-1 text-xs font-medium text-text/90"
            >
              {chip}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
