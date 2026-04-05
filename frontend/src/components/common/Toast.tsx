import React, { createContext, useCallback, useContext, useMemo, useState } from "react";
import { CheckCircle, AlertCircle, Info } from "lucide-react";

type Toast = { id: string; message: string; type?: "success" | "error" | "info" };

const ToastCtx = createContext<{ push: (message: string, type?: Toast["type"]) => void } | null>(null);

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const push = useCallback((message: string, type: Toast["type"] = "info") => {
    const id = crypto.randomUUID();
    setToasts((prev) => [...prev, { id, message, type }]);

    // ⏳ Increased duration (4 sec)
    setTimeout(() => {
      setToasts((prev) => prev.filter((x) => x.id !== id));
    }, 4000);
  }, []);

  const value = useMemo(() => ({ push }), [push]);

  return (
    <ToastCtx.Provider value={value}>
      {children}

      {/* Toast Container */}
      <div className="fixed bottom-16 right-6 z-50 flex flex-col gap-3">
        {toasts.map((t) => {
          const isSuccess = t.type === "success";
          const isError = t.type === "error";

          return (
            <div
              key={t.id}
              className={`
                flex items-start gap-3 w-80 rounded-xl px-4 py-3
                backdrop-blur-lg border shadow-lg
                animate-[slideInRight_0.4s_ease-out]
                ${
                  isSuccess
                    ? "bg-green-500/10 border-green-400/30 text-green-200"
                    : isError
                    ? "bg-red-500/10 border-red-400/30 text-red-200"
                    : "bg-blue-500/10 border-blue-400/30 text-blue-200"
                }
              `}
            >
              {/* Icon */}
              <div className="mt-0.5">
                {isSuccess ? (
                  <CheckCircle size={18} />
                ) : isError ? (
                  <AlertCircle size={18} />
                ) : (
                  <Info size={18} />
                )}
              </div>

              {/* Message */}
              <div className="text-sm leading-relaxed">
                {t.message}
              </div>
            </div>
          );
        })}
      </div>
    </ToastCtx.Provider>
  );
}

export function useToast() {
  const ctx = useContext(ToastCtx);
  if (!ctx) throw new Error("useToast must be used inside ToastProvider");
  return ctx;
}