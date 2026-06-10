// @vitest-environment jsdom
import React from 'react';
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import CorroborationBadge from '../components/analyst_review/CorroborationBadge';

describe('CorroborationBadge — ENT-2 (T5)', () => {
  it('renders nothing when there are no corroboration sources (single source)', () => {
    const { container } = render(<CorroborationBadge sources={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it('renders nothing when sources is undefined', () => {
    const { container } = render(<CorroborationBadge />);
    expect(container.firstChild).toBeNull();
  });

  it('renders a green corroborated pill when sources are present', () => {
    render(<CorroborationBadge sources={['ServiceNow']} label="Corroborated by ServiceNow incidents" />);
    const badge = screen.getByTestId('corroboration-badge');
    expect(badge).toHaveAttribute('data-variant', 'corroborated');
    expect(badge).toHaveClass('text-emerald-300');
    expect(badge.textContent).toContain('Corroborated by 1 system');
  });

  it('pluralises the system count', () => {
    render(<CorroborationBadge sources={['ServiceNow', 'Jira']} />);
    const badge = screen.getByTestId('corroboration-badge');
    expect(badge.textContent).toContain('Corroborated by 2 systems');
  });

  it('renders a gold triple-corroboration pill', () => {
    render(
      <CorroborationBadge
        sources={['ServiceNow', 'Jira']}
        tripleCorroboration
        label="Triple corroboration: Salesforce + ServiceNow + Jira"
      />,
    );
    const badge = screen.getByTestId('corroboration-badge');
    expect(badge).toHaveAttribute('data-variant', 'triple');
    expect(badge).toHaveClass('text-amber-200');
    expect(badge.textContent).toContain('Triple corroboration');
  });

  it('renders a muted pill for Slack supporting only', () => {
    render(<CorroborationBadge sources={['Slack (supporting only)']} />);
    const badge = screen.getByTestId('corroboration-badge');
    expect(badge).toHaveAttribute('data-variant', 'supporting');
    expect(badge).toHaveClass('text-muted');
    expect(badge.textContent).toContain('Supporting: Slack');
  });

  it('exposes the source list in the tooltip', () => {
    render(<CorroborationBadge sources={['ServiceNow', 'Jira']} label="Corroborated by ServiceNow incidents" />);
    const badge = screen.getByTestId('corroboration-badge');
    expect(badge.getAttribute('title')).toContain('ServiceNow');
    expect(badge.getAttribute('title')).toContain('Jira');
  });

  it('triple takes precedence over the green corroborated pill', () => {
    render(<CorroborationBadge sources={['ServiceNow', 'Jira']} tripleCorroboration />);
    const badge = screen.getByTestId('corroboration-badge');
    expect(badge).toHaveAttribute('data-variant', 'triple');
  });
});
