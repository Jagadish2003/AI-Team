import { Layers, Box, Database, Check, ChevronRight, ArrowUpRight, ArrowUp, Settings } from 'lucide-react';

function Sidebar() {
  return (
    <div className="w-full xl:w-[340px] flex flex-col gap-6">
      {/* ServiceNow Integration Panel */}
      <div className="bg-[#eeeeee] border border-neutral-300 rounded-lg overflow-hidden">
        <div className="p-4 bg-[#e8e8e8] border-b border-neutral-300">
          <h3 className="font-semibold text-lg text-neutral-700 mb-3">ServiceNow Integration</h3>
          <div className="flex items-center justify-between mb-2">
            <span className="bg-[#d5d5d5] px-2 py-1 rounded text-sm flex items-center gap-1"><Check className="w-4 h-4" /> Connected</span>
            <span className="text-xs text-neutral-500">3m ago</span>
          </div>
          <a href="#" className="text-xs text-neutral-500 flex items-center gap-1 hover:underline">Learn More <ArrowUpRight className="w-3 h-3" /></a>
        </div>
        <div className="p-4 bg-[#f5f5f5]">
          <p className="text-sm text-neutral-600 mb-3">Access as:</p>
          <ul className="space-y-3 mb-6">
            <li className="flex items-center justify-between text-sm text-neutral-700"><div className="flex items-center gap-2"><Layers className="w-4 h-4 text-neutral-500 bg-neutral-300 rounded-sm p-0.5" /> CMDB items</div><ChevronRight className="w-4 h-4" /></li>
            <li className="flex items-center gap-2 text-sm text-neutral-700"><Box className="w-4 h-4 text-neutral-500 bg-neutral-300 rounded-sm p-0.5" /> Incident Tickets</li>
            <li className="flex items-center gap-2 text-sm text-neutral-700"><Database className="w-4 h-4 text-neutral-500 bg-neutral-300 rounded-sm p-0.5" /> Change Logs</li>
          </ul>
          <button className="w-full bg-[#e0e0e0] border border-neutral-300 py-2 rounded text-sm flex items-center justify-center gap-2 hover:bg-neutral-300 transition-colors">Configure & Sync <ArrowUpRight className="w-4 h-4" /></button>
        </div>
      </div>

      {/* Confidence Panel */}
      <div className="bg-[#eeeeee] border border-neutral-300 rounded-lg p-4">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2 text-neutral-700 font-medium"><ArrowUp className="w-5 h-5 text-neutral-500" /> Confidence</div>
          <div className="flex items-center gap-4 text-[10px] text-neutral-500 relative">
            <div className="flex flex-col items-center gap-1"><div className="w-3 h-3 rounded-full bg-neutral-600 relative z-10"></div><span>LOW</span></div>
            <div className="flex flex-col items-center gap-1"><div className="w-3 h-3 rounded-full bg-neutral-200 border border-neutral-400 relative z-10"></div><span>MEDIUM</span></div>
            <div className="flex flex-col items-center gap-1"><div className="w-3 h-3 rounded-full bg-transparent border border-neutral-400 relative z-10"></div><span>HIGH</span></div>
            <div className="absolute top-[5px] left-[6px] right-[6px] h-px bg-neutral-400"></div>
          </div>
        </div>
        <p className="text-sm text-neutral-700 mb-4">Connectet of 3 recommended</p>
        <div className="bg-[#f5f5f5] border border-neutral-300 rounded p-3">
          <div className="flex items-center gap-2 text-sm font-semibold text-neutral-700 mb-3"><Settings className="w-4 h-4 text-neutral-500" /> Ready to Start: <span className="font-normal uppercase text-xs">Service Now</span></div>
          <div className="flex gap-2 text-[11px] flex-wrap">
            <span className="flex items-center gap-1 text-neutral-600"><span className="w-1.5 h-1.5 bg-neutral-400 rounded-sm"></span> Jira</span>
            <span className="flex items-center gap-1 bg-[#e0e0e0] px-1.5 py-0.5 rounded text-neutral-600"><Check className="w-3 h-3" /> MICROSOFT 365</span>
            <span className="flex items-center gap-1 text-neutral-500"><span className="w-1.5 h-1.5 bg-neutral-400 rounded-sm"></span> Not Connected</span>
          </div>
        </div>
      </div>
    </div>
  );
}

export default Sidebar;