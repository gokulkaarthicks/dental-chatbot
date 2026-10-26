/**
 * The frame every interactive card sits in, plus the slot formatters it reuses.
 *
 * `CardTone` is the closed set of card states; a new tone is a visual contract,
 * so it is added here and in `cards.css`, not spelled inline at a call site.
 */

import type { ReactNode } from "react";

export type CardTone = "slots" | "pending" | "confirmed" | "neutral" | "urgent";

/**
 * Shared frame for every interactive card.
 *
 * Cards look like a desk form, not a chat ornament: white surface, one hairline
 * border, a small state label in the header, and actions in a tinted footer.
 * Tone is communicated by the label and a single status dot, never by a glow
 * or an accent bar.
 */
export function CardShell({
  tone,
  eyebrow,
  title,
  children,
  footer,
}: {
  tone: CardTone;
  eyebrow: string;
  title: string;
  children: ReactNode;
  footer?: ReactNode;
}) {
  return (
    <section className={`cardx cardx--${tone}`} aria-label={title}>
      <header className="cardx__head">
        <p className="cardx__eyebrow">
          <span className="cardx__dot" aria-hidden="true" />
          {eyebrow}
        </p>
        <h2 className="cardx__title">{title}</h2>
      </header>
      <div className="cardx__body">{children}</div>
      {footer && <footer className="cardx__foot">{footer}</footer>}
    </section>
  );
}

export function formatSlotDay(iso: string | null | undefined): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
  });
}

export function formatSlotTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}
