import { Connector } from "../../types/connector";
import { useConnectors } from "../../context/ConnectorContext";

export default function HeroConnectorCard({ connector }: { connector: Connector }) {

  const { connectConnector, selectConnector } = useConnectors();

  return (
    <div
      className="border rounded-xl p-4 shadow hover:bg-gray-50 cursor-pointer"
      onClick={() => selectConnector(connector.id)}
    >

      <h3 className="font-semibold">{connector.name}</h3>

      <p className="text-sm text-gray-500">{connector.category}</p>

      <div className="mt-3 space-y-1 text-sm">
        {connector.metrics.map(m => (
          <div key={m.label}>
            {m.label}: {m.value}
          </div>
        ))}
      </div>

      <button
        className="mt-3 bg-blue-600 text-white px-3 py-1 rounded"
        onClick={() => connectConnector(connector.id)}
      >
        Connect
      </button>

    </div>
  );
}