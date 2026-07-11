/**
 * R18-C0 P3 — 'Coming soon' toast/caption sweep (AC3)
 *
 * Internal delivery-planning language ("later sprint", "available in Sprint N",
 * and similar release terminology) must never appear in customer-facing product
 * strings. Forward-looking features say "coming soon" instead.
 *
 * AC3 (Testable): "A text sweep finds zero occurrences of 'sprint' in
 * customer-facing strings; forward-looking features say 'coming soon'."
 *
 * This test performs that sweep over the frontend source tree. It scans the
 * text that actually renders in the product — string/JSX literals — after
 * stripping code comments (JSDoc, // and block comments). Test files are
 * excluded from the glob because they are not shipped to users.
 *
 * The Jira Agile "Sprint" object is a legitimate connector read-scope noun
 * (e.g. "Read Sprint data") and is NOT internal delivery language, so the
 * detector targets delivery-planning phrasings, not the bare word.
 *
 * Reads sources via Vite's import.meta.glob (raw) so no Node fs/types are
 * needed in the browser-mode Vitest environment.
 *
 * Run:
 *   npx vitest run src/__tests__/R18C0_ComingSoonSweep.test.tsx
 */

import { describe, it, expect } from 'vitest';

// Eagerly load every source file (excluding tests) as raw text.
const sources = import.meta.glob('../**/*.{ts,tsx}', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>;

// Delivery-planning language that must not reach the product surface.
// Deliberately does NOT match the standalone Jira "Sprint" object noun.
const FORBIDDEN_PATTERNS: RegExp[] = [
  /later sprint/i,
  /future sprint/i,
  /next sprint/i,
  /upcoming sprint/i,
  /(available|coming|added|wired|shipping|planned)\s+(in|for)\s+sprint\s*\d/i,
  /in\s+sprint\s*\d/i,
  /sprint\s*\d[\d.]*\s+(will|adds?|introduces?)/i,
];

/**
 * Strip code comments so the sweep only inspects rendered text.
 * Removes block/JSX comments and // line comments.
 */
function stripComments(source: string): string {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, ' ') // block + JSX comments
    .replace(/(^|[^:])\/\/[^\n]*/g, '$1'); // line comments
}

function isTestFile(filePath: string): boolean {
  return /\.test\.(ts|tsx)$/.test(filePath) || filePath.includes('/__tests__/');
}

describe('R18-C0 P3 — no internal sprint delivery language in customer-facing strings (AC3)', () => {
  const entries = Object.entries(sources).filter(([p]) => !isTestFile(p));

  it('finds source files to sweep', () => {
    expect(entries.length).toBeGreaterThan(0);
  });

  it('has zero delivery-planning "sprint" language in rendered strings', () => {
    const violations: string[] = [];

    for (const [filePath, raw] of entries) {
      const code = stripComments(raw);
      code.split('\n').forEach((line, idx) => {
        for (const pattern of FORBIDDEN_PATTERNS) {
          if (pattern.test(line)) {
            violations.push(`${filePath}:${idx + 1}: ${line.trim()}`);
          }
        }
      });
    }

    expect(
      violations,
      `Internal delivery language found in customer-facing strings. ` +
        `Replace with "coming soon":\n${violations.join('\n')}`,
    ).toEqual([]);
  });
});
