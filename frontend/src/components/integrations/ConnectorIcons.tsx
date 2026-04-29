import React from 'react';
import { Settings, Kanban, LayoutGrid, Box, Github, MessageSquare, Database, Cloud } from 'lucide-react';

export const connectorIcons: Record<string, React.ReactNode> = {
  Salesforce: <Cloud size={18} strokeWidth={2} className="text-[#94A3B8]" />,
  ServiceNow: <Settings size={18} strokeWidth={2} className="text-[#94A3B8]" />,
  'Jira & Confluence': <Kanban size={18} strokeWidth={2} className="text-[#94A3B8]" />,
  'Microsoft 365': <LayoutGrid size={18} strokeWidth={2} className="text-[#94A3B8]" />,
  SAP: <Box size={18} strokeWidth={2} className="text-[#94A3B8]" />,
  GitHub: <Github size={18} strokeWidth={2} className="text-[#94A3B8]" />,
  Slack: <MessageSquare size={18} strokeWidth={2} className="text-[#94A3B8]" />,
  Databricks: <Database size={18} strokeWidth={2} className="text-[#94A3B8]" />
};