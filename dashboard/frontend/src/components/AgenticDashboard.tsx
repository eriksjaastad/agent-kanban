import { useEffect, useMemo, useState } from 'react';
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
  ReferenceLine,
} from 'recharts';
import {
  fetchAgenticSummary,
  fetchMarkers,
  createMarker,
  updateMarker,
  deleteMarker,
  fetchToolStats,
  fetchBashStats,
  isAbortError,
} from '../api';
import type { AgenticMarker, AgenticSeriesEntry, AgenticSummaryResponse } from '../types';
import { useProjects } from '../hooks/useProjects';
import { useRequest } from '../hooks/useRequest';
import { PageShell } from './PageShell';
import './AgenticDashboard.css';

type TimeRange = 'week' | 'month';
type ToolView = 'all' | 'by-project' | 'by-model';

const PROJECT_COLORS = [
  '#4f9dff', '#51cf66', '#ff6b6b', '#ffd43b', '#cc5de8',
  '#ff922b', '#20c997', '#f06595', '#74c0fc', '#a9e34b',
];

export function AgenticDashboard() {
  const [timeRange, setTimeRange] = useState<TimeRange>('month');
  const [selectedProject, setSelectedProject] = useState<string | undefined>(undefined);
  const { projects: loadedProjects } = useProjects();
  const projects = useMemo(
    () => (loadedProjects ?? []).slice().sort((a, b) => a.name.localeCompare(b.name)),
    [loadedProjects],
  );
  const [summary, setSummary] = useState<AgenticSummaryResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // WebSearch tracking
  const [toolView, setToolView] = useState<ToolView>('all');

  // Stuck score (bash error rate)
  const [bashView, setBashView] = useState<ToolView>('all');

  // Marker state
  const [markers, setMarkers] = useState<AgenticMarker[]>([]);
  const [newDate, setNewDate] = useState('');
  const [newLabel, setNewLabel] = useState('');
  const [newAgent, setNewAgent] = useState('');
  const [markerError, setMarkerError] = useState<string | null>(null);
  const [markersUnreadable, setMarkersUnreadable] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editDate, setEditDate] = useState('');
  const [editLabel, setEditLabel] = useState('');
  const [editAgent, setEditAgent] = useState('');

  useEffect(() => {
    const controller = new AbortController();
    async function load() {
      try {
        const markerData = await fetchMarkers(controller.signal);
        setMarkers(markerData);
      } catch (err) {
        if (!isAbortError(err)) {
          const reason = err instanceof Error ? err.message : 'Failed to load markers';
          setMarkerError(reason);
          setMarkersUnreadable(true);
          console.error('Failed to load markers:', err);
        }
      }
    }
    load();
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    async function loadSummary() {
      setLoading(true);
      setError(null);
      try {
        const days = timeRange === 'week' ? 7 : 30;
        const data = await fetchAgenticSummary(days, selectedProject, controller.signal);
        setSummary(data);
        if (data.markers_error) {
          setMarkerError(data.markers_error);
          setMarkersUnreadable(true);
        }
      } catch (err) {
        if (isAbortError(err)) {
          return;
        }
        setError(err instanceof Error ? err.message : 'Failed to load agentic summary');
        console.error('Failed to load agentic summary:', err);
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }
    loadSummary();
    return () => controller.abort();
  }, [timeRange, selectedProject]);

  const statsDays = timeRange === 'week' ? 7 : 30;
  const toolRequest = useRequest(signal => fetchToolStats(statsDays, undefined, signal), [statsDays]);
  const toolStats = toolRequest.data;
  const bashRequest = useRequest(signal => fetchBashStats(statsDays, signal), [statsDays]);
  const bashStats = bashRequest.data;

  const projectChartData = useMemo(() => {
    if (!toolStats) return [];
    const dateMap: Record<string, Record<string, number>> = {};
    for (const entry of toolStats.by_project) {
      if (!dateMap[entry.date]) dateMap[entry.date] = {};
      dateMap[entry.date][entry.project] = entry.count;
    }
    return toolStats.by_date.map(({ date }) => ({
      date,
      ...Object.fromEntries(toolStats.projects.map(p => [p, dateMap[date]?.[p] ?? 0])),
    }));
  }, [toolStats]);

  const modelChartData = useMemo(() => {
    if (!toolStats) return [];
    const dateMap: Record<string, Record<string, number>> = {};
    for (const entry of toolStats.by_model) {
      if (!dateMap[entry.date]) dateMap[entry.date] = {};
      dateMap[entry.date][entry.model] = entry.count;
    }
    return toolStats.by_date.map(({ date }) => ({
      date,
      ...Object.fromEntries((toolStats.models || []).map(m => [m, dateMap[date]?.[m] ?? 0])),
    }));
  }, [toolStats]);

  const bashProjectChartData = useMemo(() => {
    if (!bashStats) return [];
    const dateMap: Record<string, Record<string, number>> = {};
    for (const entry of bashStats.by_project) {
      if (!dateMap[entry.date]) dateMap[entry.date] = {};
      dateMap[entry.date][entry.project] = entry.error_rate;
    }
    return bashStats.by_date.map(({ date }) => ({
      date,
      ...Object.fromEntries(bashStats.projects.map(p => [p, dateMap[date]?.[p] ?? 0])),
    }));
  }, [bashStats]);

  const chartData = (summary?.series || []).map((entry: AgenticSeriesEntry) => ({
    date: entry.date,
    bounces: entry.review_bounces,
    promotions: entry.review_promotions,
    entries: entry.review_entries,
  }));

  const formatDate = (dateStr: string) => {
    const date = new Date(dateStr);
    return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  };

  const bounceRate = summary?.summary.bounce_rate ?? 0;
  const promotionRate = summary?.summary.promotion_rate ?? 0;

  // Marker handlers
  async function handleAddMarker() {
    setMarkerError(null);
    if (!newDate || !newLabel.trim()) {
      setMarkerError('Date and label are required.');
      return;
    }
    try {
      const created = await createMarker(newDate, newLabel.trim(), newAgent.trim() || undefined);
      setMarkers(prev => [...prev, created].sort((a, b) => a.date.localeCompare(b.date)));
      setNewDate('');
      setNewLabel('');
      setNewAgent('');
    } catch (err) {
      setMarkerError(err instanceof Error ? err.message : 'Failed to add marker');
    }
  }

  function startEdit(marker: AgenticMarker) {
    setEditingId(marker.id);
    setEditDate(marker.date);
    setEditLabel(marker.label);
    setEditAgent(marker.agent || '');
  }

  async function handleSaveEdit(id: string) {
    setMarkerError(null);
    try {
      const updated = await updateMarker(id, {
        date: editDate,
        label: editLabel.trim(),
        agent: editAgent.trim() || undefined,
      });
      setMarkers(prev =>
        prev.map(m => (m.id === id ? updated : m)).sort((a, b) => a.date.localeCompare(b.date))
      );
      setEditingId(null);
    } catch (err) {
      setMarkerError(err instanceof Error ? err.message : 'Failed to save marker');
    }
  }

  async function handleDeleteMarker(id: string) {
    setMarkerError(null);
    try {
      await deleteMarker(id);
      setMarkers(prev => prev.filter(m => m.id !== id));
    } catch (err) {
      setMarkerError(err instanceof Error ? err.message : 'Failed to delete marker');
    }
  }

  const headerActions = (
    <div className="agentic-controls">
      <div className="control-group">
        <label htmlFor="agentic-project">Project</label>
        <select
          id="agentic-project"
          value={selectedProject || ''}
          onChange={(e) => setSelectedProject(e.target.value || undefined)}
        >
          <option value="">All Projects</option>
          {projects.map((project) => (
            <option key={project.id} value={project.id}>
              {project.name}
            </option>
          ))}
        </select>
      </div>
      <div className="control-group">
        <label>Time Range</label>
        <div className="time-range-buttons">
          <button
            className={timeRange === 'week' ? 'active' : ''}
            onClick={() => setTimeRange('week')}
            type="button"
          >
            Week
          </button>
          <button
            className={timeRange === 'month' ? 'active' : ''}
            onClick={() => setTimeRange('month')}
            type="button"
          >
            Month
          </button>
        </div>
      </div>
    </div>
  );

  return (
    <PageShell
      title="Agentic Autonomy"
      subtitle="Early signal on how often agent work clears Review without bouncing."
      actions={headerActions}
      headerWidth="narrow"
      contentWidth="narrow"
    >
      <div className="agentic-dashboard">
        {loading && <div className="agentic-loading">Loading agentic metrics...</div>}
        {error && <div className="agentic-error">Error: {error}</div>}

        {!loading && !error && summary && (
          <>
            <section className="agentic-stats">
              <div className="stat-card">
                <span className="stat-label">Review Bounce Rate (proxy)</span>
                <span className="stat-value">{(bounceRate * 100).toFixed(1)}%</span>
              </div>
              <div className="stat-card">
                <span className="stat-label">Review Promotion Rate</span>
                <span className="stat-value">{(promotionRate * 100).toFixed(1)}%</span>
              </div>
              <div className="stat-card">
                <span className="stat-label">Review Bounces</span>
                <span className="stat-value">{summary.summary.review_bounces}</span>
              </div>
              <div className="stat-card">
                <span className="stat-label">Review Promotions</span>
                <span className="stat-value">{summary.summary.review_promotions}</span>
              </div>
              <div className="stat-card">
                <span className="stat-label">Review Entries</span>
                <span className="stat-value">{summary.summary.review_entries}</span>
              </div>
            </section>

            <section className="agentic-chart">
              <div className="chart-header">
                <h2>Review Flow Over Time</h2>
                <p>
                  Source: task_history (Review → In Progress = bounce, Review → Done = promotion)
                </p>
              </div>
              {chartData.length === 0 ? (
                <div className="agentic-empty">No review history for the selected range.</div>
              ) : (
                <ResponsiveContainer width="100%" height={360}>
                  <LineChart data={chartData} margin={{ top: 10, right: 30, left: 10, bottom: 10 }}>
                    <CartesianGrid strokeDasharray="3 3" />
                    <XAxis
                      dataKey="date"
                      tickFormatter={formatDate}
                      angle={-40}
                      textAnchor="end"
                      height={70}
                    />
                    <YAxis />
                    <Tooltip
                      labelFormatter={(value) => `Date: ${formatDate(value as string)}`}
                    />
                    <Legend />
                    {markers.map((marker) => (
                      <ReferenceLine
                        key={marker.id}
                        x={marker.date}
                        stroke="#ffd43b"
                        strokeDasharray="4 4"
                        label={{
                          value: marker.label,
                          position: 'insideTopRight',
                          fill: '#ffd43b',
                          fontSize: 12,
                        }}
                      />
                    ))}
                    <Line
                      type="monotone"
                      dataKey="entries"
                      stroke="#4f9dff"
                      strokeWidth={2}
                      dot={{ r: 3 }}
                      name="Review Entries"
                    />
                    <Line
                      type="monotone"
                      dataKey="promotions"
                      stroke="#51cf66"
                      strokeWidth={2}
                      dot={{ r: 3 }}
                      name="Review Promotions"
                    />
                    <Line
                      type="monotone"
                      dataKey="bounces"
                      stroke="#ff6b6b"
                      strokeWidth={2}
                      dot={{ r: 3 }}
                      name="Review Bounces"
                    />
                  </LineChart>
                </ResponsiveContainer>
              )}
            </section>
          </>
        )}

        {/* WebSearch frequency chart */}
        <section className="agentic-chart">
          <div className="chart-header">
            <h2>WebSearch Calls Over Time</h2>
            <div className="time-range-buttons">
              <button
                className={toolView === 'all' ? 'active' : ''}
                onClick={() => setToolView('all')}
                type="button"
              >
                All Projects
              </button>
              <button
                className={toolView === 'by-project' ? 'active' : ''}
                onClick={() => setToolView('by-project')}
                type="button"
              >
                By Project
              </button>
              <button
                className={toolView === 'by-model' ? 'active' : ''}
                onClick={() => setToolView('by-model')}
                type="button"
              >
                By Model
              </button>
            </div>
          </div>
          {toolRequest.loading ? (
            <div className="agentic-loading">Loading search data...</div>
          ) : toolRequest.error ? (
            <div className="agentic-error" role="alert">Error: {toolRequest.error.message}</div>
          ) : !toolStats || toolStats.by_date.every(d => d.total === 0) ? (
            <div className="agentic-empty">No WebSearch data yet — hook must fire at least once.</div>
          ) : toolView === 'all' ? (
            <ResponsiveContainer width="100%" height={300}>
              <LineChart data={toolStats.by_date} margin={{ top: 10, right: 30, left: 10, bottom: 10 }}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="date" tickFormatter={formatDate} angle={-40} textAnchor="end" height={70} />
                <YAxis allowDecimals={false} />
                <Tooltip labelFormatter={(v) => `Date: ${formatDate(v as string)}`} />
                <Legend />
                <Line
                  type="monotone"
                  dataKey="total"
                  stroke="#4f9dff"
                  strokeWidth={2}
                  dot={{ r: 3 }}
                  name="WebSearch Calls"
                />
              </LineChart>
            </ResponsiveContainer>
          ) : toolView === 'by-project' ? (
            <ResponsiveContainer width="100%" height={300}>
              <LineChart data={projectChartData} margin={{ top: 10, right: 30, left: 10, bottom: 10 }}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="date" tickFormatter={formatDate} angle={-40} textAnchor="end" height={70} />
                <YAxis allowDecimals={false} />
                <Tooltip labelFormatter={(v) => `Date: ${formatDate(v as string)}`} />
                <Legend />
                {(toolStats.projects).map((project, i) => (
                  <Line
                    key={project}
                    type="monotone"
                    dataKey={project}
                    stroke={PROJECT_COLORS[i % PROJECT_COLORS.length]}
                    strokeWidth={2}
                    dot={{ r: 3 }}
                    name={project}
                  />
                ))}
              </LineChart>
            </ResponsiveContainer>
          ) : (
            <ResponsiveContainer width="100%" height={300}>
              <LineChart data={modelChartData} margin={{ top: 10, right: 30, left: 10, bottom: 10 }}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="date" tickFormatter={formatDate} angle={-40} textAnchor="end" height={70} />
                <YAxis allowDecimals={false} />
                <Tooltip labelFormatter={(v) => `Date: ${formatDate(v as string)}`} />
                <Legend />
                {((toolStats.models) || []).map((model, i) => (
                  <Line
                    key={model}
                    type="monotone"
                    dataKey={model}
                    stroke={PROJECT_COLORS[i % PROJECT_COLORS.length]}
                    strokeWidth={2}
                    dot={{ r: 3 }}
                    name={model}
                  />
                ))}
              </LineChart>
            </ResponsiveContainer>
          )}
        </section>

        {/* Stuck score: bash error rate per day */}
        <section className="agentic-chart">
          <div className="chart-header">
            <h2>Stuck Score — Bash Error Rate %</h2>
            <div className="time-range-buttons">
              <button
                className={bashView === 'all' ? 'active' : ''}
                onClick={() => setBashView('all')}
                type="button"
              >
                All Projects
              </button>
              <button
                className={bashView === 'by-project' ? 'active' : ''}
                onClick={() => setBashView('by-project')}
                type="button"
              >
                By Project
              </button>
            </div>
          </div>
          {bashRequest.loading ? (
            <div className="agentic-loading">Loading stuck score...</div>
          ) : bashRequest.error ? (
            <div className="agentic-error" role="alert">Error: {bashRequest.error.message}</div>
          ) : !bashStats || bashStats.by_date.every(d => d.total === 0) ? (
            <div className="agentic-empty">No bash call data yet.</div>
          ) : bashView === 'all' ? (
            <ResponsiveContainer width="100%" height={300}>
              <LineChart data={bashStats.by_date} margin={{ top: 10, right: 30, left: 10, bottom: 10 }}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="date" tickFormatter={formatDate} angle={-40} textAnchor="end" height={70} />
                <YAxis unit="%" domain={[0, 100]} />
                <Tooltip
                  labelFormatter={(v) => `Date: ${formatDate(v as string)}`}
                  formatter={(v, _name, props) => [`${v}% (${props.payload.errors}/${props.payload.total})`, 'Error Rate']}
                />
                <Legend />
                <Line
                  type="monotone"
                  dataKey="error_rate"
                  stroke="#ff6b6b"
                  strokeWidth={2}
                  dot={{ r: 3 }}
                  name="Error Rate %"
                />
              </LineChart>
            </ResponsiveContainer>
          ) : (
            <ResponsiveContainer width="100%" height={300}>
              <LineChart data={bashProjectChartData} margin={{ top: 10, right: 30, left: 10, bottom: 10 }}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="date" tickFormatter={formatDate} angle={-40} textAnchor="end" height={70} />
                <YAxis unit="%" domain={[0, 100]} />
                <Tooltip labelFormatter={(v) => `Date: ${formatDate(v as string)}`} />
                <Legend />
                {bashStats.projects.map((project, i) => (
                  <Line
                    key={project}
                    type="monotone"
                    dataKey={project}
                    stroke={PROJECT_COLORS[i % PROJECT_COLORS.length]}
                    strokeWidth={2}
                    dot={{ r: 3 }}
                    name={project}
                  />
                ))}
              </LineChart>
            </ResponsiveContainer>
          )}
        </section>

        {/* Marker management — always rendered, independent of chart loading */}
        <section className="agentic-markers">
          <h2>Workflow Markers</h2>
          <p className="markers-description">
            Flag significant events (new agent, workflow change, Mac Mini sessions) to explain
            metric shifts.
          </p>

          {markerError && <div className="marker-error" role="alert">{markerError}</div>}

          {markersUnreadable ? (
            <p className="markers-unreadable">
              Marker editing is disabled while the markers file can't be read, so a new marker
              can't overwrite the existing data.
            </p>
          ) : (
            <>
              <div className="marker-add-form" id="marker-add-form">
                <input
                  id="marker-date"
                  type="date"
                  value={newDate}
                  onChange={e => setNewDate(e.target.value)}
                  placeholder="Date"
                />
                <input
                  id="marker-label"
                  type="text"
                  value={newLabel}
                  maxLength={120}
                  onChange={e => setNewLabel(e.target.value)}
                  placeholder="Label (e.g. Added Mac Mini)"
                />
                <input
                  id="marker-agent"
                  type="text"
                  value={newAgent}
                  onChange={e => setNewAgent(e.target.value)}
                  placeholder="Agent (optional)"
                />
                <button
                  id="marker-add-btn"
                  type="button"
                  onClick={handleAddMarker}
                  className="btn-primary btn-small"
                >
                  Add Marker
                </button>
              </div>

              {markers.length === 0 ? (
                <div className="markers-empty">No markers yet. Add one above to annotate the chart.</div>
              ) : (
                <ul className="marker-list">
                  {markers.map(marker => (
                    <li key={marker.id} className="marker-row">
                      {editingId === marker.id ? (
                        <div className="marker-edit-row">
                          <input
                            type="date"
                            value={editDate}
                            onChange={e => setEditDate(e.target.value)}
                          />
                          <input
                            type="text"
                            value={editLabel}
                            maxLength={120}
                            onChange={e => setEditLabel(e.target.value)}
                          />
                          <input
                            type="text"
                            value={editAgent}
                            placeholder="Agent (optional)"
                            onChange={e => setEditAgent(e.target.value)}
                          />
                          <button
                            type="button"
                            onClick={() => handleSaveEdit(marker.id)}
                            className="btn-primary btn-small"
                          >
                            Save
                          </button>
                          <button
                            type="button"
                            onClick={() => setEditingId(null)}
                            className="btn-small"
                          >
                            Cancel
                          </button>
                        </div>
                      ) : (
                        <div className="marker-display-row">
                          <span className="marker-date">{marker.date}</span>
                          <span className="marker-label">{marker.label}</span>
                          {marker.agent && <span className="marker-agent">{marker.agent}</span>}
                          <span className={`marker-source marker-source--${marker.source}`}>
                            {marker.source}
                          </span>
                          {marker.source === 'manual' && (
                            <>
                              <button
                                type="button"
                                onClick={() => startEdit(marker)}
                                className="btn-small"
                                title="Edit marker"
                              >
                                Edit
                              </button>
                              <button
                                type="button"
                                onClick={() => handleDeleteMarker(marker.id)}
                                className="btn-small btn-danger"
                                title="Delete marker"
                              >
                                Delete
                              </button>
                            </>
                          )}
                        </div>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}
        </section>

        <section className="agentic-notes">
          <h2>Notes & Next Steps</h2>
          <ul>
            <li>Metrics are based on status transitions in task_history.</li>
            <li>Bounce rate is a proxy for autonomy reliability, not a perfect first-pass metric.</li>
            <li>Future: split by agent vs manual tasks, and add per-project trend comparisons.</li>
            <li>Future: Mac Mini sessions will write auto markers once brain.db is hosted.</li>
          </ul>
        </section>
      </div>
    </PageShell>
  );
}
