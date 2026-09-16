import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * The container of this design system.
 *
 * White, generously rounded, no border, seated on the tinted canvas by one
 * soft shadow with a real offset. The tint is what makes this work: on a pure
 * white page a white panel needs a border to exist at all, and the border is
 * what makes a dense page read as a grid of boxes.
 *
 * Panels do not nest. If content inside a panel needs separating, it gets a
 * hairline rule or space, never a second panel.
 */
function Panel({
  className,
  flush,
  ...props
}: React.ComponentProps<"section"> & {
  /** Drop the inner padding — for panels whose content is a full-bleed table. */
  flush?: boolean;
}) {
  return (
    <section
      className={cn(
        "rounded-[var(--radius-panel)] bg-surface shadow-panel",
        flush ? "overflow-hidden" : "p-5",
        className,
      )}
      {...props}
    />
  );
}

/**
 * A panel's heading row.
 *
 * `live` claims the panel is receiving data right now, so it is only ever
 * passed when that is true — the pulsing dot is an assertion, not an ornament.
 */
function PanelHeader({
  title,
  hint,
  live,
  action,
  className,
}: {
  title: string;
  hint?: string;
  live?: boolean;
  action?: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("mb-4 flex items-start justify-between gap-4", className)}>
      <div className="min-w-0">
        <h2 className="flex items-center gap-2 text-[0.9375rem] font-semibold tracking-[-0.01em] text-ink">
          {title}
          {live && (
            <span className="inline-flex items-center gap-1.5">
              <span
                aria-hidden="true"
                className="live-dot size-1.5 rounded-full bg-accent"
              />
              <span className="text-[0.6875rem] font-medium tracking-[0.04em] text-accent uppercase">
                Live
              </span>
            </span>
          )}
        </h2>
        {hint && <p className="mt-1 text-[0.8125rem] text-muted">{hint}</p>}
      </div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  );
}

export { Panel, PanelHeader };
