import { Connector } from "../../types/connector";
import { useConnectors } from "../../context/ConnectorContext";

export default function ConnectorTile({ connector }: { connector: Connector }) {

  const { connectConnector, selectConnector } = useConnectors();

  return (
    <div
      className="border rounded-lg p-3 cursor-pointer"
      onClick={() => selectConnector(connector.id)}
    >
      <h3 className="font-medium">{connector.name}</h3>

      <p className="text-xs text-gray-500">{connector.category}</p>

      <button
        className="mt-2 text-blue-600 text-sm"
        onClick={() => connectConnector(connector.id)}
      >
        Connect
      </button>
    </div>
  );
}