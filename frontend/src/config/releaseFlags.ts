// Temporary demo gate for Release 2.0 Arc A UI (A1/A2/A3).
// Set VITE_SHOW_RELEASE_2_ARC_A_UI=true to show these surfaces again.
export const showRelease2ArcAUi =
  ((import.meta.env.VITE_SHOW_RELEASE_2_ARC_A_UI as string | undefined) ?? "")
    .trim()
    .toLowerCase() === "true";

// R191-R1 T5 (AT-726) "Coming soon" roadmap labelling — withdrawn from the UI.
// The anchor-on-shipped rule itself is NOT reverted: the backend overlay
// (app/connector_roadmap.py) still classifies every catalog tile, still refuses a
// roadmap connect with a named reason, and the Stack Builder registry still keeps
// unshipped systems out of `system_defaults` — so nothing became dishonestly
// connectable. Only the customer-facing "Coming soon" copy is hidden.
//
// This is safe to hide because it is not the gate: every roadmap connector is
// outside ConnectorTile's ENABLED_CONNECTOR_IDS, so those tiles stay
// non-connectable on the pre-existing product gate ("Connecting new sources is
// currently unavailable"), and no roadmap tile is `multiScope`.
//
// Set VITE_SHOW_ROADMAP_COMING_SOON=true to bring the labelling back.
export const showRoadmapComingSoonLabels =
  ((import.meta.env.VITE_SHOW_ROADMAP_COMING_SOON as string | undefined) ?? "")
    .trim()
    .toLowerCase() === "true";
