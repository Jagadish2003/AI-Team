import React from 'react';
import { Link, NavLink } from 'react-router-dom';

export default function TopNav() {
  return (
    <header className="sticky top-0 z-40 border-b border-border bg-[#ececed]/95 backdrop-blur">
      <div className="mx-auto flex h-[86px] max-w-[1440px] items-center justify-between px-10">
        <Link to="/integration-hub" className="flex items-center gap-3 text-text no-underline">
          <span className="text-[24px] font-semibold tracking-tight">AgentIQ</span>
        </Link>

        <div className="flex items-center gap-6">
          <NavLink
            to="/reports"
            className={({ isActive }) =>
              `text-[15px] no-underline transition-colors ${isActive ? 'font-semibold text-text' : 'text-text/85 hover:text-text'}`
            }
          >
            Reports
          </NavLink>

          <Link
            to="/integration-hub"
            aria-label="Profile"
            className="inline-flex h-11 w-11 rounded-full border border-border bg-text/10"
          />
        </div>
      </div>
    </header>
  );
}