import React from 'react';
import { Link, useLocation } from 'react-router-dom';

const items = [
  { to: '/integration-hub', label: 'Integration Hub' },
  { to: '/source-intake', label: 'Source Intake' },
  { to: '/reports', label: 'Reports' }
];

export default function TopNav() {
  const loc = useLocation();

  return (
    <div className="sticky top-0 z-40 border-b border-border bg-bg/80 backdrop-blur">

      <div className="mx-auto flex w-full items-center px-6 py-3">

        {/* LEFT SIDE LOGO */}
        <div className="flex items-center gap-2 ml-3">
          <div className="h-7 w-7 rounded-md bg-accent/20" />
          <div className="font-semibold text-text text-[24px]">AgentIQ</div>
        </div>

        {/* RIGHT SIDE NAV */}
        <div className="ml-auto flex items-center text-sm">

          {/* NAV ITEMS */}
          {items.map(i => {
            const active = loc.pathname === i.to;

            return (
              <div key={i.to} className="border-r border-border pr-4 mr-4">
                <Link
                  to={i.to}
                  className={`rounded-md px-3 py-2 ${
                    active
                      ? 'bg-panel2 text-text'
                      : 'text-muted hover:bg-panel2 hover:text-text'
                  }`}
                >
                  {i.label}
                </Link>
              </div>
            );
          })}

          {/* Administrator (NO BORDER HERE) */}
          <div className="pr-4 mr-4 flex items-center gap-1 text-muted cursor-pointer rounded-md px-3 py-2 hover:bg-panel2 hover:text-text">
            Administrator
            <i className="bi bi-caret-down-fill text-xs mt-[2px]"></i>
          </div>

          {/* Avatar */}
          <div className="flex items-center justify-center h-8 w-8 rounded-full bg-panel2">
            <i className="bi bi-person-fill text-lg"></i>
          </div>

        </div>

      </div>

    </div>
  );
}