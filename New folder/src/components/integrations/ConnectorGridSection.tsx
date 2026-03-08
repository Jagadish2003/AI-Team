import { useConnectors } from "../../context/ConnectorContext";
import ConnectorTile from "./ConnectorTile";

export default function ConnectorGridSection() {

  const { connectors } = useConnectors();

  const grid = connectors.filter(c => c.tier === "standard");

  return (
    <div>
      <h2 className="text-lg font-semibold mb-4">
        Add more coverage
      </h2>

      <div className="grid grid-cols-3 gap-4">
        {grid.map(conn => (
          <ConnectorTile key={conn.id} connector={conn} />
        ))}
      </div>
    </div>
  );
}