import type { ButtonHTMLAttributes, ReactNode } from "react";

type Variant = "default" | "primary" | "ghost" | "danger";
type Size = "md" | "sm";

/**
 * The single button primitive. Every button in both interfaces renders through
 * this so height, radius, weight, and focus treatment never drift.
 */
export function Button({
  variant = "default",
  size = "md",
  iconOnly = false,
  block = false,
  icon,
  className,
  children,
  type = "button",
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: Variant;
  size?: Size;
  iconOnly?: boolean;
  block?: boolean;
  icon?: ReactNode;
}) {
  const classes = [
    "btn",
    variant !== "default" && `btn--${variant}`,
    size === "sm" && "btn--sm",
    iconOnly && "btn--icon",
    block && "btn--block",
    className,
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <button type={type} className={classes} {...rest}>
      {icon}
      {children}
    </button>
  );
}
