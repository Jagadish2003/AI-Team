import React from "react";

/**
 * Layout-shaped loading placeholders.
 *
 * Prefer these over a single centered spinner (LoadingPanel) for a page's main
 * content: a skeleton reserves the FINAL dimensions of what is loading, so when
 * the real content arrives it fills the same box instead of snapping in and
 * pushing the layout around (no cumulative layout shift, less "pop-in").
 *
 * `Skeleton` is a single shimmer block; size/position it with Tailwind classes
 * (e.g. `h-9 w-64`, `h-[560px] w-full`). The page composes several blocks into a
 * shape that mirrors the real layout it replaces.
 */
export function Skeleton({ className = "" }: { className?: string }) {
  return (
    <div
      aria-hidden="true"
      className={`animate-pulse rounded-md bg-border/60 ${className}`}
    />
  );
}

/** A stat-card-shaped placeholder (matches the StatCard used across the app). */
export function SkeletonStatCard() {
  return (
    <div className="rounded-xl border border-border bg-panel p-4">
      <Skeleton className="h-3 w-32" />
      <Skeleton className="mt-3 h-8 w-16" />
      <Skeleton className="mt-3 h-3 w-40" />
    </div>
  );
}

/**
 * A row of `count` stat-card placeholders in the app's standard 3-col grid.
 * Wrap page skeletons in an element with `aria-busy` so assistive tech announces
 * the loading state.
 */
export function SkeletonStatCards({ count = 3 }: { count?: number }) {
  return (
    <div className="grid grid-cols-3 gap-4">
      {Array.from({ length: count }).map((_, i) => (
        <SkeletonStatCard key={i} />
      ))}
    </div>
  );
}
