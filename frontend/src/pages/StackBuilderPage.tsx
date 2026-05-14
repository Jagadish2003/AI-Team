import React from 'react';
import { useNavigate } from 'react-router-dom';
import TopNav from '../components/common/TopNav';
import { useSetupState } from '../components/stack_builder';

import DiscoveryFocusPage from './DiscoveryFocusPage';
import YourSystemsPage from './YourSystemsPage';
import SourceWeightingScreen from '../pages/SourceWeightingPage'; 
import DiscoveryPlanScreen from '../pages/DiscoveryPlanPage';

export default function StackBuilderPage() {
  const setupState = useSetupState();
  const { state } = setupState;
  const navigate = useNavigate();

  const handleLaunchDiscovery = () => {
    // In Sprint 8/9 this will trigger the actual backend /launch API
    // For now, navigate to the discovery run page
    navigate('/discovery-run'); 
  };

  return (
    <div className="min-h-screen text-text bg-bg">
      <TopNav />
      <div className="w-full">
        {state.currentStep === 1 && (
          <DiscoveryFocusPage setupState={setupState} />
        )}
        {state.currentStep === 2 && (
          <YourSystemsPage setupState={setupState} />
        )}
        
        {/* --- ADDED FOR TASKS 10 & 11 --- */}
        {state.currentStep === 3 && (
          <SourceWeightingScreen setupState={setupState} />
        )}
        {state.currentStep === 4 && (
          <DiscoveryPlanScreen 
            setupState={setupState} 
            onLaunch={handleLaunchDiscovery} 
          />
        )}
      </div>
    </div>
  );
}