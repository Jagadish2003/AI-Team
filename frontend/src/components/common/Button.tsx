import React from 'react';

type Variant = 'primary' | 'secondary' | 'ghost' | 'danger';

export default function Button({
  children,
  onClick,
  disabled,
  variant = 'primary',
  className = '',
  title,
  ariaLabel
}: {
  children: React.ReactNode;
  onClick?: React.MouseEventHandler<HTMLButtonElement>;
  disabled?: boolean;
  variant?: Variant;
  className?: string;
  title?: string;
  ariaLabel?: string;
}) {
  const base = 'inline-flex min-h-9 items-center justify-center whitespace-nowrap rounded-md px-3 py-2 text-sm font-medium transition focus:outline-none focus:ring-2 focus:ring-accent/50 disabled:cursor-not-allowed disabled:opacity-40';
  const variants: Record<Variant, string> = {
  primary: 'bg-accent text-white hover:opacity-90',
  secondary: 'border border-border bg-buttonbg text-text hover:bg-panel2',
  ghost: 'text-text hover:bg-panel2',
  danger: 'border border-red-500/30 bg-red-500/15 text-red-300 hover:bg-red-500/25'
};
  return (
    <button
      title={title}
      aria-label={ariaLabel}
      className={`${base} ${variants[variant] ?? ''} ${className}`}
      onClick={disabled ? undefined : onClick}
      disabled={disabled}
    >
      {children}
    </button>
  );
}
