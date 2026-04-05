import React from "react";

export default function LoadingPanel({
  title = "Loading...",
  subtitle = "Fetching data from the API.",
}: {
  title?: string;
  subtitle?: string;
}) {
  return (
    <div className="flex items-center justify-center h-[85vh]">   
      <div
        className="w-full max-w-lg rounded-2xl 
        bg-white/5 backdrop-blur-xl 
        border border-white/10 
        shadow-xl shadow-black/20 
        px-10 py-10 text-center space-y-6">
        <div className="flex justify-center">
          <div className="relative">
            <div className="h-14 w-14 rounded-full border-2 border-white/10" />
            <div className="absolute inset-0 h-14 w-14 rounded-full border-2 border-transparent border-t-cyan-300 border-r-blue-500 animate-spin" />
            <div className="absolute inset-0 rounded-full blur-md bg-cyan-400/20 animate-pulse" />
          </div>
        </div>
        <p className="text-lg font-semibold text-white tracking-wide">
          {title}
        </p>
        <p className="text-sm text-gray-400">
          {subtitle}
        </p>
      </div>
    </div>
  );
}