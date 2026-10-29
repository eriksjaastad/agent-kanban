import { useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { PageShell } from './PageShell';
import { Spinner } from './Spinner';
import { OutreachPanel } from './OutreachPanel';
import { useRequest } from '../hooks/useRequest';
import './MorningPage.css';

interface Segment {
  type: 'text' | 'strong' | 'code' | 'link';
  text: string;
  href?: string;
}

interface Step {
  number: string;
  channel: string;
  time: string;
  description: Segment[];
}

interface HnSnapshot {
  kind: 'freelance' | 'hiring';
  filename: string;
  present: boolean;
  text: string | null;
}

interface MorningData {
  date: string;
  source: string;
  intro: Segment[][];
  steps: Step[];
  next_morning: Segment[][];
  snapshots: HnSnapshot[];
}

function storageKey(date: string): string {
  return `morning-checklist:${date}`;
}

function readStoredChecks(date: string): string[] {
  try {
    const raw = window.localStorage.getItem(storageKey(date));
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) {
      return parsed.filter((item): item is string => typeof item === 'string');
    }
    return [];
  } catch {
    return [];
  }
}

function writeStoredChecks(date: string, checked: string[]): void {
  try {
    window.localStorage.setItem(storageKey(date), JSON.stringify(checked));
  } catch {
    // Local storage is unavailable; the page still works for this session.
  }
}

function renderSegments(segments: Segment[], keyPrefix: string): ReactNode[] {
  return segments.map((segment, index) => {
    const key = `${keyPrefix}-${index}`;
    if (segment.type === 'strong') {
      return <strong key={key}>{segment.text}</strong>;
    }
    if (segment.type === 'code') {
      return <code key={key}>{segment.text}</code>;
    }
    if (segment.type === 'link') {
      return (
        <a key={key} href={segment.href} target="_blank" rel="noopener noreferrer">
          {segment.text}
        </a>
      );
    }
    return <span key={key}>{segment.text}</span>;
  });
}

export function MorningPage() {
  const { data, error, loading } = useRequest<MorningData>(async (signal) => {
    const response = await fetch('/api/morning', { signal });
    if (!response.ok) {
      const payload = await response.json().catch(() => null);
      const detail =
        payload && typeof payload.detail === 'string'
          ? payload.detail
          : `Failed to load morning plan (HTTP ${response.status})`;
      throw new Error(detail);
    }
    return (await response.json()) as MorningData;
  }, []);
  // Ticks made on this page, keyed by the plan's date; until the first tick
  // the stored checks for that date are shown.
  const [ticked, setTicked] = useState<{ date: string; checked: Set<string> } | null>(null);
  const storedChecks = useMemo(() => new Set(data ? readStoredChecks(data.date) : []), [data]);
  const checked = data && ticked?.date === data.date ? ticked.checked : storedChecks;

  useEffect(() => {
    if (error) console.error('Failed to load morning plan:', error);
  }, [error]);

  function toggleStep(number: string) {
    if (!data) return;
    const next = new Set(checked);
    if (next.has(number)) {
      next.delete(number);
    } else {
      next.add(number);
    }
    setTicked({ date: data.date, checked: next });
    writeStoredChecks(data.date, Array.from(next));
  }

  function formatDate(date: string): string {
    try {
      return new Intl.DateTimeFormat('en-US', {
        weekday: 'long',
        month: 'long',
        day: 'numeric',
        year: 'numeric',
      }).format(new Date(`${date}T12:00:00`));
    } catch {
      return date;
    }
  }

  const hasSteps = (data?.steps.length ?? 0) > 0;

  return (
    <PageShell
      title="Morning"
      subtitle={data ? formatDate(data.date) : 'Warm-up checklist'}
      contentWidth="narrow"
    >
      <OutreachPanel />

      {loading ? (
        <div className="morning-loading">
          <Spinner size="large" />
        </div>
      ) : !data || !hasSteps ? (
        <div className="morning-error">
          <p role="alert">{error?.message ?? 'No morning plan to show right now.'}</p>
        </div>
      ) : (
        <div className="morning-container">
          {data.intro.length > 0 && (
            <section className="morning-intro">
              {data.intro.map((paragraph, index) => (
                <p key={`intro-${index}`}>{renderSegments(paragraph, `intro-${index}`)}</p>
              ))}
            </section>
          )}

          <section className="morning-steps" aria-label="Morning checklist">
            {data.steps.map((step) => {
              const isChecked = checked.has(step.number);
              return (
                <div
                  key={step.number}
                  className={`morning-step${isChecked ? ' morning-step--done' : ''}`}
                >
                  <input
                    type="checkbox"
                    id={`morning-step-${step.number}`}
                    checked={isChecked}
                    onChange={() => toggleStep(step.number)}
                  />
                  <label htmlFor={`morning-step-${step.number}`}>
                    <span className="morning-step-number">{step.number}</span>
                    <span className="morning-step-channel">{step.channel}</span>
                    <span className="morning-step-time">{step.time}</span>
                    <span className="morning-step-description">
                      {renderSegments(step.description, `step-${step.number}`)}
                    </span>
                  </label>
                </div>
              );
            })}
          </section>

          {data.next_morning.length > 0 && (
            <section className="morning-today">
              <h2>Today</h2>
              {data.next_morning.map((paragraph, index) => (
                <p key={`next-${index}`}>{renderSegments(paragraph, `next-${index}`)}</p>
              ))}
            </section>
          )}

          <section className="morning-snapshots">
            {data.snapshots.map((snapshot) => (
              <details key={snapshot.kind} className="morning-snapshot">
                <summary>
                  <span className="morning-snapshot-kind">{snapshot.kind}</span>
                  <span className="morning-snapshot-filename">{snapshot.filename}</span>
                </summary>
                {snapshot.present ? (
                  <pre>{snapshot.text ?? 'Could not read file'}</pre>
                ) : (
                  <p className="morning-snapshot-missing">
                    Not found — check hn-jobs-cron.log
                  </p>
                )}
              </details>
            ))}
          </section>
        </div>
      )}
    </PageShell>
  );
}
