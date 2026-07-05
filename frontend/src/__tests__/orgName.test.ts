/**
 * R17-D4 Addendum A §2 / T13 (AT-508) — org-name resolver tests.
 *
 * The single client-side resolver turns the license-resolved `org_name` (null
 * before any key is installed) into a display string, substituting a neutral
 * default so no stale/placeholder naming ever shows (AC16).
 *
 * Run: npx vitest run src/__tests__/orgName.test.ts
 */
import { describe, it, expect } from "vitest";
import { resolveOrgName, NEUTRAL_ORG_NAME } from "../utils/orgName";

describe("resolveOrgName", () => {
  it("returns the license-resolved org name when present", () => {
    expect(resolveOrgName("Teachers Credit Union")).toBe("Teachers Credit Union");
  });

  it("trims surrounding whitespace", () => {
    expect(resolveOrgName("  Teachers Credit Union  ")).toBe("Teachers Credit Union");
  });

  it("falls back to the neutral default for null (no key installed) — AC16", () => {
    expect(resolveOrgName(null)).toBe(NEUTRAL_ORG_NAME);
  });

  it("falls back to the neutral default for undefined (loading)", () => {
    expect(resolveOrgName(undefined)).toBe(NEUTRAL_ORG_NAME);
  });

  it("falls back to the neutral default for an empty/whitespace string", () => {
    expect(resolveOrgName("")).toBe(NEUTRAL_ORG_NAME);
    expect(resolveOrgName("   ")).toBe(NEUTRAL_ORG_NAME);
  });

  it("never returns an empty string", () => {
    for (const v of [null, undefined, "", "  "]) {
      expect(resolveOrgName(v).length).toBeGreaterThan(0);
    }
  });
});
