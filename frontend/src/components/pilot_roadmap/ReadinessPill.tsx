import React from 'react';
import { Readiness } from '../../types/pilotRoadmap';

interface Props { status: Readiness; }

export default function ReadinessPill({ status }: Props) {
  const cls =
    status === 'READY'   ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-200' :
    status === 'PENDING' ? 'border-amber-500/40  bg-amber-500/10  text-amber-200'   :
                           'border-red-500/40    bg-red-500/10    text-red-200';
  return (
    <span className={`rounded-full border px-2 py-0.5 text-xs ${cls}`}>
      {status}
    </span>
  );
}