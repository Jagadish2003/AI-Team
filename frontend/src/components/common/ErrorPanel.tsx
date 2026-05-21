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
    <div className="flex min-h-[min(620px,75vh)] items-center justify-center px-4">
      
      <div className="w-full max-w-lg rounded-2xl 
        border border-red-400/20 
        bg-gradient-to-br from-red-500/10 to-red-500/5 
        backdrop-blur-xl 
        shadow-xl shadow-red-900/20 
        px-6 py-8 text-center space-y-5 sm:px-8
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
              variant="tertiary"
              onClick={onRetry}
              className="rounded-lg px-6 py-2">
              Retry
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}
