/**
 * Onboarding slide content + lightweight lucide-based illustrations.
 *
 * Illustrations are composed from the app's existing icon library (lucide-react)
 * inside a themed "glass tile" — no stock photos, no extra dependencies. Each
 * illustration inherits theme tokens (accent / border / muted) so it adapts to
 * light and dark automatically.
 */
import React from "react";
import {
  Sparkles,
  Cable,
  Waypoints,
  LayoutDashboard,
  ServerCog,
  Bug,
  ShieldAlert,
  Activity,
  type LucideIcon,
} from "lucide-react";

export interface SlideDef {
  /** Stable id for keys / test ids. */
  id: string;
  /** Rendered heading. `name` is interpolated for the welcome slide. */
  title: (name: string | null) => string;
  /** Short supporting copy. */
  description: string;
  /** Optional chips (e.g. connectable platforms) shown under the copy. */
  chips?: string[];
  /** Illustration node. */
  illustration: React.ReactNode;
}

/** A framed illustration tile with a primary icon and optional orbiting icons. */
function IllustrationTile({
  icon: Icon,
  satellites = [],
}: {
  icon: LucideIcon;
  satellites?: LucideIcon[];
}) {
  return (
    <div
      aria-hidden="true"
      className="relative mx-auto flex h-28 w-28 items-center justify-center rounded-2xl border border-accent/25 bg-accent/10 text-accent shadow-inner sm:h-32 sm:w-32"
    >
      <Icon className="h-12 w-12 sm:h-14 sm:w-14" strokeWidth={1.5} />
      {satellites.map((Sat, i) => {
        // Distribute satellites around the top arc of the tile.
        const positions = [
          "-left-3 -top-3",
          "-right-3 -top-3",
          "-left-3 -bottom-3",
          "-right-3 -bottom-3",
        ];
        return (
          <span
            key={i}
            className={`absolute ${positions[i] ?? positions[0]} flex h-9 w-9 items-center justify-center rounded-xl border border-border bg-panel text-muted shadow-sm`}
          >
            <Sat className="h-4 w-4" strokeWidth={1.75} />
          </span>
        );
      })}
    </div>
  );
}

export const SLIDES: SlideDef[] = [
  {
    id: "welcome",
    title: (name) => `Welcome, ${name ?? "there"} 👋`,
    description:
      "Welcome to AgentIQ. We're excited to help you monitor, correlate and understand your enterprise operations using AI-powered intelligence.",
    illustration: <IllustrationTile icon={Sparkles} />,
  },
  {
    id: "connect",
    title: () => "Connect your systems",
    description:
      "Connect enterprise platforms to begin ingesting operational signals from across your stack.",
    chips: ["ServiceNow", "Jira", "Azure", "AWS"],
    illustration: (
      <IllustrationTile icon={Cable} satellites={[ServerCog, Waypoints]} />
    ),
  },
  {
    id: "correlate",
    title: () => "AI correlation",
    description:
      "AgentIQ automatically correlates incidents, vulnerabilities, operational events and enterprise signals to surface meaningful insights.",
    illustration: (
      <IllustrationTile icon={Waypoints} satellites={[Bug, ShieldAlert]} />
    ),
  },
  {
    id: "insights",
    title: () => "Insights & decisions",
    description:
      "Visualize operational health, investigate relationships, and receive AI-assisted recommendations through a unified dashboard.",
    illustration: (
      <IllustrationTile icon={LayoutDashboard} satellites={[Activity]} />
    ),
  },
];
