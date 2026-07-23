import { FlaskConical } from "lucide-react";
import { EmptyState } from "@/components/EmptyState";

/**
 * Placeholder route. No benchmark endpoint exists yet — evaluation runs
 * (bench/) aren't wired to the orchestrator API until M9. This renders an
 * honest empty state only; no fake charts, no invented numbers.
 */
export function Benchmarks() {
  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-lg font-semibold text-primary">Benchmarks</h1>
      <EmptyState
        icon={<FlaskConical className="size-8" />}
        title="No benchmark runs yet"
        description="This page will show evaluation-run results (throughput, accuracy, convergence) once benchmark execution lands in M9. There is nothing to display honestly before then."
      />
    </div>
  );
}
