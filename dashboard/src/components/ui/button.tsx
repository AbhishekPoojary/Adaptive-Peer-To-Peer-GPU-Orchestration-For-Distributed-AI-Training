import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

/**
 * Buttons.
 *
 * The primary action is **ink**, not the accent. The accent is spent on focus,
 * live measurement and chart lines; a thing being the main action and a thing
 * being live are different claims, and sharing one colour makes both vaguer.
 *
 * No focus ring here. `:focus-visible` is themed once globally, drawn outside
 * the element so it never shifts layout or gets clipped by a rounded parent —
 * a second ring at component level just doubles it.
 */
const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-[var(--radius-control)] font-sans text-[0.8125rem] font-medium outline-none transition-[background-color,color,border-color,box-shadow] duration-150 ease-out motion-reduce:transition-none disabled:pointer-events-none disabled:opacity-45 [&_svg]:size-4 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        default: "bg-ink text-white shadow-[0_1px_2px_rgba(14,22,38,0.16)] hover:bg-[#1c2740]",
        secondary:
          "border border-hairline bg-surface text-ink hover:border-hairline-strong hover:bg-sunken",
        outline:
          "border border-hairline-strong bg-transparent text-ink hover:bg-sunken",
        ghost: "bg-transparent text-muted hover:bg-sunken hover:text-ink",
        destructive:
          "border border-[color-mix(in_srgb,var(--fault)_30%,transparent)] bg-fault-wash text-fault hover:bg-[color-mix(in_srgb,var(--fault)_16%,transparent)]",
        link: "bg-transparent text-accent underline decoration-[1.5px] underline-offset-[3px] hover:decoration-2",
      },
      size: {
        default: "h-9 px-3.5",
        sm: "h-8 px-3 text-xs",
        lg: "h-11 px-5 text-sm",
        icon: "size-9",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

function Button({ className, variant, size, asChild = false, ...props }: ButtonProps) {
  const Comp = asChild ? Slot : "button";
  return (
    <Comp className={cn(buttonVariants({ variant, size, className }))} {...props} />
  );
}

export { Button, buttonVariants };
