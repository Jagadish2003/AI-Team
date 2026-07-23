import React from 'react';

type Variant = 'primary' | 'secondary' | 'tertiary' | 'ghost' | 'danger';

export default function Button({
  children,
  onClick,
  disabled,
  variant = 'primary',
  className = '',
  title,
  ariaLabel,
  type = 'button'
}: {
  children: React.ReactNode;
  onClick?: React.MouseEventHandler<HTMLButtonElement>;
  disabled?: boolean;
  variant?: Variant;
  className?: string;
  title?: string;
  ariaLabel?: string;
  // Defaults to 'button' so an existing Button placed inside a <form> never
  // accidentally submits it; pass 'submit' explicitly for a form's submit button.
  type?: 'button' | 'submit' | 'reset';
}) {
  const base = 'inline-flex min-h-9 items-center justify-center whitespace-nowrap rounded-md px-3 py-2 text-sm font-medium transition-[border-color,background-color,box-shadow,color,opacity] focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 disabled:cursor-not-allowed disabled:opacity-40';
  const variants: Record<Variant, string> = {
  primary: 'border border-accent bg-accent text-textwhite shadow-sm hover:bg-accent/90',
  secondary: 'border border-accent/45 bg-buttonbg text-accent shadow-sm hover:bg-accent/10',
  tertiary: 'border border-accent/20 bg-accent/5 text-accent hover:border-accent/45 hover:bg-accent/10',
  ghost: 'border border-accent/20 bg-accent/5 text-accent hover:border-accent/45 hover:bg-accent/10',
  danger: 'border border-red-500/30 bg-red-500/15 text-red-300 hover:bg-red-500/25'
};
  return (
    <button
      type={type}
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
