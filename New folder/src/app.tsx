import IntegrationHubPage from "./pages/IntegrationHubPage";
import { ConnectorProvider } from "./context/ConnectorContext";

function App() {
  return (
    <ConnectorProvider>
      <IntegrationHubPage />
    </ConnectorProvider>
  );
}

export default App;