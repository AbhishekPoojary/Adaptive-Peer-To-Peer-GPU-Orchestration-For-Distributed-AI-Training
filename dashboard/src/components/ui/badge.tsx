import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

/**
 * Status badge.
 *
 * A tinted wash with same-hue text, no border. On a light ground the previous
 * border-plus-10%-fill recipe produced a ring of dirty colour around every
 * state; a flat wash at full text contrast reads cleaner and holds AA.
 *
 * Colour is never the only channel — `StatusPill` always pairs these with a
 * label and an icon, so the state survives greyscale and colour-blindness.
 */
const badgeVariants = cva(
  "inline-flex w-fit shrink-0 items-center gap-1.5 rounded-full px-2 py-0.5 font-sans text-[0.6875rem] font-medium whitespace-nowrap [&_svg]:size-3 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        neutral: "bg-sunken text-muted",
        good: "bg-ok-wash text-ok",
        active: "bg-accent-wash text-accent",
        warn: "bg-caution-wash text-caution",
        bad: "bg-fault-wash text-fault",
      },
    },
    defaultVariants: { variant: "neutral" },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}

export { Badge, badgeVariants };
