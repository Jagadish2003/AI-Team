import React from 'react';
import TopNav from '../components/common/TopNav';
import { Link } from 'react-router-dom';
export default function SourceIntakePlaceholder() {
  return (
    <div className="min-h-screen text-text">
      <TopNav />
      <div className="mx-auto max-w-4xl px-4 py-10">
        <div className="text-2xl font-semibold">Source Intake</div>
        <div className="mt-2 text-muted">Placeholder route. Screen 2 will be implemented next.</div>
        <div className="mt-6"><Link to="/integration-hub" className="text-accent underline">Back to Integration Hub</Link></div>
      </div>
    </div>
  );
}
