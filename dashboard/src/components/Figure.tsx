import { TrendingUp } from "lucide-react";
import { useCountUp } from "@/hooks/use-count-up";
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
  delta,
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
  /**
   * A change this figure actually measured, e.g. "+22.4 pts since epoch 1".
   *
   * The idiom this was rebuilt into puts a trend chip here, usually reading
   * "vs last month". This product keeps no historical series for most of these
   * figures, so such a chip would be an invented comparison — the one thing
   * PRODUCT.md rules out. Passed only where a real before-and-after exists.
   */
  delta?: string;
  className?: string;
}) {
  const unmeasured = value === null;
  // Animated only when the figure is a bare number. A formatted string like
  // "86.4%" is left alone rather than parsed and re-formatted, which would put
  // this component in the business of guessing at its own callers' units.
  const numeric = typeof value === "number" ? value : null;
  const counted = useCountUp(numeric);
  const shown =
    numeric !== null && counted !== null ? Math.round(counted) : value;

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
        {unmeasured ? "—" : shown}
      </div>
      <div className="label mt-1.5 flex items-center gap-2">
        {label}
        {!unmeasured && delta && (
          <span className="inline-flex items-center gap-1 rounded-full bg-ok-wash px-1.5 py-px text-[0.625rem] font-medium tracking-normal normal-case text-ok">
            <TrendingUp className="size-2.5" aria-hidden="true" />
            {delta}
          </span>
        )}
      </div>
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
