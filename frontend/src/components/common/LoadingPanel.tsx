import React from "react";
import { Loader2 } from "lucide-react";
import { InfoPanel } from "./InfoPanel";

export default function LoadingPanel({
  title = "Loading...",
  subtitle = "Fetching data from the API.",
}: {
  title?: string;
  subtitle?: React.ReactNode;
}) {
  return (
    <InfoPanel
      icon={<Loader2 size={24} className="animate-spin" />}
      title={title}
      message={subtitle}
    />
  );
}
