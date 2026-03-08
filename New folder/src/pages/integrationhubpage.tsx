import HeroConnectorSection from "../components/integrations/HeroConnectorSection";
import ConnectorGridSection from "../components/integrations/ConnectorGridSection";
import RightPanel from "../components/integrations/RightPanel";
import DiscoveryStartBar from "../components/integrations/DiscoveryStartBar";

export default function IntegrationHubPage() {
  return (
    <div className="flex h-screen flex-col">

      <div className="flex flex-1">

        <div className="w-2/3 p-6 space-y-8">
          <HeroConnectorSection />
          <ConnectorGridSection />
        </div>

        <div className="w-1/3 border-l">
          <RightPanel />
        </div>

      </div>

      <DiscoveryStartBar />
    </div>
  );
}