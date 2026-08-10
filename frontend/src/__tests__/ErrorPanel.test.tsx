import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import ErrorPanel from '../components/common/ErrorPanel';

describe('ErrorPanel', () => {
  it('uses theme tokens for the panel and copy so it stays readable in light theme', () => {
    const { container } = render(<ErrorPanel message="Could not load proposals." />);

    const alert = screen.getByRole('alert');
    expect(alert.className).toContain('bg-panel');
    expect(alert.className).toContain('border-red-500/30');
    expect(container.querySelector('.text-text')).toHaveTextContent('Something went wrong');
    expect(screen.getByText('Could not load proposals.').className).toContain('text-muted');
  });

  it('shows Retry and invokes the callback when provided', () => {
    const onRetry = vi.fn();
    render(<ErrorPanel message="Could not load proposals." onRetry={onRetry} />);

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });
});
