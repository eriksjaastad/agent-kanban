import { render, screen } from '@testing-library/react';
import { ProjectsProvider } from '../hooks/ProjectsProvider';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AgenticDashboard } from './AgenticDashboard';

vi.mock('../api', async (importOriginal) => ({
  // Spread the real module so isAbortError is the REAL implementation.
  ...(await importOriginal<typeof import('../api')>()),
  fetchProjects: vi.fn(),
  fetchMarkers: vi.fn(),
  fetchAgenticSummary: vi.fn(),
  fetchToolStats: vi.fn(),
  fetchBashStats: vi.fn(),
  createMarker: vi.fn(),
  updateMarker: vi.fn(),
  deleteMarker: vi.fn(),
}));

import {
  fetchAgenticSummary,
  fetchBashStats,
  fetchMarkers,
  fetchProjects,
  fetchToolStats,
} from '../api';

const PROJECTS = [
  { id: 'project-tracker', name: 'Project Tracker', path: '/tmp/project-tracker', status: 'active' },
];

const SUMMARY = {
  summary: {
    review_bounces: 0,
    review_promotions: 0,
    review_entries: 0,
    bounce_rate: 0,
    promotion_rate: 0,
  },
  series: [],
  markers: [],
  markers_error: null,
  date_range: { start: '2026-03-01', end: '2026-03-31' },
  project_id: null,
};

const EMPTY_TOOL_STATS = { by_date: [], by_project: [], by_model: [], projects: [], models: [] };
const EMPTY_BASH_STATS = { by_date: [], by_project: [], projects: [] };

describe('AgenticDashboard — load failures', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(fetchProjects).mockResolvedValue(PROJECTS);
    vi.mocked(fetchMarkers).mockResolvedValue([]);
    vi.mocked(fetchAgenticSummary).mockResolvedValue(SUMMARY);
    vi.mocked(fetchToolStats).mockResolvedValue(EMPTY_TOOL_STATS);
    vi.mocked(fetchBashStats).mockResolvedValue(EMPTY_BASH_STATS);
  });

  it('shows the markers 503 detail and hides the empty state and add form', async () => {
    vi.mocked(fetchMarkers).mockRejectedValue(
      new Error('invalid JSON in /data/agentic_markers.json: Expecting value: line 1 column 1 (char 0)')
    );

    render(<AgenticDashboard />, { wrapper: ProjectsProvider });

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'invalid JSON in /data/agentic_markers.json'
    );
    expect(screen.queryByText(/No markers yet/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Add Marker' })).not.toBeInTheDocument();
    expect(screen.getByText(/Marker editing is disabled/)).toBeInTheDocument();
  });

  it('still renders markers when projects fail to load', async () => {
    vi.mocked(fetchProjects).mockRejectedValue(new Error('projects service down'));
    vi.mocked(fetchMarkers).mockResolvedValue([
      { id: 'm1', date: '2026-03-10', label: 'Workflow change', source: 'manual' },
    ]);

    render(<AgenticDashboard />, { wrapper: ProjectsProvider });

    expect(await screen.findByText('Workflow change')).toBeInTheDocument();
    expect(screen.queryByText(/No markers yet/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add Marker' })).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('surfaces a summary markers_error the same way as a markers load failure', async () => {
    vi.mocked(fetchAgenticSummary).mockResolvedValue({
      ...SUMMARY,
      markers_error: 'invalid JSON in /data/agentic_markers.json: Expecting value: line 1 column 1 (char 0)',
    });

    render(<AgenticDashboard />, { wrapper: ProjectsProvider });

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'invalid JSON in /data/agentic_markers.json'
    );
    expect(screen.queryByRole('button', { name: 'Add Marker' })).not.toBeInTheDocument();
    expect(screen.getByText(/Marker editing is disabled/)).toBeInTheDocument();
  });

  it('shows why the brain.db charts are unavailable instead of an empty chart', async () => {
    vi.mocked(fetchToolStats).mockRejectedValue(new Error('Cannot read /p/brain.db: no such table: tool_invocations'));
    vi.mocked(fetchBashStats).mockRejectedValue(new Error('Cannot read /p/brain.db: no such table: bash_calls'));

    render(<AgenticDashboard />, { wrapper: ProjectsProvider });

    expect(await screen.findByText(/no such table: tool_invocations/)).toBeInTheDocument();
    expect(await screen.findByText(/no such table: bash_calls/)).toBeInTheDocument();
    expect(screen.queryByText(/No WebSearch data yet/)).not.toBeInTheDocument();
    expect(screen.queryByText(/No bash call data yet/)).not.toBeInTheDocument();
  });
});
