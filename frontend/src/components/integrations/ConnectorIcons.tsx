import React from 'react';
import {
  BookOpenText,
  BriefcaseBusiness,
  Building2,
  CloudCog,
  Database,
  FileText,
  GitBranch,
  GitPullRequest,
  Github,
  Gitlab,
  Kanban,
  Layers,
  Mail,
  MessagesSquare,
  PackageCheck,
  PanelTop,
  Plug,
  ServerCog,
  Share2,
  Slack,
  Snowflake,
  SquareKanban,
  Table2,
  Ticket,
  Users,
  Workflow,
} from 'lucide-react';

const iconProps = {
  size: 18,
  strokeWidth: 2,
  className: 'connector-icon',
};

function ServiceNowConnectorIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      aria-hidden="true"
      className="connector-icon h-[18px] w-[18px]"
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

// MSP-B13 (AT-748): provider-branded, theme-aware (currentColor) marks for the
// AWS/Azure Event tiles — a monochrome line style consistent with the custom
// ServiceNow icon above and the lucide line icons in this library. No external
// image URLs; the mark inherits the connector-icon accent colour.

/** AWS — the cloud + "smile" arrow, drawn as a single-colour line mark. */
function AwsConnectorIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      aria-hidden="true"
      className="connector-icon h-[18px] w-[18px]"
      fill="none"
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="2"
    >
      {/* cloud */}
      <path d="M7.5 12.5a2.6 2.6 0 0 1 .3-5.2 4 4 0 0 1 7.7-1 3.1 3.1 0 0 1 1 6.2H8a2.6 2.6 0 0 1-.5 0Z" />
      {/* smile */}
      <path d="M4.5 16.4c4.6 2.3 10.4 2.3 15 0" />
      {/* arrow head on the smile */}
      <path d="M17.8 15.4 19.7 16.3 18.9 18.2" />
    </svg>
  );
}

/** Azure — the stylised "A" chevron mark, single-colour line. */
function AzureConnectorIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      aria-hidden="true"
      className="connector-icon h-[18px] w-[18px]"
      fill="none"
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="2"
    >
      {/* left face of the A */}
      <path d="M11 4 4 19h4l4.5-10.5" />
      {/* right face + base tie of the A */}
      <path d="M12.2 8 16.5 19H20L13.2 4 12.2 8Z" />
      <path d="M9.5 15.5h6.2" />
    </svg>
  );
}

export const connectorIcons: Record<string, React.ReactNode> = {
  Salesforce: <CloudCog {...iconProps} />,
  ServiceNow: <ServiceNowConnectorIcon />,
  'Jira & Confluence': <SquareKanban {...iconProps} />,
  SAP: <PackageCheck {...iconProps} />,
  'Oracle EBS': <Building2 {...iconProps} />,
  Workday: <Users {...iconProps} />,
  'Dynamics 365': <BriefcaseBusiness {...iconProps} />,

  Jira: <Kanban {...iconProps} />,
  'Azure DevOps': <Workflow {...iconProps} />,
  Linear: <GitPullRequest {...iconProps} />,
  Zendesk: <Ticket {...iconProps} />,

  'Microsoft Teams': <MessagesSquare {...iconProps} />,
  'Microsoft 365': <Mail {...iconProps} />,
  Confluence: <BookOpenText {...iconProps} />,
  SharePoint: <Share2 {...iconProps} />,
  Notion: <PanelTop {...iconProps} />,
  Slack: <Slack {...iconProps} />,

  'AWS Events': <AwsConnectorIcon />,
  'Azure Events': <AzureConnectorIcon />,

  GitHub: <Github {...iconProps} />,
  GitLab: <Gitlab {...iconProps} />,
  Bitbucket: <GitBranch {...iconProps} />,
  'Azure Repos': <GitBranch {...iconProps} />,
  PostgreSQL: <Database {...iconProps} />,
  'SQL Server': <ServerCog {...iconProps} />,
  'Oracle DB': <Table2 {...iconProps} />,
  Databricks: <Layers {...iconProps} />,
  Snowflake: <Snowflake {...iconProps} />,
  dbt: <FileText {...iconProps} />,
};

export const fallbackConnectorIcon = <Plug {...iconProps} />;
