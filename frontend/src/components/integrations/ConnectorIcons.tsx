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
