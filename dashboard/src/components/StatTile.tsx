import type { ReactNode } from "react";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

/**
 * A labelled value inside a panel.
 *
 * Not a panel itself. Every caller already places these inside one, and a
 * shadowed box inside a shadowed box is the nested-card mistake — it reads as
 * two levels of importance where there is only one. Separation comes from a
 * hairline between columns and from space, the same device `FigureRow` uses at
 * the larger size.
 *
 * `value` of `"—"` is the honest empty: callers pass a dash when nothing
 * measured the figure, and it renders in the no-signal colour so it cannot be
 * mistaken for a measurement of zero.
 */
export function StatTile({
  label,
  value,
  hint,
  icon,
  isLoading,
  className,
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  icon?: ReactNode;
  isLoading?: boolean;
  className?: string;
}) {
  const unmeasured = value === "—";

  return (
    <div className={cn("flex min-w-0 flex-col gap-1.5", className)}>
      <div className="label flex items-center gap-1.5">
        {label}
        {icon}
      </div>
      {isLoading ? (
        <Skeleton className="h-7 w-16" />
      ) : (
        <div
          className={cn(
            "numeral truncate font-data text-[1.375rem] leading-tight",
            unmeasured ? "text-nosignal-text" : "text-ink",
          )}
        >
          {value}
        </div>
      )}
      {hint && <div className="text-xs text-muted">{hint}</div>}
    </div>
  );
}
