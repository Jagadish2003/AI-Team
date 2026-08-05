// Temporary demo gate for Release 2.0 Arc A UI (A1/A2/A3).
// Set VITE_SHOW_RELEASE_2_ARC_A_UI=true to show these surfaces again.
export const showRelease2ArcAUi =
  ((import.meta.env.VITE_SHOW_RELEASE_2_ARC_A_UI as string | undefined) ?? "")
    .trim()
    .toLowerCase() === "true";
