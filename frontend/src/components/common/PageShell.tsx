import React from "react";
import TopNav from "./TopNav";

type PageShellProps = {
  title?: React.ReactNode;
  description?: React.ReactNode;
  actions?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
  contentClassName?: string;
  headerClassName?: string;
};

export function PageHeader({
  title,
  description,
  actions,
  className = "",
}: Pick<PageShellProps, "title" | "description" | "actions"> & {
  className?: string;
}) {
  if (!title && !description && !actions) return null;

  return (
    <div
      className={`mb-5 flex flex-col gap-3 md:flex-row md:items-start md:justify-between ${className}`}
    >
      <div className="min-w-0">
        {title && (
          <h1 className="truncate text-2xl font-semibold leading-tight text-text" data-testid="page-title">
            {title}
          </h1>
        )}
        {description && (
          <div className="mt-1 max-w-3xl text-sm leading-relaxed text-muted">
            {description}
          </div>
        )}
      </div>
      {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

export default function PageShell({
  title,
  description,
  actions,
  children,
  className = "",
  contentClassName = "",
  headerClassName = "",
}: PageShellProps) {
  return (
    <div className={`min-h-screen text-text ${className}`}>
      <TopNav />
      <main className={`w-full px-4 py-5 pb-10 sm:px-6 lg:px-8 ${contentClassName}`}>
        <PageHeader
          title={title}
          description={description}
          actions={actions}
          className={headerClassName}
        />
        {children}
      </main>
    </div>
  );
}
