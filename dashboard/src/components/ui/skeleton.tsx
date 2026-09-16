import { cn } from "@/lib/utils";

/**
 * Loading placeholder.
 *
 * A sweep rather than a pulse: an opacity pulse on a light ground is nearly
 * invisible, and it reads as something broken rather than something arriving.
 *
 * Only ever used while a request is genuinely in flight. A skeleton standing in
 * for a value that will never arrive would be a placeholder implying data,
 * which PRODUCT.md's anti-fabrication law rules out — that case renders as a
 * no-signal dash with its reason instead (see `Figure`).
 */
function Skeleton({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      aria-hidden="true"
      className={cn(
        "relative overflow-hidden rounded-[7px] bg-sunken",
        "after:absolute after:inset-0 after:-translate-x-full after:animate-[sweep_1.6s_ease-in-out_infinite] after:bg-[linear-gradient(90deg,transparent,rgba(255,255,255,0.75),transparent)] motion-reduce:after:hidden",
        className,
      )}
      {...props}
    />
  );
}

export { Skeleton };
