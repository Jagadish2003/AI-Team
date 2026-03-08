import { Settings, Layers, Box, Github, MessageSquare, Check } from 'lucide-react';

function IntegrationPanels() {
  return (
    <div className="flex-grow space-y-6">
      {/* Start Here Panel */}
      <div className="bg-[#e8e8e8] border border-neutral-300 rounded-lg p-5">
        <div className="flex items-end gap-2 mb-1">
          <h2 className="text-lg font-bold text-neutral-800">Start here</h2>
          <span className="text-neutral-600 mb-[2px]">(fastest to value)</span>
        </div>
        <p className="text-sm text-neutral-600 mb-4">Connect 1 to start discovery</p>
        
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          {/* ServiceNow Card */}
          <div className="bg-[#f2f2f2] border border-neutral-300 rounded-md p-4 flex flex-col justify-between">
            <div>
              <div className="flex items-center gap-2 mb-4"><Settings className="w-5 h-5 text-neutral-500" /><span className="font-semibold text-neutral-700">ServiceNow</span></div>
              <ul className="text-sm text-neutral-600 space-y-2 mb-6">
                <li className="flex items-center gap-2"><div className="w-1.5 h-1.5 rounded-full bg-neutral-400"></div><span className="font-semibold">2,847</span> Incidents</li>
                <li className="flex items-center gap-2"><div className="w-1.5 h-1.5 rounded-full bg-neutral-400"></div><span>312 CMDB items</span></li>
              </ul>
            </div>
            <div className="flex gap-2"><button className="flex-1 bg-[#e0e0e0] border border-neutral-300 text-sm py-1.5 rounded hover:bg-neutral-300 transition-colors">Configure & Sync</button><button className="flex-1 bg-[#e0e0e0] border border-neutral-300 text-sm py-1.5 rounded hover:bg-neutral-300 transition-colors">View data</button></div>
          </div>
          {/* Jira Card */}
          <div className="bg-[#f2f2f2] border border-neutral-300 rounded-md p-4 flex flex-col justify-between">
            <div>
              <div className="flex items-center gap-2 mb-4"><Layers className="w-5 h-5 text-neutral-500" /><span className="font-semibold text-neutral-700">Jira & Confluence</span></div>
              <ul className="text-sm text-neutral-600 space-y-2 mb-6">
                <li className="flex items-center gap-2"><div className="w-1.5 h-1.5 rounded-full bg-neutral-400"></div><span className="font-semibold">5,847</span> Records ingested</li>
                <li className="flex items-center gap-2"><div className="w-1.5 h-1.5 rounded-full bg-neutral-400"></div><span>Emails - Teams Chats</span></li>
              </ul>
            </div>
            <div className="flex gap-2"><button className="flex-1 bg-[#e0e0e0] border border-neutral-300 text-sm py-1.5 rounded hover:bg-neutral-300 transition-colors">Configure & Sync</button><button className="flex-1 bg-[#e0e0e0] border border-neutral-300 text-sm py-1.5 rounded hover:bg-neutral-300 transition-colors">View data</button></div>
          </div>
          {/* Microsoft 365 Card */}
          <div className="bg-[#f2f2f2] border border-neutral-300 rounded-md p-4 flex flex-col justify-between">
            <div>
              <div className="flex items-center gap-2 mb-4"><Box className="w-5 h-5 text-neutral-500" /><span className="font-semibold text-neutral-700">Microsoft 365</span></div>
              <ul className="text-sm text-neutral-600 space-y-2 mb-6">
                <li className="flex items-center gap-2"><div className="w-1.5 h-1.5 rounded-full bg-neutral-400"></div><span>Not connected</span></li>
                <li className="flex items-center gap-2"><div className="w-1.5 h-1.5 rounded-full bg-neutral-400"></div><span>Last synced 2 hrs ago</span></li>
              </ul>
            </div>
            <div className="flex gap-2"><button className="flex-1 bg-[#e0e0e0] border border-neutral-300 text-sm py-1.5 rounded hover:bg-neutral-300 transition-colors">Connect</button><button className="flex-1 bg-[#e0e0e0] border border-neutral-300 text-sm py-1.5 rounded hover:bg-neutral-300 transition-colors">View data</button></div>
          </div>
        </div>
      </div>
      {/* Add More Coverage Panel */}
      <div>
        <h2 className="text-lg font-semibold text-neutral-700 mb-4">Add more coverage</h2>
        <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
          <div className="bg-[#eeeeee] border border-neutral-300 rounded-md p-4 flex flex-col gap-4">
            <div className="flex items-center gap-2"><Box className="w-5 h-5 text-neutral-500" /><span className="font-semibold text-neutral-700">SAP</span></div>
            <div className="flex items-center gap-2 text-sm text-neutral-700"><Check className="w-4 h-4" /> Connected</div>
            <div className="bg-[#e0e0e0] text-sm py-1 px-3 rounded w-fit flex items-center gap-2"><div className="w-2 h-2 bg-neutral-400 rounded-sm"></div> Object 1</div>
            <button className="w-full mt-auto bg-[#e0e0e0] border border-neutral-300 text-sm py-1.5 rounded flex items-center justify-center gap-2 hover:bg-neutral-300 transition-colors"><Check className="w-4 h-4" /> View data</button>
          </div>
          <div className="bg-[#eeeeee] border border-neutral-300 rounded-md p-4 flex flex-col gap-4">
            <div className="flex items-center gap-2"><Github className="w-5 h-5 text-neutral-500" /><span className="font-semibold text-neutral-700">GitHub</span></div>
            <div className="flex items-center gap-2 text-sm text-neutral-700"><Check className="w-4 h-4" /> Connected</div>
            <div className="bg-[#e0e0e0] text-sm py-1 px-3 rounded w-fit flex items-center gap-2"><div className="w-2 h-2 bg-neutral-400 rounded-sm"></div> Object 2</div>
            <button className="w-full mt-auto bg-[#e0e0e0] border border-neutral-300 text-sm py-1.5 rounded flex items-center justify-center gap-2 hover:bg-neutral-300 transition-colors"><Check className="w-4 h-4" /> View data</button>
          </div>
          <div className="bg-[#eeeeee] border border-neutral-300 rounded-md p-4 flex flex-col gap-4">
            <div className="flex items-center gap-2"><MessageSquare className="w-5 h-5 text-neutral-500" /><span className="font-semibold text-neutral-700">Slack</span></div>
            <div className="flex items-center gap-2 text-sm text-neutral-700"><Check className="w-4 h-4" /> Connected</div>
            <div className="bg-[#e0e0e0] text-sm py-1 px-3 rounded w-fit flex items-center gap-2"><div className="w-2 h-2 bg-neutral-400 rounded-sm"></div> Object 2</div>
            <button className="w-full mt-auto bg-[#e0e0e0] border border-neutral-300 text-sm py-1.5 rounded hover:bg-neutral-300 transition-colors">View data</button>
          </div>
          {/* This empty div is for the Databricks card, which was not in your final code. I've left a placeholder */}
        </div>
      </div>
    </div>
  );
}

export default IntegrationPanels;