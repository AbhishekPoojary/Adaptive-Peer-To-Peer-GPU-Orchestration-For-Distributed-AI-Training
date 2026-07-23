import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center gap-1.5 rounded border px-1.5 py-0.5 text-xs font-medium font-sans w-fit whitespace-nowrap shrink-0",
  {
    variants: {
      variant: {
        neutral: "border-hairline bg-elevated text-secondary",
        good: "border-good/40 bg-good/10 text-good",
        active: "border-active/40 bg-active/10 text-active",
        warn: "border-warn/40 bg-warn/10 text-warn",
        bad: "border-bad/40 bg-bad/10 text-bad",
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
