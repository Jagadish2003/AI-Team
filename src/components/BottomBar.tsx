import { Check, ChevronRight } from 'lucide-react';

function BottomBar() {
  return (
    <div className="fixed bottom-0 left-0 right-0 w-full bg-[#eeeeee] border-t border-neutral-300 px-8 py-4 flex flex-col md:flex-row items-center justify-between z-50 gap-4">
      {/* Left Side: Progress & Upload */}
      <div className="flex items-center gap-4 w-full md:w-auto">
        <div className="flex flex-col gap-2">
          <div className="flex items-center gap-0 w-[240px] relative mt-2">
            <div className="absolute top-1/2 left-0 w-full h-1 bg-neutral-300 rounded -translate-y-1/2"></div>
            <div className="absolute top-1/2 left-0 w-1/2 h-1 bg-neutral-400 rounded -translate-y-1/2"></div>
            <div className="flex flex-col items-start relative z-10 w-1/3"><div className="w-4 h-4 rounded-full bg-neutral-400 shadow translate-x-1"></div></div>
            <div className="flex flex-col items-center relative z-10 w-1/3"><div className="w-4 h-4 rounded-full bg-neutral-400 shadow"></div></div>
            <div className="flex flex-col items-end relative z-10 w-1/3"><div className="w-4 h-4 rounded-full bg-neutral-300 border border-neutral-400 -translate-x-1"></div></div>
          </div>
          <div className="flex text-[10px] font-semibold text-neutral-500 justify-between w-[240px] px-1 tracking-wider"><span>LOW</span><span>MEDIUM</span><span>HIGH</span></div>
        </div>
        <button className="bg-[#dcdcdc] border border-neutral-300 text-sm py-1.5 px-4 rounded w-fit hover:bg-neutral-300 transition-colors ml-4 whitespace-nowrap">Upload Files Instead</button>
      </div>

      {/* Middle Side: Connection Badges */}
      <div className="flex flex-col items-center gap-2">
        <div className="text-sm text-neutral-700 font-medium">Connect:1 of 3 recommended <span className="text-neutral-400 mx-2">|</span> ReCx</div>
        <div className="flex items-center gap-3 text-xs bg-[#e0e0e0] border border-neutral-300 rounded px-3 py-1.5">
          <span className="flex items-center gap-1 text-neutral-600"><span className="w-3 h-3 bg-neutral-300 rounded-sm flex items-center justify-center"><Check className="w-2.5 h-2.5" /></span> Jira</span>
          <span className="flex items-center gap-1 text-neutral-500"><span className="w-2 h-2 bg-neutral-400 rounded-sm"></span> Connected</span>
          <span className="flex items-center gap-1 text-neutral-600"><span className="w-3 h-3 bg-neutral-300 rounded-sm flex items-center justify-center"><Check className="w-2.5 h-2.5" /></span> Microsoft 365</span>
          <span className="flex items-center gap-1 text-neutral-500"><span className="w-2 h-2 bg-neutral-400 rounded-sm"></span> Not Connected</span>
        </div>
      </div>

      {/* Right Side: CTA */}
      <div className="flex flex-col items-end gap-2">
        <div className="text-sm text-neutral-700 font-medium">CONFIDENCE: Medium</div>
        <button className="bg-[#9b9b9b] text-white text-lg font-medium py-2.5 px-8 rounded shadow flex items-center gap-2 hover:bg-neutral-500 transition-colors whitespace-nowrap">Start Discovery Run <ChevronRight className="w-5 h-5" /></button>
        <div className="text-[11px] text-neutral-600 flex items-center gap-1 cursor-pointer hover:underline">Connect 1 more source to reach HIGH <ChevronRight className="w-3 h-3" /></div>
      </div>
    </div>
  );
}

export default BottomBar;