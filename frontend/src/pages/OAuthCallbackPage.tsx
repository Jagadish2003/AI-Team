import React, { useEffect } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useConnectorContext } from "../context/ConnectorContext";
import LoadingPanel from "../components/common/LoadingPanel";

export default function OAuthCallbackPage() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const { refetch } = useConnectorContext();

  useEffect(() => {
    const status = searchParams.get("status");
    const connectorId = searchParams.get("connected");
    const errorCode = searchParams.get("code");

    if (status === "success" && connectorId) {
      refetch();
      navigate("/integration-hub", {
        state: { justConnected: connectorId },
        replace: true,
      });
    } else {
      navigate("/integration-hub", {
        state: { oauthError: errorCode ?? "unknown" },
        replace: true,
      });
    }
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  return <LoadingPanel title="Completing connection..." />;
}
