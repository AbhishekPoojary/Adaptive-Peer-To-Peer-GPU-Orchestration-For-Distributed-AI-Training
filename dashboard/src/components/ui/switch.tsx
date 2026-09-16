import * as React from "react";
import * as SwitchPrimitive from "@radix-ui/react-switch";
import { cn } from "@/lib/utils";

/**
 * Switch.
 *
 * Adapted from the toggle idiom on 21st.dev, re-tokened: those ship shadcn's
 * `primary`/`input` defaults, which on this palette would put an unrelated
 * colour on the page. The accent is spent here because a switch is a control
 * whose state must read at a glance, and the thumb's travel is the one place
 * motion is doing real work rather than decorating.
 *
 * Replaces a raw `<input type="checkbox">`. A checkbox says "this is one of
 * several things you might tick"; a switch says "this is on, and you can turn
 * it off", which is what the setting it controls actually is.
 */
function Switch({
  className,
  ...props
}: React.ComponentProps<typeof SwitchPrimitive.Root>) {
  return (
    <SwitchPrimitive.Root
      className={cn(
        "peer inline-flex h-[18px] w-8 shrink-0 cursor-pointer items-center rounded-full border border-transparent transition-colors duration-200 ease-out",
        "data-[state=checked]:bg-accent data-[state=unchecked]:bg-hairline-strong",
        "disabled:cursor-not-allowed disabled:opacity-50 motion-reduce:transition-none",
        className,
      )}
      {...props}
    >
      <SwitchPrimitive.Thumb
        className={cn(
          "pointer-events-none block size-3.5 rounded-full bg-white shadow-[0_1px_2px_rgba(14,22,38,0.28)]",
          "transition-transform duration-200 ease-out motion-reduce:transition-none",
          "data-[state=checked]:translate-x-[15px] data-[state=unchecked]:translate-x-[2px]",
        )}
      />
    </SwitchPrimitive.Root>
  );
}

export { Switch };
