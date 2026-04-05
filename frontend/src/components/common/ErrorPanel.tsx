import React from "react";
import { AlertCircle } from "lucide-react";
import Button from "./Button";

export default function ErrorPanel({
  message,
  onRetry,
  title = "Something went wrong",
}: {
  message: string;
  onRetry?: () => void;
  title?: string;
}) {
  return (
    <div className="flex items-center justify-center h-[85vh]">
      
      <div className="w-full max-w-lg rounded-2xl 
        border border-red-400/20 
        bg-gradient-to-br from-red-500/10 to-red-500/5 
        backdrop-blur-xl 
        shadow-xl shadow-red-900/20 
        px-8 py-5 text-center space-y-5
        transition-all duration-300"
      >
        <div className="flex justify-center">
          <div className="p-3 rounded-full bg-red-500/20">
            <AlertCircle className="text-red-400 w-7 h-6" />
          </div>
        </div>
        <h2 className="text-xl font-semibold text-red-100/70">
          {title}
        </h2>
        <p className="text-sm text-red-100/80 leading-relaxed whitespace-pre-wrap">
          {message}
        </p>
        {onRetry && (
          <div className="pt-2">
            <Button
              variant="secondary"
              onClick={onRetry}
              className="px-6 py-2 rounded-lg 
                bg-red-500/60 hover:bg-red-500/70 
                text-red-100 transition-all">
              Retry
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}