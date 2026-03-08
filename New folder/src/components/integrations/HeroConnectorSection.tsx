import { useConnectors } from "../../context/ConnectorContext";
import HeroConnectorCard from "./HeroConnectorCard";

export default function HeroConnectorSection() {

  const { connectors } = useConnectors();

  const recommended = connectors.filter(
    c => c.tier === "recommended"
  );

  return (
    <div>
      <h2 className="text-xl font-semibold mb-4">
        Start here (fastest to value)
      </h2>

      <div className="grid grid-cols-3 gap-4">
        {recommended.map(conn => (
          <HeroConnectorCard key={conn.id} connector={conn} />
        ))}
      </div>
    </div>
  );
}