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
      <div
        role="alert"
        className="w-full max-w-lg space-y-5 rounded-2xl border border-red-500/30 bg-panel px-6 py-8 text-center shadow-xl shadow-black/10 transition-all duration-300 dark:border-red-400/20 dark:bg-gradient-to-br dark:from-red-500/10 dark:to-red-500/5 dark:shadow-red-900/20 sm:px-8"
      >
        <div className="flex justify-center">
          <div className="rounded-full bg-red-500/10 p-3 text-red-500 dark:bg-red-500/20 dark:text-red-400">
            <AlertCircle className="h-6 w-7" />
          </div>
        </div>
        <h2 className="text-xl font-semibold text-text">{title}</h2>
        <p className="whitespace-pre-wrap text-sm leading-relaxed text-muted">
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
