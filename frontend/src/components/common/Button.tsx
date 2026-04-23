import React from 'react';

type Variant = 'primary' | 'secondary' | 'ghost' | 'danger';

export default function Button({
  children,
  onClick,
  disabled,
  variant = 'primary',
  className = '',
  title
}: {
  children: React.ReactNode;
  onClick?: React.MouseEventHandler<HTMLButtonElement>;
  disabled?: boolean;
  variant?: Variant;
  className?: string;
  title?: string;
}) {
  const base = 'inline-flex items-center justify-center whitespace-nowrap rounded-md px-3 py-2 text-sm font-medium transition';
  const variants: Record<Variant, string> = {
  primary: 'bg-accent text-bg hover:opacity-90 disabled:opacity-40',
  secondary: 'text-text border border-border disabled:opacity-40', 
  ghost: 'text-text hover:bg-panel2 disabled:opacity-40',
  danger: 'bg-red-500/15 text-red-300 border border-red-500/30 hover:bg-red-500/25 disabled:opacity-40'
};
  return (
    <button
      title={title}
      className={`${base} ${variants[variant] ?? ''} ${className}`}
      onClick={disabled ? undefined : onClick}
      disabled={disabled}
    >
      {children}
    </button>
  );
}