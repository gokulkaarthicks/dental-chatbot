/**
 * Conversation feedback: the patient's own signal about a turn.
 *
 * A thumbs-down is not a bug report on its own, but it tells the front desk
 * which stored transcript is worth reading. Nothing here is inferred: the
 * rating, optional comment, and reply excerpt all come from SQLite.
 */

import { useMemo, useState } from "react";
import { ThumbsDown, ThumbsUp } from "lucide-react";
import { staffApi, type ConversationFeedbackRow } from "../../../api/staff";
import { Button } from "../../../components/Button";
import { fullTimestamp, relativeTime } from "../../../lib/staffFormat";
import { EmptyState, ErrorState, SkeletonRows } from "../StaffUi";
import { useStaffQuery } from "../useStaffData";

const WINDOWS = [
  { days: 7, label: "Last 7 days" },
  { days: 30, label: "Last 30 days" },
  { days: 90, label: "Last 90 days" },
];

export function FeedbackSection() {
  const [days, setDays] = useState(30);
  const key = useMemo(() => `feedback-${days}`, [days]);
  const query = useStaffQuery(key, (signal) => staffApi.feedback(days, signal));

  const counts = query.data?.counts ?? { up: 0, down: 0, total: 0 };
  const positive = counts.total > 0 ? Math.round((counts.up / counts.total) * 100) : null;
  const rows = query.data?.feedback ?? [];

  return (
    <div className="sd-stack">
      <div className="sd-toolbar">
        <label className="sd-toolbar__field">
          <span className="sd-field__label">Window</span>
          <select
            className="input"
            value={days}
            onChange={(event) => setDays(Number(event.target.value))}
          >
            {WINDOWS.map((option) => (
              <option key={option.days} value={option.days}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <div className="sd-toolbar__actions">
          <Button onClick={query.reload}>Refresh</Button>
        </div>
      </div>

      <section className="sd-stats sd-stats--three" aria-label="Feedback summary">
        <div className="sd-stat">
          <span className="sd-stat__label">Positive</span>
          <span className="sd-stat__value">{positive === null ? "--" : `${positive}%`}</span>
          <span className="sd-stat__hint">Share of rated replies</span>
        </div>
        <div className="sd-stat">
          <span className="sd-stat__label">Helpful</span>
          <span className="sd-stat__value">{counts.up}</span>
          <span className="sd-stat__hint">Thumbs up</span>
        </div>
        <div className="sd-stat">
          <span className="sd-stat__label">Needs a look</span>
          <span className="sd-stat__value">{counts.down}</span>
          <span className="sd-stat__hint">Thumbs down</span>
        </div>
      </section>

      {query.error ? <ErrorState message={query.error} onRetry={query.reload} /> : null}

      <section className="sd-panel" aria-label="Recent feedback">
        <header className="sd-panel__header">
          <div>
            <h2 className="sd-panel__title">Recent feedback</h2>
            <p className="sd-panel__sub">
              Newest first. The excerpt is the reply the patient rated.
              {query.refreshing ? " · refreshing" : ""}
            </p>
          </div>
        </header>

        {query.loading ? <SkeletonRows rows={5} label="Loading feedback" /> : null}

        {!query.loading && !query.error && rows.length === 0 ? (
          <EmptyState
            title="No feedback yet"
            hint="Patients can rate the latest reply at the bottom of a conversation."
          />
        ) : null}

        {rows.length > 0 ? (
          <ul className="sd-list">
            {rows.map((row) => (
              <FeedbackRow key={row.id} row={row} />
            ))}
          </ul>
        ) : null}
      </section>
    </div>
  );
}

function FeedbackRow({ row }: { row: ConversationFeedbackRow }) {
  const down = row.rating === "down";
  return (
    <li className="sd-list__row">
      <div className="sd-list__who">
        <span className="sd-list__name">
          {row.conversation_title ?? "Conversation"}
          <span className={`sd-badge ${down ? "sd-badge--warn" : "sd-badge--ok"} sd-list__kind`}>
            {down ? "Not helpful" : "Helpful"}
          </span>
        </span>
        <span className="sd-list__meta">{row.excerpt || "No reply text stored."}</span>
        {row.reason ? (
          <span className="sd-list__meta sd-feedback__reason">“{row.reason}”</span>
        ) : null}
      </div>
      <div className="sd-list__side">
        {down ? (
          <ThumbsDown size={15} strokeWidth={1.75} aria-label="Not helpful" />
        ) : (
          <ThumbsUp size={15} strokeWidth={1.75} aria-label="Helpful" />
        )}
        <span className="sd-list__time" title={fullTimestamp(row.created_at)}>
          {relativeTime(row.created_at)}
        </span>
      </div>
    </li>
  );
}
