/**
 * LicenseBanner (LIC-1 / T9 / AT-350) tests.
 *
 * Covers:
 *   - the grace message renders in grace state
 *   - the read-only message renders past grace (readonly)
 *   - the banner is absent when the license is valid
 *   - the banner is absent when the status source is unavailable (network error)
 *
 * The status source (api/licenseApi.fetchLicenseBanner) is mocked — no backend
 * is touched. The banner reads the auth-only banner endpoint so it renders for
 * every role (AC4/AC5). The banner is rendered inside the real LicenseProvider
 * so the shared-fetch flow is exercised end to end.
 *
 * Run: npx vitest run src/__tests__/LicenseBanner.test.tsx
 */
import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";
import type { LicenseBannerResponse } from "../types/license";

const h = vi.hoisted(() => ({ mockFetch: vi.fn(), mockOrgName: vi.fn() }));

vi.mock("../api/licenseApi", () => ({
  fetchLicenseBanner: (...a: unknown[]) => h.mockFetch(...a),
  // T13: the shared LicenseProvider now also reads the org name on refresh.
  fetchLicenseOrgName: (...a: unknown[]) => h.mockOrgName(...a),
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

const make = (over: Partial<LicenseBannerResponse>): LicenseBannerResponse => ({
  status: "valid",
  expires_at: "2026-06-10",
  ...over,
});

describe("LicenseBanner (LIC-1 / T9)", () => {
  beforeEach(() => {
    h.mockFetch.mockReset();
    h.mockOrgName.mockReset();
    h.mockOrgName.mockResolvedValue({ orgName: "Your Organisation" });
  });

  it("renders the grace message with a runs-blocked countdown", async () => {
    h.mockFetch.mockResolvedValue(
      make({ status: "grace", expires_at: "2026-06-10", grace_days_remaining: 9 }),
    );
    renderBanner();
    expect(
      await screen.findByText(
        "Your AgentIQ license expired on June 10, 2026. Discovery runs will be blocked in 9 days — contact CloudFulcrum to renew.",
      ),
    ).toBeInTheDocument();
  });

  it("renders the grace message without a countdown when N is unavailable", async () => {
    h.mockFetch.mockResolvedValue(make({ status: "grace", expires_at: "2026-06-10" }));
    renderBanner();
    expect(
      await screen.findByText(
        "Your AgentIQ license expired on June 10, 2026. Discovery runs still work during the grace period — contact CloudFulcrum to renew.",
      ),
    ).toBeInTheDocument();
  });

  it("renders the read-only 'expired' message past grace (expired term)", async () => {
    h.mockFetch.mockResolvedValue(make({ status: "readonly", reason: null }));
    renderBanner();
    expect(
      await screen.findByText("License expired. Renew to resume discovery runs."),
    ).toBeInTheDocument();
  });

  it("renders 'No valid license installed' for a fresh no-key install (AC6)", async () => {
    h.mockFetch.mockResolvedValue(make({ status: "readonly", reason: "no_license", expires_at: null }));
    renderBanner();
    expect(
      await screen.findByText("No valid license installed. Paste a valid license key to activate AgentIQ."),
    ).toBeInTheDocument();
  });

  it("renders 'No valid license installed' for an invalid/tampered key (AC2)", async () => {
    h.mockFetch.mockResolvedValue(make({ status: "invalid", reason: "signature_or_format", expires_at: null }));
    renderBanner();
    expect(
      await screen.findByText("No valid license installed. Paste a valid license key to activate AgentIQ."),
    ).toBeInTheDocument();
  });

  it("renders the org-mismatch message for a wrong-org key (R-1.9.1-L1 / T2, AC1)", async () => {
    h.mockFetch.mockResolvedValue(make({ status: "invalid", reason: "org_mismatch", expires_at: null }));
    renderBanner();
    const banner = await screen.findByTestId("license-banner");
    expect(banner).toHaveAttribute("data-reason", "org_mismatch");
    expect(banner.textContent).toMatch(/issued to a different organisation/i);
  });

  it("renders a clock-inconsistency message when the clock guard trips (AC8)", async () => {
    h.mockFetch.mockResolvedValue(make({ status: "readonly", reason: "clock_rollback", expires_at: null }));
    renderBanner();
    expect(
      await screen.findByText(/system clock looks inconsistent/i),
    ).toBeInTheDocument();
  });

  it("renders nothing when the license is valid", async () => {
    h.mockFetch.mockResolvedValue(make({ status: "valid" }));
    renderBanner();
    await waitFor(() => expect(h.mockFetch).toHaveBeenCalled());
    expect(screen.queryByTestId("license-banner")).toBeNull();
  });

  it("renders nothing when no usable status is available", async () => {
    // The provider stores null when status can't be read (e.g. a transient
    // network error is caught) — the banner must render nothing in that case.
    h.mockFetch.mockResolvedValue(null as unknown as LicenseBannerResponse);
    renderBanner();
    await waitFor(() => expect(h.mockFetch).toHaveBeenCalled());
    expect(screen.queryByTestId("license-banner")).toBeNull();
  });
});
