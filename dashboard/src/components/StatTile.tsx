import type { ReactNode } from "react";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

export function StatTile({
  label,
  value,
  hint,
  icon,
  isLoading,
  className,
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  icon?: ReactNode;
  isLoading?: boolean;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-col gap-1 rounded-md border border-hairline bg-panel p-4",
        className,
      )}
    >
      <div className="flex items-center justify-between text-xs font-medium uppercase tracking-wide text-tertiary">
        {label}
        {icon}
      </div>
      {isLoading ? (
        <Skeleton className="h-8 w-16" />
      ) : (
        <div className="font-data text-2xl font-semibold text-primary">{value}</div>
      )}
      {hint && <div className="text-xs text-secondary">{hint}</div>}
    </div>
  );
}
