import type { ReactNode } from "react";
import { Inbox } from "lucide-react";
import { cn } from "@/lib/utils";

export interface EmptyStateProps {
  title: string;
  description?: ReactNode;
  action?: ReactNode;
  icon?: ReactNode;
  className?: string;
}

/** Every list gets one of these instead of rendering blank. Plain language +
 * a clear next action — never a fake "waiting" spinner with nothing behind it. */
export function EmptyState({ title, description, action, icon, className }: EmptyStateProps) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center gap-2 rounded-md border border-dashed border-hairline bg-panel px-6 py-12 text-center",
        className,
      )}
    >
      <div className="mb-1 text-tertiary">{icon ?? <Inbox className="size-8" />}</div>
      <p className="text-sm font-medium text-primary">{title}</p>
      {description && (
        <div className="max-w-md text-sm text-secondary">{description}</div>
      )}
      {action && <div className="mt-3">{action}</div>}
    </div>
  );
}
