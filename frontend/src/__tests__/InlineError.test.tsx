import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import InlineError from '../components/common/InlineError';

describe('InlineError', () => {
  it('renders a title, message, and an accessible alert role', () => {
    render(<InlineError title="Could not load" message="The server was unreachable." />);
    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(screen.getByText('Could not load')).toBeInTheDocument();
    expect(screen.getByText('The server was unreachable.')).toBeInTheDocument();
  });

  it('uses theme surface tokens so it reads in light and dark themes', () => {
    const { container } = render(<InlineError message="msg" />);
    const alert = container.querySelector('[role="alert"]');
    // Surface + text come from theme tokens, not a fixed light/dark colour.
    expect(alert?.className).toContain('bg-panel');
    expect(alert?.className).toContain('border-red-500/30');
    expect(screen.getByText('Something went wrong').className).toContain('text-text');
  });

  it('shows Retry and invokes the callback when provided', () => {
    const onRetry = vi.fn();
    render(<InlineError message="msg" onRetry={onRetry} />);
    const retry = screen.getByRole('button', { name: 'Retry' });
    fireEvent.click(retry);
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it('omits the Retry button when no callback is given', () => {
    render(<InlineError message="msg" />);
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });
});
