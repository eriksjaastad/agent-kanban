import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import './JobStatsChart.css';

interface JobStatsData {
  jobs_per_day: Array<{ date: string; count: number }>;
  submissions_per_day: Array<{ date: string; count: number }>;
}

interface Props {
  stats: JobStatsData;
}

function displayDate(value: string) {
  const [, month, day] = value.split('-');
  return `${Number(month)}/${Number(day)}`;
}

export function JobStatsChart({ stats }: Props) {
  const allDates = new Set<string>();
  stats.jobs_per_day.forEach(item => allDates.add(item.date));
  stats.submissions_per_day.forEach(item => allDates.add(item.date));

  const dateArray = Array.from(allDates).sort();
  
  const jobsMap = new Map(stats.jobs_per_day.map(item => [item.date, item.count]));
  const submissionsMap = new Map(stats.submissions_per_day.map(item => [item.date, item.count]));

  const chartData = dateArray.map(date => ({
    date,
    jobs: jobsMap.get(date) || 0,
    submissions: submissionsMap.get(date) || 0,
  }));

  const hasData = chartData.length > 0;

  return (
    <section className="job-stats-chart" aria-label="Job activity over time">
      <div className="job-stats-chart-heading">
        <h2>Job Activity</h2>
        <p className="job-stats-chart-subtitle">
          Daily job discoveries and submissions by first-seen/submission date
        </p>
      </div>
      {hasData ? (
        <div className="job-stats-plot" role="img" aria-label="Daily job activity time series">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={chartData} margin={{ top: 8, right: 0, left: 0, bottom: 4 }}>
              <CartesianGrid stroke="#364052" strokeDasharray="3 3" />
              <XAxis
                dataKey="date"
                tickFormatter={displayDate}
                stroke="#acb5c3"
                minTickGap={16}
              />
              <YAxis yAxisId="jobs" stroke="#68b5ff" width={52} />
              {/* Submissions run far below discoveries, so they get their own scale. */}
              <YAxis
                yAxisId="submissions"
                orientation="right"
                stroke="#66d9a7"
                width={40}
                allowDecimals={false}
              />
              <Tooltip
                labelFormatter={value => String(value)}
                contentStyle={{ background: '#1e2530', border: '1px solid #47536a' }}
              />
              <Legend />
              <Line
                type="linear"
                yAxisId="jobs"
                dataKey="jobs"
                name="Jobs discovered"
                stroke="#68b5ff"
                strokeWidth={2}
                dot={chartData.length <= 14}
                connectNulls={false}
              />
              <Line
                type="linear"
                yAxisId="submissions"
                dataKey="submissions"
                name="Submissions"
                stroke="#66d9a7"
                strokeWidth={2}
                dot={chartData.length <= 14}
                connectNulls={false}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      ) : (
        <p className="job-stats-chart-empty">No job activity data yet</p>
      )}
    </section>
  );
}
