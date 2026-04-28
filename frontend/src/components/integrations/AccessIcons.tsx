import React from 'react';
import { Database, Ticket, RefreshCcw, FileText } from 'lucide-react';

export const accessIcons: Record<string, React.ReactNode> = {
  'CMDB Records': <Database size={12} className="text-accent-400" />,
  'Incident Tickets': <Ticket size={12} className="text-accent-400" />,
  'Change Logs': <RefreshCcw size={12} className="text-accent-400" />,
  fallback: <FileText size={12} className="text-accent-400" />
};