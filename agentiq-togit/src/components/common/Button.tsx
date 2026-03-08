import React from 'react';

type Variant = 'primary' | 'secondary' | 'ghost';

export default function Button({
  children,
  onClick,
  disabled,
  variant = 'primary',
  className = '',
  title,
}: {
  children: React.ReactNode;
  onClick?: React.MouseEventHandler<HTMLButtonElement>;
  disabled?: boolean;
  variant?: Variant;
  className?: string;
  title?: string;
}) {
  const base =
    'inline-flex h-11 items-center justify-center rounded-lg border px-4 text-sm font-medium transition focus:outline-none focus:ring-2 focus:ring-accent/25 disabled:cursor-not-allowed disabled:opacity-50';
  const variants: Record<Variant, string> = {
    primary: 'border-accent bg-accent text-white hover:opacity-95',
    secondary: 'border-border bg-panel2 text-text hover:bg-[#efeff1]',
    ghost: 'border-transparent bg-transparent text-muted hover:bg-panel2 hover:text-text',
  };

  return (
    <button title={title} className={`${base} ${variants[variant]} ${className}`} onClick={onClick} disabled={disabled}>
      {children}
    </button>
  );
}
