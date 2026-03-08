import { useState } from 'react';
import { Activity, ChevronDown, User } from 'lucide-react';

function Header() {
  const [isMenuOpen, setIsMenuOpen] = useState(false);

  return (
    <header className="sticky top-0 z-50 flex items-center justify-between px-6 py-3 border-b border-neutral-400 bg-[#cecece]">
      <div className="flex items-center gap-2">
        <Activity className="w-6 h-6 text-neutral-600" />
        <span className="text-xl font-semibold text-neutral-700">AgentIQ</span>
      </div>
      <div className="flex items-center gap-6 text-sm text-neutral-600">
        <a href="#" className="hover:text-neutral-900">Discovery Dashboard</a>
        <span className="w-px h-4 bg-neutral-400"></span>
        <a href="#" className="hover:text-neutral-900">Opportunity Map</a>
        <span className="w-px h-4 bg-neutral-400"></span>
        <a href="#" className="hover:text-neutral-900">Reports</a>
        
        {/* ADMINISTRATOR DROPDOWN */}
        <div className="relative">
          <div 
            className="flex items-center gap-1 cursor-pointer hover:text-neutral-900 py-2 select-none"
            onClick={() => setIsMenuOpen(!isMenuOpen)}
          >
            Administrator 
            <ChevronDown className={`w-4 h-4 transition-transform duration-200 ${isMenuOpen ? 'rotate-180' : ''}`} />
          </div>

          {isMenuOpen && (
            <div className="absolute right-0 top-full mt-1 w-40 bg-[#eeeeee] border border-neutral-300 rounded-md shadow-lg z-50 overflow-hidden">
              <div className="py-1">
                <a href="#" className="block px-4 py-2 text-sm text-neutral-700 hover:bg-[#dcdcdc] hover:text-neutral-900 transition-colors">
                  Dashboards
                </a>
              </div>
            </div>
          )}
        </div>
        
        <div className="w-8 h-8 rounded-full bg-neutral-500 flex items-center justify-center text-white ml-2">
          <User className="w-5 h-5" />
        </div>
      </div>
    </header>
  );
}

export default Header;