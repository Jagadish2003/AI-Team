import React from 'react';
import { Connector } from '../../types/connector';
import Badge from '../common/Badge';
import Button from '../common/Button';

const accessIcons: Record<string, string> = {
  "CMDB Records": "bi-database-fill",
  "Incident Tickets": "bi-ticket-perforated-fill",
  "Change Logs": "bi-arrow-repeat"
};

export default function ConnectorDetailPanel({
  connector,
  onConfigure
}: {
  connector: Connector | null;
  onConfigure: () => void;
}) {

  if (!connector) {
    return (
      <div className="rounded-xl border border-border bg-panel p-4 text-sm text-muted">
        Select a connector to view details.
      </div>
    );
  }

  const isConnected = connector.status === 'connected';

  return (
    <div className="rounded-xl border border-border bg-panel p-5">

      {/* HEADER */}
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="text-base font-semibold text-text">
            {connector.name} Integration
          </div>
          <div className="mt-1 text-sm text-muted">
            {connector.category}
          </div>
        </div>

        <Badge status={connector.status} />
      </div>

      {/* LAST SYNC + LEARN MORE */}
      <div className="mt-3 flex items-center justify-between text-xs text-muted">

        <div>
          Last sync:{" "}
          <span className="text-text">
            {isConnected ? connector.lastSynced : "—"}
          </span>
        </div>

        <button className="text-accent hover:underline">
          Learn More ↗
        </button>

      </div>

      {/* DIVIDER */}
      <div className="mt-4 border-t border-border" />

      {/* ACCESS AS */}
      <div className="mt-4">

        <div className="text-sm font-medium text-text mb-2">
          Access as:
        </div>

        <div className="space-y-2">

          {connector.reads.slice(0, 3).map((r) => (

            <div
              key={r}
              className="flex items-center justify-between rounded-md border border-border px-3 py-2 hover:bg-panel2"
            >

              <div className="flex items-center gap-2 text-sm text-text">

                {/* ICON BOX */}
                <div className="h-5 w-5 rounded bg-accent/20 flex items-center justify-center">
                  <i className={`bi ${accessIcons[r] || "bi-file-earmark"} text-[10px] text-accent`} />
                </div>

                {r}

              </div>

              <span className="text-muted">›</span>

            </div>

          ))}

        </div>
      </div>

      {/* BUTTON */}
      <div className="mt-5">
        <Button
          variant="primary"
          className="w-full whitespace-nowrap"
          onClick={onConfigure}
          disabled={connector.status === "coming_soon"}
        >
          Configure & Sync
        </Button>
      </div>

    </div>
  );
}