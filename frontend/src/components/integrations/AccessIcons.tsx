import React from 'react';
import {
  AtSign,
  BadgeCheck,
  BadgeDollarSign,
  BookOpenText,
  BriefcaseBusiness,
  Bug,
  ClipboardCheck,
  Database,
  FilePenLine,
  FileText,
  GitBranch,
  GitCommitHorizontal,
  GitPullRequest,
  Hash,
  MessagesSquare,
  NotebookTabs,
  RefreshCcw,
  TicketCheck,
  Truck,
  Users,
  Workflow,
} from 'lucide-react';

const iconProps = {
  size: 13,
  strokeWidth: 2,
  className: 'text-accent',
};

export const accessIcons: Record<string, React.ReactNode> = {
  Accounts: <Users {...iconProps} />,
  Opportunities: <BadgeDollarSign {...iconProps} />,
  Cases: <BriefcaseBusiness {...iconProps} />,

  'CMDB Records': <Database {...iconProps} />,
  'Incident Tickets': <TicketCheck {...iconProps} />,
  'Change Logs': <RefreshCcw {...iconProps} />,

  Issues: <Bug {...iconProps} />,
  Transitions: <GitBranch {...iconProps} />,
  'Runbooks / Pages': <BookOpenText {...iconProps} />,

  'Change Documents': <FilePenLine {...iconProps} />,
  Approvals: <ClipboardCheck {...iconProps} />,
  'Transports (if available)': <Truck {...iconProps} />,

  'Pull Requests': <GitPullRequest {...iconProps} />,
  'Review Comments': <MessagesSquare {...iconProps} />,
  Commits: <GitCommitHorizontal {...iconProps} />,

  Channels: <Hash {...iconProps} />,
  Threads: <MessagesSquare {...iconProps} />,
  Mentions: <AtSign {...iconProps} />,

  'Job Runs': <BadgeCheck {...iconProps} />,
  Pipelines: <Workflow {...iconProps} />,
  Notebooks: <NotebookTabs {...iconProps} />,

  fallback: <FileText {...iconProps} />,
};
