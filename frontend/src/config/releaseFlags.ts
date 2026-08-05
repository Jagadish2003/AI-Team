// Temporary demo gate for Release 2.0 Arc A UI (A1/A2/A3).
// Default is visible again; set VITE_SHOW_RELEASE_2_ARC_A_UI=false to hide.
const release2ArcAUiFlag =
  (import.meta.env.VITE_SHOW_RELEASE_2_ARC_A_UI as string | undefined)
    ?.trim()
    .toLowerCase() ?? "";

export const showRelease2ArcAUi = release2ArcAUiFlag !== "false";
