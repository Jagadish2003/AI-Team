import React from "react";
import WorkspaceMembersPanel from "../components/settings/WorkspaceMembersPanel";

/**
 * CS-1 T3 — Settings page shell.
 *
 * Deliberately minimal for the POC: a width-constrained container, a single
 * heading, and the WorkspaceMembersPanel. Future settings sections (Account,
 * Notifications, Billing) will be added to this shell in later sprints.
 *
 * This is a pure layout shell — it holds no state, effects, or data fetching.
 * All member-management logic lives in WorkspaceMembersPanel (T4).
 */
export default function SettingsPage() {
  return (
    <div className="max-w-4xl mx-auto px-6 py-8">
      <h1>Settings</h1>
      <WorkspaceMembersPanel />
    </div>
  );
}
