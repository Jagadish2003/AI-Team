import React, { useState, useRef, useEffect } from 'react';
import { Search, ChevronDown } from 'lucide-react';
import { OpportunityTier } from '../../types/analystReview';
 
export type TierFilter = 'All' | OpportunityTier;
export type ConfidenceFilter = 'All' | 'LOW' | 'MEDIUM' | 'HIGH';
export type DecisionFilter = 'All' | 'UNREVIEWED' | 'APPROVED' | 'REJECTED';
 
function CustomDropdown<T extends string>({
  options,
  value,
  onChange,
  label,
}: {
  options: { value: T; label: string }[];
  value: T;
  onChange: (v: T) => void;
  label: string;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
 
  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);
 
  const selectedLabel = options.find((o) => o.value === value)?.label ?? value;
 
  return (
    <div ref={ref} className="relative w-full">
      <button
        onClick={() => setOpen((o) => !o)}
        className="w-full cursor-pointer whitespace-nowrap rounded-md border border-border bg-bg/50 px-3 py-2 text-sm text-text hover:border-[#0D55D7]/50 transition-colors focus:outline-none focus:border-[#0D55D7] focus:ring-2 focus:ring-[#0D55D7]/50"
      >
        <span className="flex items-center justify-between gap-2">
          <span>{label}: {selectedLabel}</span>
          <ChevronDown
            size={14}
            className={`text-muted transition-transform ${open ? 'rotate-180' : ''}`}
          />
        </span>
      </button>
 
      {open && (
        <div className="absolute left-0 z-50 mt-1 w-full overflow-hidden rounded-lg border border-border bg-panel shadow-lg">
          {options.map((opt) => (
            <div
              key={opt.value}
              onClick={() => {
                onChange(opt.value);
                setOpen(false);
              }}
              className={`cursor-pointer px-4 py-2 text-sm transition-colors ${
                opt.value === value
                  ? 'bg-[#0D55D7] font-medium text-[#0d1117]'
                  : 'text-text hover:bg-[#0D55D7]/15 hover:text-[#0D55D7]'
              }`}
            >
              {opt.label}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
 
const tierOptions: { value: TierFilter; label: string }[] = [
  { value: 'All', label: 'All' },
  { value: 'Quick Win', label: 'Quick Win' },
  { value: 'Strategic', label: 'Strategic' },
  { value: 'Complex', label: 'Complex' },
];
 
const confOptions: { value: ConfidenceFilter; label: string }[] = [
  { value: 'All', label: 'All' },
  { value: 'HIGH', label: 'High' },
  { value: 'MEDIUM', label: 'Medium' },
  { value: 'LOW', label: 'Low' },
];
 
const decisionOptions: { value: DecisionFilter; label: string }[] = [
  { value: 'All', label: 'All' },
  { value: 'UNREVIEWED', label: 'Unreviewed' },
  { value: 'APPROVED', label: 'Approved' },
  { value: 'REJECTED', label: 'Rejected' },
];
 
export default function OpportunityToolbar({
  q, onQ,
  tier, onTier,
  conf, onConf,
  decision, onDecision,
  totalShown,
}: {
  q: string;
  onQ: (v: string) => void;
  tier: TierFilter;
  onTier: (v: TierFilter) => void;
  conf: ConfidenceFilter;
  onConf: (v: ConfidenceFilter) => void;
  decision: DecisionFilter;
  onDecision: (v: DecisionFilter) => void;
  totalShown: number;
}) {
  return (
    <div className="mb-4 rounded-xl border border-border bg-panel p-3">
      <div className="grid grid-cols-1 gap-2 md:grid-cols-[1fr_200px_200px_200px]">
 
        {/* Search */}
        <div className="relative">
          <input
            value={q}
            onChange={e => onQ(e.target.value)}
            placeholder="Search opportunities…"
            className="w-full rounded-md border border-border bg-bg/50 px-3 py-2 pr-10 text-sm text-text placeholder:text-muted hover:border-[#0D55D7]/50 transition-colors focus:outline-none focus:border-[#0D55D7] focus:ring-2 focus:ring-[#0D55D7]/50 appearance-none"
          />
          <Search className="absolute right-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted pointer-events-none" />
        </div>
 
        {/* Tier */}
        <CustomDropdown<TierFilter>
          options={tierOptions}
          value={tier}
          onChange={onTier}
          label="Tier"
        />
 
        {/* Confidence */}
        <CustomDropdown<ConfidenceFilter>
          options={confOptions}
          value={conf}
          onChange={onConf}
          label="Confidence"
        />
 
        {/* Decision */}
        <CustomDropdown<DecisionFilter>
          options={decisionOptions}
          value={decision}
          onChange={onDecision}
          label="Decision"
        />
 
      </div>
 
      <div className="mt-2 text-xs text-muted">
        Showing <span className="font-semibold text-text">{totalShown}</span> opportunities.
      </div>
    </div>
  );
}
 