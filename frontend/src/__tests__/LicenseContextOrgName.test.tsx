/**
 * R17-D4 Addendum A §2 / T13 (AT-508) — LicenseContext org-name source tests.
 *
 * The shared provider is the "one name, resolved once" (§5): it fetches T12's
 * `GET /api/license/org-name` once and exposes it via `useOrgName()`, and
 * `refresh()` re-reads it so pasting a new key updates every surface with no
 * restart (AC15). Before a key / on error it shows the neutral default (AC16).
 *
 * Run: npx vitest run src/__tests__/LicenseContextOrgName.test.tsx
 */
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";

const h = vi.hoisted(() => ({
  mockBanner: vi.fn(),
  mockOrgName: vi.fn(),
}));

vi.mock("../api/licenseApi", () => ({
  fetchLicenseBanner: (...a: unknown[]) => h.mockBanner(...a),
  fetchLicenseOrgName: (...a: unknown[]) => h.mockOrgName(...a),
}));

import { LicenseProvider, useLicense, useOrgName } from "../context/LicenseContext";

function Consumer() {
  const orgName = useOrgName();
  const { refresh } = useLicense();
  return (
    <div>
      <span data-testid="name">{orgName}</span>
      <button type="button" onClick={() => void refresh()}>
        refresh
      </button>
    </div>
  );
}

function renderConsumer() {
  return render(
    <LicenseProvider>
      <Consumer />
    </LicenseProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  h.mockBanner.mockResolvedValue({ status: "valid", expires_at: "2027-06-18" });
  h.mockOrgName.mockResolvedValue({ orgName: "Teachers Credit Union" });
});

describe("LicenseContext org name (T13)", () => {
  it("resolves the shared org name from the org-name endpoint on mount", async () => {
    renderConsumer();
    expect(await screen.findByText("Teachers Credit Union")).toBeInTheDocument();
    // One shared fetch — not per-surface.
    expect(h.mockOrgName).toHaveBeenCalledTimes(1);
  });

  it("updates the name on refresh after a new key is pasted, no restart (AC15)", async () => {
    h.mockOrgName
      .mockResolvedValueOnce({ orgName: "Teachers Credit Union" })
      .mockResolvedValue({ orgName: "City National Bank" });

    renderConsumer();
    expect(await screen.findByText("Teachers Credit Union")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "refresh" }));

    await waitFor(() =>
      expect(screen.getByTestId("name")).toHaveTextContent("City National Bank"),
    );
  });

  it("shows the neutral default when the org name is unavailable (AC16)", async () => {
    h.mockOrgName.mockRejectedValue(new Error("network"));
    renderConsumer();
    expect(await screen.findByText("Your Organisation")).toBeInTheDocument();
  });

  it("keeps the org name even if the banner read fails (independent reads)", async () => {
    h.mockBanner.mockRejectedValue(new Error("banner down"));
    renderConsumer();
    expect(await screen.findByText("Teachers Credit Union")).toBeInTheDocument();
  });
});
