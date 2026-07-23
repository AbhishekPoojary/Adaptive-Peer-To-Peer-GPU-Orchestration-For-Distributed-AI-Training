import type { ReactNode } from "react";
import { useState } from "react";
import { ChevronRight, Terminal } from "lucide-react";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";

/**
 * Collapsed-by-default panel for internal fields that don't belong in the
 * plain-language primary copy: lease_epoch, scheduling L/R/D/S scores,
 * token/JWT internals if ever shown. Used on Job detail (scheduling
 * decisions) and available anywhere else raw internals need surfacing.
 */
export function TechnicalDetails({
  title = "Technical details",
  children,
  defaultOpen = false,
}: {
  title?: string;
  children: ReactNode;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <CollapsibleTrigger className="flex w-full items-center gap-2 rounded-md border border-hairline bg-panel px-3 py-2 text-left text-sm font-medium text-secondary outline-none transition-colors motion-reduce:transition-none hover:text-primary focus-visible:ring-2 focus-visible:ring-accent">
        <Terminal className="size-3.5" aria-hidden="true" />
        {title}
        <ChevronRight
          className={cn(
            "ml-auto size-3.5 transition-transform motion-reduce:transition-none",
            open && "rotate-90",
          )}
          aria-hidden="true"
        />
      </CollapsibleTrigger>
      <CollapsibleContent className="mt-2 rounded-md border border-hairline bg-base/40 p-3">
        {children}
      </CollapsibleContent>
    </Collapsible>
  );
}
