/**
 * A labelled figure and the grid it sits in.
 *
 * Rendered as `<dl>`/`<dt>`/`<dd>`, so a screen reader reads each number with
 * its label instead of announcing a loose string of counts.
 */

type StatProps = {
  label: string;
  value: number | string;
  hint?: string;
};

export function Stat({ label, value, hint }: StatProps) {
  return (
    <div className="stat">
      <dt className="stat__label">{label}</dt>
      <dd className="stat__value">
        {typeof value === "number" ? value.toLocaleString("en-US") : value}
      </dd>
      {hint && <p className="stat__hint">{hint}</p>}
    </div>
  );
}

export function StatGrid({ children }: { children: React.ReactNode }) {
  return <dl className="stat-grid">{children}</dl>;
}
