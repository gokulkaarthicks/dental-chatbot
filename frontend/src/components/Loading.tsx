/**
 * Loading and error blocks for the patient surfaces.
 *
 * Loading keeps a spinner plus a label and stays the height of the content it
 * replaces, so a slow fetch does not shift the page. Errors are text first and
 * only offer a retry when the caller can actually re-run the request.
 *
 * The staff dashboard keeps its own equivalents under the `sd-` namespace --
 * `SkeletonRows` for tabular loading and `ErrorState` for failures -- because
 * dashboard rows need a different shape. Both draw from the same tokens.
 */

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
