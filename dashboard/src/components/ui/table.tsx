import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * Tables.
 *
 * The dense pages are the ones that decide whether this reads as a product or
 * as scaffolding, and the change that does most of the work is removing rules:
 * a sunken header band, a hairline under it, and hairlines only *between* rows
 * — no vertical rules, no border on the last row, no outer frame. The panel
 * around it already provides the edge.
 *
 * Rows are 44px rather than 36px. Slightly generous for an operator scanning a
 * hundred of them, and a long way from cramped for the visitor reading three.
 */
function Table({
  className,
  scrollerClassName,
  ...props
}: React.ComponentProps<"table"> & { scrollerClassName?: string }) {
  return (
    // Its own scroller: a wide table scrolls sideways inside the panel rather
    // than making the whole page scroll horizontally, and when a height is
    // given it becomes the scroll parent the sticky header anchors to.
    <div className={cn("w-full overflow-auto", scrollerClassName)}>
      <table
        className={cn("w-full caption-bottom border-collapse text-[0.8125rem]", className)}
        {...props}
      />
    </div>
  );
}

function TableHeader({ className, ...props }: React.ComponentProps<"thead">) {
  return (
    <thead
      className={cn(
        "bg-sunken [&_tr]:hover:bg-transparent",
        // Sticks to the top of the nearest scroll container while the rows
        // pass under it. On the 61-row Machines page a column heading that
        // scrolls away leaves you counting cells to work out what you are
        // looking at.
        "[&_th]:sticky [&_th]:top-0 [&_th]:z-10 [&_th]:bg-sunken",
        // The hairline rides as a shadow: a sticky cell leaves its own border
        // behind at the original position.
        "[&_th]:shadow-[inset_0_-1px_0_var(--hairline)]",
        className,
      )}
      {...props}
    />
  );
}

function TableBody({ className, ...props }: React.ComponentProps<"tbody">) {
  return (
    <tbody
      className={cn("[&_tr:last-child]:border-0", className)}
      {...props}
    />
  );
}

function TableRow({
  className,
  clickable,
  ...props
}: React.ComponentProps<"tr"> & { clickable?: boolean }) {
  return (
    <tr
      className={cn(
        "border-b border-hairline transition-colors duration-150 ease-out motion-reduce:transition-none",
        clickable && "cursor-pointer hover:bg-sunken",
        className,
      )}
      {...props}
    />
  );
}

function TableHead({ className, ...props }: React.ComponentProps<"th">) {
  return (
    <th
      className={cn(
        // The one label style in the system, so a column heading never
        // outranks the data under it.
        "label h-8 px-3 text-left align-middle whitespace-nowrap",
        className,
      )}
      {...props}
    />
  );
}

function TableCell({ className, ...props }: React.ComponentProps<"td">) {
  return (
    <td className={cn("px-3 py-3 align-middle text-ink", className)} {...props} />
  );
}

export { Table, TableHeader, TableBody, TableRow, TableHead, TableCell };
