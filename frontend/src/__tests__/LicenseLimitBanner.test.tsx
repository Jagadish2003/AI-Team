/**
 * R17-D4 Addendum A / T11 (AT-506) — LicenseLimitBanner.
 *
 * The Integration-Hub usage strip that shows systems-used vs systems-licensed
 * (AC14) and the "contact CloudFulcrum" notice once the org is at its limit
 * (AC10). Values come from T10's GET /api/license/limits, so the count shown
 * here is the count the connect-time gate enforces.
 *
 * Run:
 *   npx vitest run src/__tests__/LicenseLimitBanner.test.tsx
 */
import React from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import LicenseLimitBanner from "../components/integrations/LicenseLimitBanner";
import type { LicenseLimitsResponse } from "../types/license";

function limits(over: Partial<LicenseLimitsResponse>): LicenseLimitsResponse {
  return { systemsUsed: 0, systemsLicensed: null, unlimited: true, canConnectMore: true, ...over };
}

describe("LicenseLimitBanner (AT-506 / AC14)", () => {
  it("shows systems-used vs systems-licensed under the limit, with no at-limit notice", () => {
    render(
      <LicenseLimitBanner
        limits={limits({ systemsUsed: 2, systemsLicensed: 6, unlimited: false, canConnectMore: true })}
      />,
    );
    expect(screen.getByTestId("license-usage-count").textContent).toBe("2 of 6");
    expect(screen.queryByTestId("license-at-limit")).toBeNull();
  });

  it("shows the exact contact-CloudFulcrum notice at the limit (AC10)", () => {
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

  it("shows the approaching-capacity notice when within the configured margin (MSP-B13 AC2)", () => {
    render(
      <LicenseLimitBanner
        limits={limits({
          systemsUsed: 1,
          systemsLicensed: 2,
          unlimited: false,
          canConnectMore: true,
          approachingCap: true,
          atCap: false,
          notice: "You are approaching your licence limit: 1 of 2 systems connected (1 system remaining). Contact CloudFulcrum to add more.",
        })}
      />,
    );
    expect(screen.getByTestId("license-approaching-limit").textContent).toContain(
      "approaching your licence limit",
    );
    // Not the hard-stop notice — that renders only at the cap.
    expect(screen.queryByTestId("license-at-limit")).toBeNull();
  });

  it("shows an unlimited license without a cap or a notice (AC13)", () => {
    render(
      <LicenseLimitBanner
        limits={limits({ systemsUsed: 4, systemsLicensed: null, unlimited: true, canConnectMore: true })}
      />,
    );
    expect(screen.getByTestId("license-usage-count").textContent).toBe("4 (unlimited license)");
    expect(screen.queryByTestId("license-at-limit")).toBeNull();
  });

  it("renders nothing when limits are unknown (fetch failed / not yet loaded)", () => {
    const { container } = render(<LicenseLimitBanner limits={null} />);
    expect(container.firstChild).toBeNull();
  });
});
