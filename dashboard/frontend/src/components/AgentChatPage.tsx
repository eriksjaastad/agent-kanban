import { useRequest } from '../hooks/useRequest';
import { PageShell } from './PageShell';
import './AgentChatPage.css';

type ChatMessage = {
  id: number;
  ts: string;
  sender: string;
  recipient?: string | null;
  priority?: string | null;
  reply_to?: number | null;
  body: string;
  reply_to_message?: ChatMessage | null;
};

export function AgentChatPage() {
  const { data, error, loading } = useRequest<ChatMessage[]>(async (signal) => {
    const res = await fetch('/api/agent-chat/messages?limit=150', { signal });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      throw new Error(detail.detail || `HTTP ${res.status}`);
    }
    const body = await res.json();
    return body.messages || [];
  }, []);
  const messages = data ?? [];

  return (
    <PageShell
      title="Agent Chat"
      subtitle="All agent-to-agent traffic (not an inbox filter). Read-only."
      contentWidth="narrow"
    >
      <div className="agent-chat-board">
        {loading && <p className="agent-chat-status">Loading messages…</p>}
        {error && <p className="agent-chat-error" role="alert">{error.message || 'Failed to load Agent Chat'}</p>}
        {!loading && !error && messages.length === 0 && (
          <p className="agent-chat-status">No messages on the board.</p>
        )}
        <ol className="agent-chat-list">
          {messages.map((m) => (
            <li key={m.id} id={`msg-${m.id}`} className="agent-chat-item">
              <div className="agent-chat-meta">
                <span className="agent-chat-id">#{m.id}</span>
                <time dateTime={m.ts}>{new Date(m.ts).toLocaleString()}</time>
                <span className="agent-chat-from">{m.sender}</span>
                <span className="agent-chat-arrow" aria-hidden="true">→</span>
                <span className="agent-chat-to">{m.recipient || 'broadcast'}</span>
                {m.priority && m.priority !== 'normal' && (
                  <span className={`agent-chat-priority priority-${m.priority}`}>{m.priority}</span>
                )}
                {m.reply_to != null && (
                  <a className="agent-chat-reply" href={`#msg-${m.reply_to}`}>
                    re #{m.reply_to}
                  </a>
                )}
              </div>
              {m.reply_to_message && (
                <blockquote className="agent-chat-parent">
                  <span className="agent-chat-parent-label">
                    {m.reply_to_message.sender}:
                  </span>{' '}
                  {m.reply_to_message.body.slice(0, 180)}
                  {m.reply_to_message.body.length > 180 ? '…' : ''}
                </blockquote>
              )}
              <pre className="agent-chat-body">{m.body}</pre>
            </li>
          ))}
        </ol>
      </div>
    </PageShell>
  );
}
