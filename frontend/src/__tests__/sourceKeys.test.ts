import { describe, expect, it } from 'vitest';
import {
  SOURCE_KEY_MAP,
  connectorIdForSourceKey,
  sourceKeyForConnector,
  zeroSignalReason,
} from '../utils/sourceKeys';

describe('sourceKeys registry', () => {
  it('SK1: maps connector IDs to canonical source keys', () => {
    expect(sourceKeyForConnector('servicenow')).toBe('ServiceNow');
    expect(sourceKeyForConnector('jira')).toBe('Jira');
  });

  it('SK1: maps source keys back to canonical connector IDs', () => {
    expect(connectorIdForSourceKey('Jira')).toBe('jira');
  });

  it('keeps current bundled Jira connector as an alias without duplicating values', () => {
    expect(sourceKeyForConnector('jira_confluence')).toBe('Jira');
  });

  it('SK2: SOURCE_KEY_MAP has no duplicate values', () => {
    const values = Object.values(SOURCE_KEY_MAP);
    expect(new Set(values).size).toBe(values.length);
  });

  it('returns the requested zero-signal sub-states', () => {
    expect(zeroSignalReason('loading', 0, 0)).toBe('checking');
    expect(zeroSignalReason('warning', 0, 0)).toBe('permissionLimited');
    expect(zeroSignalReason('confirmed', 0, 1)).toBe('notAnalyzed');
    expect(zeroSignalReason('confirmed', 0, 0)).toBe('noSignals');
    expect(zeroSignalReason('unknown', 0, 0)).toBe('unknown');
  });
});
