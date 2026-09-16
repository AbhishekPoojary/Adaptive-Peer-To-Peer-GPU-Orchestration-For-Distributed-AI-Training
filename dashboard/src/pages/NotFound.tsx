import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";

export function NotFound() {
  return (
    <div className="flex flex-col items-center justify-center gap-3 py-20 text-center">
      <h1 className="text-[1.375rem] font-semibold tracking-[-0.02em] text-ink">Page not found</h1>
      <p className="text-sm text-secondary">
        There's no route at this address.
      </p>
      <Button asChild size="sm">
        <Link to="/">Back to Overview</Link>
      </Button>
    </div>
  );
}
