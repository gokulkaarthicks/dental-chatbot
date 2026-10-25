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
