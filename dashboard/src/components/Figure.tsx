import { cn } from "@/lib/utils";

/**
 * A headline figure, and what measured it.
 *
 * The big-number-small-label tile is the default this category ships, and on
 * its own it is the lazy move. What earns it here is the third line: every
 * figure states the quantity behind it, so a number can be weighed rather than
 * just read. "3 machines" means little; "3 machines · of 7 enrolled" is a fact.
 *
 * `value === null` is the load-bearing case. An unmeasured figure renders as a
 * dash in the no-signal colour with the reason beside it — never a zero, never
 * a skeleton that implies a number is coming, never a plausible placeholder.
 * PRODUCT.md's anti-fabrication law applies to pixels, and this is where.
 */
export function Figure({
  value,
  label,
  measuredBy,
  unmeasuredReason = "not reported",
  tone = "ink",
  className,
}: {
  /** The measured value, or null when nothing has reported one. */
  value: string | number | null;
  label: string;
  /** The quantity that makes the value mean something. */
  measuredBy?: string;
  /** Shown in place of `measuredBy` when the value is null. */
  unmeasuredReason?: string;
  tone?: "ink" | "ok" | "caution" | "fault";
  className?: string;
}) {
  const unmeasured = value === null;

  return (
    <div className={cn("min-w-0", className)}>
      <div
        className={cn(
          "numeral text-[2.125rem] leading-[1.05] sm:text-[2.5rem]",
          unmeasured && "text-nosignal-text",
          !unmeasured && tone === "ink" && "text-ink",
          !unmeasured && tone === "ok" && "text-ok",
          !unmeasured && tone === "caution" && "text-caution",
          !unmeasured && tone === "fault" && "text-fault",
        )}
      >
        {unmeasured ? "—" : value}
      </div>
      <div className="label mt-1.5">{label}</div>
      <div
        className={cn(
          "mt-0.5 truncate text-xs",
          unmeasured ? "text-nosignal-text italic" : "text-muted",
        )}
      >
        {unmeasured ? unmeasuredReason : measuredBy}
      </div>
    </div>
  );
}

/**
 * A row of figures, separated by rules rather than boxed into cards.
 *
 * Deliberately not a grid of tiles: the figures are one reading, and hairlines
 * between them keep that reading continuous instead of chopping it into four
 * competing containers.
 */
export function FigureRow({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "grid grid-cols-2 gap-x-6 gap-y-7 sm:gap-x-0 lg:grid-cols-4",
        "sm:[&>*:not(:first-child)]:border-l sm:[&>*:not(:first-child)]:border-hairline sm:[&>*:not(:first-child)]:pl-6",
        className,
      )}
    >
      {children}
    </div>
  );
}
