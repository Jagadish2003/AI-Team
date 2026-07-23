/**
 * R17-D4 Addendum A / T11 (AT-506) — LicenseLimitBanner.
 *
 * The Integration-Hub usage strip that shows systems-used vs systems-licensed
 * (AC14) and the "contact CloudFulcrum" notice once the org is at its limit
 * (AC10). Values come from T10's GET /api/license/limits, so the count shown
 * here is the count the connect-time gate enforces.
 *
 * The count wording also depends on the live license STATUS so a never-licensed
 * install (no cap because there is no license) is not mislabelled as having an
 * "unlimited license" — useLicense is mocked here to drive that.
 *
 * Run:
 *   npx vitest run src/__tests__/LicenseLimitBanner.test.tsx
 */
import React from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";

// Control the license status the banner reads (valid / grace / readonly / invalid,
// or null for the loading/unknown window). Hoisted so the vi.mock factory can see it.
const licenseMock = vi.hoisted(() => ({ status: null as string | null }));
vi.mock("../context/LicenseContext", () => ({
  useLicense: () => ({ status: licenseMock.status ? { status: licenseMock.status } : null }),
}));

import LicenseLimitBanner from "../components/integrations/LicenseLimitBanner";
import type { LicenseLimitsResponse } from "../types/license";

function limits(over: Partial<LicenseLimitsResponse>): LicenseLimitsResponse {
  return { systemsUsed: 0, systemsLicensed: null, unlimited: true, canConnectMore: true, ...over };
}

describe("LicenseLimitBanner (AT-506 / AC14)", () => {
  beforeEach(() => {
    licenseMock.status = null;
  });

  it("shows systems-used vs systems-licensed under the limit, with no at-limit notice", () => {
    licenseMock.status = "valid";
    render(
      <LicenseLimitBanner
        limits={limits({ systemsUsed: 2, systemsLicensed: 6, unlimited: false, canConnectMore: true })}
      />,
    );
    expect(screen.getByTestId("license-usage-count").textContent).toBe("2 of 6");
    expect(screen.queryByTestId("license-at-limit")).toBeNull();
  });

  it("shows the exact contact-CloudFulcrum notice at the limit (AC10)", () => {
    licenseMock.status = "valid";
    render(
      <LicenseLimitBanner
        limits={limits({ systemsUsed: 3, systemsLicensed: 3, unlimited: false, canConnectMore: false })}
      />,
    );
    expect(screen.getByTestId("license-usage-count").textContent).toBe("3 of 3");
    expect(screen.getByTestId("license-at-limit").textContent).toBe(
      "Your license covers 3 systems. Contact CloudFulcrum to add more.",
    );
  });

  it("shows licensing-specific wording at the unlicensed cap (T5 / AC4)", () => {
    licenseMock.status = "readonly"; // no active license → unlicensed cap, not a licensed limit
    render(
      <LicenseLimitBanner
        limits={limits({ systemsUsed: 2, systemsLicensed: 2, unlimited: false, canConnectMore: false })}
      />,
    );
    expect(screen.getByTestId("license-usage-count").textContent).toBe("2 of 2");
    // Must name the MISSING license, not claim "your license covers 2".
    expect(screen.getByTestId("license-at-limit").textContent).toBe(
      "No license is installed. Unlicensed installations can connect up to 2 systems. Install a license from CloudFulcrum to connect more.",
    );
  });

  it("labels a genuine unlimited license (valid status) as such (AC13)", () => {
    licenseMock.status = "valid";
    render(
      <LicenseLimitBanner
        limits={limits({ systemsUsed: 4, systemsLicensed: null, unlimited: true, canConnectMore: true })}
      />,
    );
    expect(screen.getByTestId("license-usage-count").textContent).toBe("4 (unlimited license)");
    expect(screen.queryByTestId("license-at-limit")).toBeNull();
  });

  it("does NOT claim 'unlimited license' when there is no active license", () => {
    licenseMock.status = "invalid"; // never-licensed / unusable key
    render(
      <LicenseLimitBanner
        limits={limits({ systemsUsed: 0, systemsLicensed: null, unlimited: true, canConnectMore: true })}
      />,
    );
    const text = screen.getByTestId("license-usage-count").textContent ?? "";
    expect(text).not.toContain("unlimited license");
    expect(text).toBe("0 · no active license");
  });

  it("makes no license claim while status is still unknown (loading/error)", () => {
    licenseMock.status = null; // status not yet resolved
    render(
      <LicenseLimitBanner
        limits={limits({ systemsUsed: 5, systemsLicensed: null, unlimited: true, canConnectMore: true })}
      />,
    );
    const text = screen.getByTestId("license-usage-count").textContent ?? "";
    expect(text).toBe("5");
    expect(text).not.toContain("unlimited license");
  });

  it("renders nothing when limits are unknown (fetch failed / not yet loaded)", () => {
    const { container } = render(<LicenseLimitBanner limits={null} />);
    expect(container.firstChild).toBeNull();
  });
});
