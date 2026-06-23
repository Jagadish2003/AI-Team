import { describe, expect, it } from "vitest";
import {
  clusterOverlappingBubbles,
  type OverlapBubble,
} from "../components/executive_report/SnapshotMatrix";

// Helper: normalise the partition (sort each cluster, then sort clusters by
// their first index) so assertions are independent of grouping/iteration order.
function normalise(clusters: number[][]): number[][] {
  return clusters
    .map((c) => [...c].sort((a, b) => a - b))
    .sort((a, b) => a[0] - b[0]);
}

const bubble = (bubbleX: number, bubbleCy: number, r: number): OverlapBubble => ({
  bubbleX,
  bubbleCy,
  r,
});

describe("clusterOverlappingBubbles (SnapshotMatrix union-find)", () => {
  it("returns no clusters for empty input", () => {
    expect(clusterOverlappingBubbles([])).toEqual([]);
  });

  it("puts a single bubble in its own cluster", () => {
    expect(clusterOverlappingBubbles([bubble(0, 0, 10)])).toEqual([[0]]);
  });

  it("keeps non-overlapping bubbles as separate singleton clusters", () => {
    // Centres 100 apart, radii 5 each → distance 100 > 10, no overlap.
    const clusters = clusterOverlappingBubbles([
      bubble(0, 0, 5),
      bubble(100, 0, 5),
    ]);
    expect(normalise(clusters)).toEqual([[0], [1]]);
  });

  it("groups two overlapping bubbles into one cluster", () => {
    // Centres 5 apart, radii 10 each → distance 5 < 20, overlap.
    const clusters = clusterOverlappingBubbles([
      bubble(0, 0, 10),
      bubble(5, 0, 10),
    ]);
    expect(normalise(clusters)).toEqual([[0, 1]]);
  });

  it("treats a transitive overlap chain (A–B, B–C, not A–C) as one cluster", () => {
    // A(0) overlaps B(1): dist 15 < 20. B(1) overlaps C(2): dist 15 < 20.
    // A(0) vs C(2): dist 30 > 20 → NOT directly overlapping. Union-find must
    // still merge all three via the shared middle bubble.
    const clusters = clusterOverlappingBubbles([
      bubble(0, 0, 10),
      bubble(15, 0, 10),
      bubble(30, 0, 10),
    ]);
    expect(normalise(clusters)).toEqual([[0, 1, 2]]);
  });

  it("separates an overlapping pair from a distant singleton", () => {
    const clusters = clusterOverlappingBubbles([
      bubble(0, 0, 10),
      bubble(8, 0, 10), // overlaps index 0
      bubble(500, 500, 10), // far from both
    ]);
    expect(normalise(clusters)).toEqual([[0, 1], [2]]);
  });

  it("uses 2D distance, not just the x-axis, to detect overlap", () => {
    // Same x, 6 apart vertically, radii 10 → distance 6 < 20 → overlap.
    const clusters = clusterOverlappingBubbles([
      bubble(50, 0, 10),
      bubble(50, 6, 10),
    ]);
    expect(normalise(clusters)).toEqual([[0, 1]]);
  });

  it("returns a partition: every index appears exactly once", () => {
    const input = [
      bubble(0, 0, 10),
      bubble(5, 0, 10),
      bubble(40, 40, 8),
      bubble(43, 40, 8),
      bubble(200, 200, 12),
    ];
    const clusters = clusterOverlappingBubbles(input);
    const flat = clusters.flat().sort((a, b) => a - b);
    expect(flat).toEqual([0, 1, 2, 3, 4]);
    // Touching pairs grouped, lone bubble separate: {0,1}, {2,3}, {4}.
    expect(normalise(clusters)).toEqual([[0, 1], [2, 3], [4]]);
  });
});
