import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * Text input.
 *
 * White field with a hairline, not a filled grey one. On a white panel a
 * grey-filled field reads as disabled — which is exactly what the previous
 * dark-theme token did once the ground went light.
 *
 * The inner shadow is one pixel at the top edge only: enough to seat the field
 * into the surface without becoming a bevel.
 */
function Input({ className, type, ...props }: React.ComponentProps<"input">) {
  return (
    <input
      type={type}
      className={cn(
        "flex h-9 w-full rounded-[var(--radius-control)] border border-hairline bg-surface px-3 text-[0.8125rem] text-ink shadow-[inset_0_1px_1px_rgba(14,22,38,0.04)] outline-none transition-[border-color,box-shadow] duration-150 ease-out placeholder:text-faint hover:border-hairline-strong disabled:cursor-not-allowed disabled:bg-sunken disabled:text-faint motion-reduce:transition-none",
        // File inputs render a native button; give it the secondary button's
        // shape so it stops looking like an unstyled browser control.
        "file:mr-3 file:-ml-1 file:cursor-pointer file:rounded-[7px] file:border-0 file:bg-sunken file:px-2.5 file:py-1.5 file:text-xs file:font-medium file:text-ink hover:file:bg-hairline",
        className,
      )}
      {...props}
    />
  );
}

export { Input };
