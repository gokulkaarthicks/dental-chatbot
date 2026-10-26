/**
 * A small dotted state pill for the overview page's health rows.
 *
 * The dot is the status cue and the text is the status: colour alone never
 * carries meaning. Reads from the shared `pill--*` modifiers in `global.css`.
 */

type StatusTone = "ok" | "warn" | "error" | "neutral";

type StatusPillProps = {
  tone: StatusTone;
  children: React.ReactNode;
};

export function StatusPill({ tone, children }: StatusPillProps) {
  return (
    <span className={`pill pill--${tone}`}>
      <span className="pill__dot" aria-hidden="true" />
      {children}
    </span>
  );
}
