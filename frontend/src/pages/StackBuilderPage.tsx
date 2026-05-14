import React from 'react';
// 1. Add the TopNav import exactly as seen in your screenshot (Line 12 of SourceIntelligencePage)
import TopNav from '../components/common/TopNav';
import { useSetupState } from '../components/stack_builder';

import DiscoveryFocusPage from './DiscoveryFocusPage';
import YourSystemsPage from './YourSystemsPage';

export default function StackBuilderPage() {
  const setupState = useSetupState();
  const { state } = setupState;

  return (
    // 2. Wrap everything in a div with min-h-screen as seen in your screenshot (Line 374)
    <div className="min-h-screen text-text bg-bg">
      {/* 3. Place the TopNav at the very top */}
      <TopNav />

      {/* 4. The main content follows below */}
      <div className="w-full">
        {state.currentStep === 1 && (
          <DiscoveryFocusPage setupState={setupState} />
        )}
        {state.currentStep === 2 && (
          <YourSystemsPage setupState={setupState} />
        )}
      </div>
    </div>
  );
}