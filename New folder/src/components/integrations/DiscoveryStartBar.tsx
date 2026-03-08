import { useConnectors } from "../../context/ConnectorContext";

export default function DiscoveryStartBar() {

  const { confidence } = useConnectors();

  return (
    <div className="border-t p-4 flex justify-between items-center">

      <div>
        Confidence: <b>{confidence}</b>
      </div>

      <button className="bg-green-600 text-white px-5 py-2 rounded">
        Start Discovery Run
      </button>

    </div>
  );
}