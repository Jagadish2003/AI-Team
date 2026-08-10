/**
 * 2.0-C4 T3 (AT-844) — org-config migration assist.
 *
 * Parent-story criterion exercised here (UI half):
 *
 *   AC2 — migration previews the config change, applies it on confirmation, and is
 *         reversible.
 *
 * The load-bearing behaviours, tested throughout:
 *
 *   * the change set is SHOWN before anything is applied — both the old and the new
 *     value, per field;
 *   * applying sends the fingerprint of the plan on screen, so the thing applied is
 *     provably the thing displayed;
 *   * undo is offered immediately after applying, never hidden behind a re-preview;
 *   * a reference the migration could not map is NAMED, not silently omitted;
 *   * nothing renders when there is no migration to offer, and a preview failure
 *     degrades to nothing rather than a half-rendered offer.
 *
 * The backend halves are pinned in ``backend/tests/unit/test_pack_migration.py`` and
 * ``backend/tests/contract/test_pack_migration_api.py``.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import PackMigrationAssist from '../components/common/PackMigrationAssist';
import type {
  PackMigrationPlan,
  PackMigrationRecord,
} from '../types/packMigration';

const previewPackMigration = vi.fn();
const applyPackMigration = vi.fn();
const revertPackMigration = vi.fn();

vi.mock('../api/packMigrationApi', () => ({
  previewPackMigration: (...args: unknown[]) => previewPackMigration(...args),
  applyPackMigration: (...args: unknown[]) => applyPackMigration(...args),
  revertPackMigration: (...args: unknown[]) => revertPackMigration(...args),
  fetchPackMigrations: vi.fn(),
}));

const plan = (overrides: Partial<PackMigrationPlan> = {}): PackMigrationPlan => ({
  orgId: 'org_1',
  packId: 'cloud_ops',
  packName: 'Cloud Operations',
  replacementPackId: 'enterprise_ops',
  replacementPackName: 'Enterprise Operations',
  available: true,
  applicable: true,
  reason: '',
  reasonCode: '',
  changes: [
    {
      surface: 'stack_builder_setup_state',
      field: 'packId',
      previousValue: 'cloud_ops',
      newValue: 'enterprise_ops',
      description: '',
    },
    {
      surface: 'stack_builder_setup_state',
      field: 'packIds',
      previousValue: ['cloud_ops', 'service_cloud'],
      newValue: ['enterprise_ops', 'service_cloud'],
      description: '',
    },
  ],
  unmapped: [],
  warnings: [],
  deprecation: null,
  evaluatedOn: '2026-08-06',
  fingerprint: 'fp_abc123',
  ...overrides,
});

const record = (
  overrides: Partial<PackMigrationRecord> = {},
): PackMigrationRecord => ({
  id: 'pmig_1',
  kind: 'apply',
  orgId: 'org_1',
  packId: 'cloud_ops',
  replacementPackId: 'enterprise_ops',
  changes: plan().changes,
  unmapped: [],
  warnings: [],
  reason: null,
  actorId: 'user_owner',
  at: '2026-08-06T09:00:00Z',
  fingerprint: 'fp_abc123',
  revertsMigrationId: null,
  reverted: false,
  revertedAt: null,
  revertedBy: null,
  changed: true,
  ...overrides,
});

beforeEach(() => {
  previewPackMigration.mockReset();
  applyPackMigration.mockReset();
  revertPackMigration.mockReset();
});

describe('preview', () => {
  it('shows every field that would change, with both values', async () => {
    previewPackMigration.mockResolvedValue(plan());
    render(<PackMigrationAssist packId="cloud_ops" />);

    await screen.findByTestId('pack-migration-assist');
    expect(screen.getByTestId('pack-migration-change-packId-from')).toHaveTextContent(
      'cloud_ops',
    );
    expect(screen.getByTestId('pack-migration-change-packId-to')).toHaveTextContent(
      'enterprise_ops',
    );
    expect(
      screen.getByTestId('pack-migration-change-packIds-to'),
    ).toHaveTextContent('enterprise_ops, service_cloud');
  });

  it('applies nothing while it is only previewing', async () => {
    previewPackMigration.mockResolvedValue(plan());
    render(<PackMigrationAssist packId="cloud_ops" />);

    await screen.findByTestId('pack-migration-assist');
    expect(applyPackMigration).not.toHaveBeenCalled();
  });

  it('names a reference it could not map instead of omitting it', async () => {
    previewPackMigration.mockResolvedValue(
      plan({
        unmapped: [
          {
            surface: 'stack_builder_setup_state',
            field: 'templateIds',
            value: 'managed_cloud_operations',
            reason: 'no_replacement_template',
            detail:
              "No registered template declares pack 'enterprise_ops', so template " +
              "'managed_cloud_operations' is left selected.",
          },
        ],
      }),
    );
    render(<PackMigrationAssist packId="cloud_ops" />);

    expect(
      await screen.findByTestId(
        'pack-migration-unmapped-managed_cloud_operations',
      ),
    ).toHaveTextContent('is left selected');
  });

  it('surfaces a warning about the destination', async () => {
    previewPackMigration.mockResolvedValue(
      plan({
        warnings: [
          {
            code: 'replacement_pack_disabled',
            detail:
              "Replacement pack 'enterprise_ops' is disabled for this organisation.",
          },
        ],
      }),
    );
    render(<PackMigrationAssist packId="cloud_ops" />);

    expect(
      await screen.findByTestId(
        'pack-migration-warning-replacement_pack_disabled',
      ),
    ).toHaveTextContent('is disabled');
  });
});

describe('nothing to offer', () => {
  it('renders nothing when no migration is available', async () => {
    previewPackMigration.mockResolvedValue(
      plan({
        available: false,
        applicable: false,
        changes: [],
        reason: "Pack 'cloud_ops' names no registered replacement pack.",
        reasonCode: 'no_replacement_declared',
      }),
    );
    const { container } = render(<PackMigrationAssist packId="cloud_ops" />);

    await waitFor(() => expect(previewPackMigration).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when this org's configuration does not select the pack", async () => {
    previewPackMigration.mockResolvedValue(
      plan({ applicable: false, changes: [] }),
    );
    const { container } = render(<PackMigrationAssist packId="cloud_ops" />);

    await waitFor(() => expect(previewPackMigration).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it('degrades to nothing when the preview cannot be read', async () => {
    previewPackMigration.mockRejectedValue(new Error('boom'));
    const { container } = render(<PackMigrationAssist packId="cloud_ops" />);

    await waitFor(() => expect(previewPackMigration).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });
});

describe('apply', () => {
  it('confirms the exact plan on screen by sending its fingerprint', async () => {
    previewPackMigration.mockResolvedValue(plan());
    applyPackMigration.mockResolvedValue(record());
    render(<PackMigrationAssist packId="cloud_ops" />);

    fireEvent.click(await screen.findByTestId('pack-migration-apply'));

    await waitFor(() =>
      expect(applyPackMigration).toHaveBeenCalledWith('cloud_ops', {
        fingerprint: 'fp_abc123',
      }),
    );
  });

  it('reports what changed and tells the reader history is untouched', async () => {
    previewPackMigration.mockResolvedValue(plan());
    applyPackMigration.mockResolvedValue(record());
    render(<PackMigrationAssist packId="cloud_ops" />);

    fireEvent.click(await screen.findByTestId('pack-migration-apply'));

    const applied = await screen.findByTestId('pack-migration-applied');
    expect(applied).toHaveTextContent('Migrated 2 configuration fields');
    expect(applied).toHaveTextContent('existing runs and findings are unchanged');
  });

  it('notifies the page so it can reload the migrated configuration', async () => {
    previewPackMigration.mockResolvedValue(plan());
    applyPackMigration.mockResolvedValue(record());
    const onMigrated = vi.fn();
    render(<PackMigrationAssist packId="cloud_ops" onMigrated={onMigrated} />);

    fireEvent.click(await screen.findByTestId('pack-migration-apply'));

    await waitFor(() => expect(onMigrated).toHaveBeenCalled());
  });

  it('shows a stale-preview refusal rather than retrying blind', async () => {
    previewPackMigration.mockResolvedValue(plan());
    applyPackMigration.mockRejectedValue({
      status: 409,
      body: { detail: 'The configuration changed since it was previewed.' },
    });
    render(<PackMigrationAssist packId="cloud_ops" />);

    fireEvent.click(await screen.findByTestId('pack-migration-apply'));

    expect(await screen.findByTestId('pack-migration-error')).toHaveTextContent(
      'changed since it was previewed',
    );
    // Still on the preview, with a freshly-read plan.
    expect(screen.getByTestId('pack-migration-apply')).toBeInTheDocument();
    expect(previewPackMigration).toHaveBeenCalledTimes(2);
  });

  it("explains a role refusal in the reader's own terms", async () => {
    previewPackMigration.mockResolvedValue(plan());
    applyPackMigration.mockRejectedValue({ status: 403, body: {} });
    render(<PackMigrationAssist packId="cloud_ops" />);

    fireEvent.click(await screen.findByTestId('pack-migration-apply'));

    expect(await screen.findByTestId('pack-migration-error')).toHaveTextContent(
      'Only a workspace owner',
    );
  });
});

describe('revert', () => {
  it('offers undo as soon as the migration is applied', async () => {
    previewPackMigration.mockResolvedValue(plan());
    applyPackMigration.mockResolvedValue(record());
    render(<PackMigrationAssist packId="cloud_ops" />);

    fireEvent.click(await screen.findByTestId('pack-migration-apply'));

    expect(await screen.findByTestId('pack-migration-revert')).toBeInTheDocument();
  });

  it('reverts the migration it applied, by id', async () => {
    previewPackMigration.mockResolvedValue(plan());
    applyPackMigration.mockResolvedValue(record({ id: 'pmig_42' }));
    revertPackMigration.mockResolvedValue(
      record({ id: 'pmig_43', kind: 'revert', revertsMigrationId: 'pmig_42' }),
    );
    render(<PackMigrationAssist packId="cloud_ops" />);

    fireEvent.click(await screen.findByTestId('pack-migration-apply'));
    fireEvent.click(await screen.findByTestId('pack-migration-revert'));

    await waitFor(() =>
      expect(revertPackMigration).toHaveBeenCalledWith('pmig_42'),
    );
    // Back to the preview: the migration is available to make again.
    expect(await screen.findByTestId('pack-migration-apply')).toBeInTheDocument();
  });

  it('surfaces a refusal to discard a later edit', async () => {
    previewPackMigration.mockResolvedValue(plan());
    applyPackMigration.mockResolvedValue(record());
    revertPackMigration.mockRejectedValue({
      status: 409,
      body: {
        detail:
          'The configuration has changed since this migration was applied ' +
          '(packIds no longer holds the migrated value).',
      },
    });
    render(<PackMigrationAssist packId="cloud_ops" />);

    fireEvent.click(await screen.findByTestId('pack-migration-apply'));
    fireEvent.click(await screen.findByTestId('pack-migration-revert'));

    expect(await screen.findByTestId('pack-migration-error')).toHaveTextContent(
      'no longer holds the migrated value',
    );
  });
});
