/**
 * LicenseBanner (LIC-1 / T9 / AT-350) tests.
 *
 * Covers:
 *   - the grace message renders in grace state
 *   - the read-only message renders past grace (readonly)
 *   - the banner is absent when the license is valid
 *   - the banner is absent when the status source is unavailable (e.g. non-Owner 403)
 *
 * The status source (api/licenseApi.fetchLicenseStatus) is mocked — no backend
 * is touched. The banner is rendered inside the real LicenseProvider so the
 * shared-fetch flow is exercised end to end.
 *
 * Run: npx vitest run src/__tests__/LicenseBanner.test.tsx
 */
import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";
import type { LicenseStatusResponse } from "../types/license";

const h = vi.hoisted(() => ({ mockFetch: vi.fn() }));

vi.mock("../api/licenseApi", () => ({
  fetchLicenseStatus: (...a: unknown[]) => h.mockFetch(...a),
}));

import { LicenseProvider } from "../context/LicenseContext";
import LicenseBanner from "../components/common/LicenseBanner";

function renderBanner() {
  return render(
    <LicenseProvider>
      <LicenseBanner />
    </LicenseProvider>,
  );
}

const make = (over: Partial<LicenseStatusResponse>): LicenseStatusResponse => ({
  status: "valid",
  customer: "City National Bank",
  term: 12,
  expires_at: "2026-06-10",
  days_remaining: 5,
  ...over,
});

describe("LicenseBanner (LIC-1 / T9)", () => {
  beforeEach(() => h.mockFetch.mockReset());

  it("renders the grace message in grace state", async () => {
    h.mockFetch.mockResolvedValue(make({ status: "grace", expires_at: "2026-06-10" }));
    renderBanner();
    expect(
      await screen.findByText(
        "Your AgentIQ license expired on 2026-06-10. Contact CloudFulcrum to renew.",
      ),
    ).toBeInTheDocument();
  });

  it("renders the read-only message past grace", async () => {
    h.mockFetch.mockResolvedValue(make({ status: "readonly" }));
    renderBanner();
    expect(
      await screen.findByText("License expired. Renew to resume discovery runs."),
    ).toBeInTheDocument();
  });

  it("renders nothing when the license is valid", async () => {
    h.mockFetch.mockResolvedValue(make({ status: "valid" }));
    renderBanner();
    await waitFor(() => expect(h.mockFetch).toHaveBeenCalled());
    expect(screen.queryByTestId("license-banner")).toBeNull();
  });

  it("renders nothing when no usable status is available", async () => {
    // The provider stores null when status can't be read (e.g. a non-Owner 403
    // is caught) — the banner must render nothing in that case.
    h.mockFetch.mockResolvedValue(null as unknown as LicenseStatusResponse);
    renderBanner();
    await waitFor(() => expect(h.mockFetch).toHaveBeenCalled());
    expect(screen.queryByTestId("license-banner")).toBeNull();
  });
});
