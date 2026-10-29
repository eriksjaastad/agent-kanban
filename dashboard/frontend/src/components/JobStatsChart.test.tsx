import { render, screen } from '@testing-library/react';
import { cloneElement, type ReactElement } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { JobStatsChart } from './JobStatsChart';

// jsdom has no layout, so ResponsiveContainer would draw nothing; give the chart a fixed size.
vi.mock('recharts', async importOriginal => {
  const recharts = await importOriginal<typeof import('recharts')>();
  return {
    ...recharts,
    ResponsiveContainer: ({ children }: { children: ReactElement<{ width?: number; height?: number }> }) =>
      cloneElement(children, { width: 800, height: 300 }),
  };
});

describe('JobStatsChart', () => {
  it('renders chart with job and submission data', () => {
    const stats = {
      jobs_per_day: [
        { date: '2026-09-20', count: 5 },
        { date: '2026-09-21', count: 3 },
      ],
      submissions_per_day: [
        { date: '2026-09-20', count: 1 },
        { date: '2026-09-22', count: 2 },
      ],
    };

    render(<JobStatsChart stats={stats} />);

    expect(screen.getByText('Job Activity')).toBeInTheDocument();
    expect(screen.getByText('Daily job discoveries and submissions by first-seen/submission date')).toBeInTheDocument();
  });

  it('puts submissions on their own right-hand axis', () => {
    const stats = {
      jobs_per_day: [{ date: '2026-09-20', count: 120 }],
      submissions_per_day: [{ date: '2026-09-20', count: 2 }],
    };

    const { container } = render(<JobStatsChart stats={stats} />);

    expect(container.querySelectorAll('.recharts-yAxis')).toHaveLength(2);
  });

  it('shows empty state when no data is available', () => {
    const stats = {
      jobs_per_day: [],
      submissions_per_day: [],
    };

    render(<JobStatsChart stats={stats} />);

    expect(screen.getByText('No job activity data yet')).toBeInTheDocument();
  });

  it('merges dates from both series correctly', () => {
    const stats = {
      jobs_per_day: [{ date: '2026-09-20', count: 5 }],
      submissions_per_day: [{ date: '2026-09-21', count: 2 }],
    };

    const { container } = render(<JobStatsChart stats={stats} />);

    const plotElement = container.querySelector('.job-stats-plot');
    expect(plotElement).toBeInTheDocument();
  });

  it('handles zero counts correctly', () => {
    const stats = {
      jobs_per_day: [{ date: '2026-09-20', count: 0 }],
      submissions_per_day: [{ date: '2026-09-21', count: 0 }],
    };

    const { container } = render(<JobStatsChart stats={stats} />);

    const plotElement = container.querySelector('.job-stats-plot');
    expect(plotElement).toBeInTheDocument();
  });
});
