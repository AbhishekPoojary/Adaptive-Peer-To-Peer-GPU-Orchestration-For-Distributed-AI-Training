import { cn } from "@/lib/utils";

function Skeleton({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        "animate-pulse motion-reduce:animate-none rounded bg-elevated",
        className,
      )}
      {...props}
    />
  );
}

export { Skeleton };
