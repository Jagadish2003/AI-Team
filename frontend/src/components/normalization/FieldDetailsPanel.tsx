import React from 'react';
import { Check, X } from 'lucide-react';
import { useNormalizationContext } from '../../context/NormalizationContext';
import { useToast } from '../../components/common/Toast';

export default function FieldDetailsPanel() {
  const { selectedRow, relevantPermissions } = useNormalizationContext();
  const { push } = useToast(); // ✅ added

  return (
    <div className="rounded-xl border border-border bg-panel p-4 h-full flex flex-col">
      
      <div className="text-xl font-semibold text-text mb-3">
        Field Details
      </div>

      {!selectedRow ? (
        <div className="text-sm text-muted">
          Select a row to view details.
        </div>
      ) : (
        <>
          {/* Header */}
          <div className="text-sm font-semibold text-text">
            {selectedRow.sourceSystem}.{selectedRow.sourceField}
          </div>

          <div className="mt-1 text-xs text-muted">
            Source: {selectedRow.sourceSystem} · {selectedRow.sourceType}
          </div>

          {/* Common Field */}
          <div className="mt-4 rounded-lg border border-border bg-bg/20 px-3 py-2">
            <div className="flex items-center justify-between gap-3">
              <div className="min-w-0">
                <div className="truncate text-sm text-text">
                  {selectedRow.commonField}
                </div>
                {selectedRow.notes && (
                  <div className="mt-1 text-xs text-muted">
                    {selectedRow.notes}
                  </div>
                )}
              </div>
            </div>
          </div>

          {/* Sample Values */}
          <div className="mt-4 rounded-lg border border-border bg-bg/20 p-3">
            <div className="mb-2 text-xs font-semibold text-muted">
              Sample values:
            </div>
            <div className="space-y-1 text-sm text-text">
              {selectedRow.sampleValues.map((value, index) => (
                <div key={index}>{value}</div>
              ))}
            </div>
          </div>

          {/* Permissions */}
          <div className="mt-4 rounded-lg border border-border bg-bg/20 p-3">
            <div className="flex items-center justify-between">
              
              <div className="text-xs font-semibold text-muted">
                Required Permissions
              </div>

              {/* ✅ Toast added (no style change) */}
              <span
                onClick={() => push('More details available in later sprint.')}
                className="text-xs text-muted underline cursor-pointer"
              >
                Learn more
              </span>
            </div>

            {relevantPermissions.length === 0 ? (
              <div className="mt-3 text-sm text-muted">
                No permissions defined for this source yet.
              </div>
            ) : (
              <div className="mt-2 border-t border-border pt-3 space-y-3">
                {relevantPermissions.map((permission) => (
                  <div
                    key={permission.id}
                    className="flex items-center gap-3 text-sm text-text"
                  >
                    {permission.satisfied ? (
                      <Check
                        size={16}
                        strokeWidth={3}
                        className="text-white shrink-0"
                      />
                    ) : (
                      <X
                        size={16}
                        strokeWidth={3}
                        className="text-white shrink-0"
                      />
                    )}
                    <span>{permission.label}</span>
                  </div>
                ))}
              </div>
            )}
          </div>

          <div className="flex-1" />
        </>
      )}
    </div>
  );
}