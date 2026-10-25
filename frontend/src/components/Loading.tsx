type LoadingBlockProps = {
  label?: string;
  variant?: "inline" | "block";
};

export function LoadingBlock({ label = "Loading…", variant = "block" }: LoadingBlockProps) {
  return (
    <div className={`loading loading--${variant}`} role="status" aria-live="polite">
      <span className="loading__spinner" aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}

export function ErrorBlock({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="error-block" role="alert">
      <p>{message}</p>
      {onRetry && (
        <button type="button" className="btn btn--ghost btn--sm" onClick={onRetry}>
          Try again
        </button>
      )}
    </div>
  );
}
