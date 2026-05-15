import React from 'react';
import {
  AppWindow,
  CloudCog,
  Github,
  Layers,
  PackageCheck,
  Plug,
  Slack,
  SquareKanban,
} from 'lucide-react';

const iconProps = {
  size: 18,
  strokeWidth: 2,
  className: 'text-muted',
};

function ServiceNowConnectorIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      aria-hidden="true"
      className="h-[18px] w-[18px] text-muted"
      fill="none"
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="2"
    >
      <path d="M18.6 18.1A9 9 0 1 0 3 12a8.8 8.8 0 0 0 2.4 6.1c.5.6 1.4.6 2 .1A6.8 6.8 0 0 1 12 16.5a6.8 6.8 0 0 1 4.6 1.7c.6.5 1.5.5 2-.1Z" />
      <path d="M12 8.2a3.8 3.8 0 1 1 0 7.6 3.8 3.8 0 0 1 0-7.6Z" />
    </svg>
  );
}

export const connectorIcons: Record<string, React.ReactNode> = {
  Salesforce: <CloudCog {...iconProps} />,
  ServiceNow: <ServiceNowConnectorIcon />,
  'Jira & Confluence': <SquareKanban {...iconProps} />,
  'Microsoft 365': <AppWindow {...iconProps} />,
  SAP: <PackageCheck {...iconProps} />,
  GitHub: <Github {...iconProps} />,
  Slack: <Slack {...iconProps} />,
  Databricks: <Layers {...iconProps} />,
};

export const fallbackConnectorIcon = <Plug {...iconProps} />;
