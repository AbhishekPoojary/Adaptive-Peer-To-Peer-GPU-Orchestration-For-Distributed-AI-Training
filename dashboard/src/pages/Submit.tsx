import { useMemo, useState, type FormEvent, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { ChevronRight, Settings2 } from "lucide-react";
import { useNodesQuery } from "@/api/nodes";
import { useJobsQuery, useSubmitJobMutation } from "@/api/jobs";
import { ApiError } from "@/api/client";
import type { JobSubmitRequest } from "@/api/types";
import { Button } from "@/components/ui/button";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { toast } from "@/hooks/use-toast";
import { cn } from "@/lib/utils";

const DATASETS = ["cifar10", "mnist"] as const;
const SCHEDULERS = ["adaptive", "round_robin", "least_loaded"] as const;

/** Jobs states that occupy a node under the "one job per node" rule
 * (orchestrator/schedulers/base.py NodeSnapshot.has_active_lease docstring:
 * ACTIVE lease OR the not-yet-claimed target of a SCHEDULED job). */
const OCCUPYING_JOB_STATES = new Set(["SCHEDULED", "LEASED", "RUNNING"]);

function maxGpuVramBytes(gpus: { vram_bytes: number }[]): number | null {
  if (gpus.length === 0) return null;
  return Math.max(...gpus.map((g) => g.vram_bytes));
}

export function Submit() {
  const navigate = useNavigate();
  const nodesQuery = useNodesQuery();
  const jobsQuery = useJobsQuery();
  const submitMutation = useSubmitJobMutation();

  const [dataset, setDataset] = useState<(typeof DATASETS)[number]>("cifar10");
  // "cnn" (the trainer's real SmallCNN — see trainer/train.py) is the only
  // architecture actually implemented; defaulting to a name it doesn't
  // recognize (e.g. "resnet18") would train fine but silently substitute
  // SmallCNN anyway, which is honest in the logs but a confusing default.
  const [model, setModel] = useState("cnn");
  const [epochs, setEpochs] = useState("5");
  const [batchSize, setBatchSize] = useState("32");
  const [learningRate, setLearningRate] = useState("0.01");
  const [worldSize, setWorldSize] = useState("1");
  const [minGpuMemBytes, setMinGpuMemBytes] = useState("");
  const [schedulerName, setSchedulerName] = useState<(typeof SCHEDULERS)[number]>("adaptive");
  const [submittedBy, setSubmittedBy] = useState("dashboard-operator");
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const parsedMinGpuMem = minGpuMemBytes.trim() === "" ? null : Number(minGpuMemBytes);

  // Client-side approximation of the real ONLINE + fresh-heartbeat + free-of-
  // active-lease + GPU-memory hard filters the orchestrator applies
  // server-side (orchestrator/schedulers/base.py passes_hard_filters). It is
  // a real computation over real /nodes and /jobs data, not a static guess —
  // but it can't see the exact instant the scheduler runs, so it's labeled
  // as an approximation below rather than a guaranteed count.
  const eligibleCount = useMemo(() => {
    if (!nodesQuery.data || !jobsQuery.data) return null;
    const occupied = new Set(
      jobsQuery.data.jobs
        .filter((j) => j.scheduled_node_id && OCCUPYING_JOB_STATES.has(j.state))
        .map((j) => j.scheduled_node_id),
    );
    return nodesQuery.data.nodes.filter((n) => {
      if (n.status !== "ONLINE" || n.heartbeat_stale) return false;
      if (occupied.has(n.id)) return false;
      if (parsedMinGpuMem !== null && !Number.isNaN(parsedMinGpuMem)) {
        const vram = maxGpuVramBytes(n.hardware.gpus);
        if (vram === null || vram < parsedMinGpuMem) return false;
      }
      return true;
    }).length;
  }, [nodesQuery.data, jobsQuery.data, parsedMinGpuMem]);

  function validate(): string | null {
    if (!model.trim()) return "Model is required.";
    if (!submittedBy.trim()) return "Submitted by is required.";
    const e = Number(epochs);
    if (!Number.isInteger(e) || e < 1) return "Epochs must be a whole number >= 1.";
    const bs = Number(batchSize);
    if (!Number.isInteger(bs) || bs < 1) return "Batch size must be a whole number >= 1.";
    const lr = Number(learningRate);
    if (!(lr > 0)) return "Learning rate must be greater than 0.";
    const ws = Number(worldSize);
    if (!Number.isInteger(ws) || ws < 1) return "World size must be a whole number >= 1.";
    if (minGpuMemBytes.trim() !== "") {
      const m = Number(minGpuMemBytes);
      if (!Number.isInteger(m) || m < 0) {
        return "Min GPU memory must be a whole number of bytes >= 0.";
      }
    }
    return null;
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const validationError = validate();
    if (validationError) {
      setFormError(validationError);
      return;
    }
    setFormError(null);

    const body: JobSubmitRequest = {
      spec: {
        dataset,
        model: model.trim(),
        epochs: Number(epochs),
        batch_size: Number(batchSize),
        learning_rate: Number(learningRate),
        world_size: Number(worldSize),
        min_gpu_mem_bytes: minGpuMemBytes.trim() === "" ? null : Number(minGpuMemBytes),
      },
      scheduler_name: schedulerName,
      submitted_by: submittedBy.trim(),
    };

    try {
      const job = await submitMutation.mutateAsync(body);
      toast({ title: "Training started", description: `Job ${job.id.slice(0, 8)} queued.`, variant: "success" });
      navigate(`/jobs/${job.id}`);
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : "Submission failed. Please try again.");
    }
  }

  return (
    <div className="flex max-w-2xl flex-col gap-5">
      <div>
        <h1 className="text-lg font-semibold text-primary">Submit a training job</h1>
        <p className="text-sm text-secondary">
          Queues a real job against the fleet. Once a peer picks it up it runs
          real training (real dataset, real backprop) inside a container on
          that machine — you'll land on the job page to watch it happen.
        </p>
      </div>

      <form onSubmit={(e) => void handleSubmit(e)} className="flex flex-col gap-4">
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Field label="Dataset" htmlFor="dataset">
            <Select value={dataset} onValueChange={(v) => setDataset(v as typeof dataset)}>
              <SelectTrigger id="dataset">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {DATASETS.map((d) => (
                  <SelectItem key={d} value={d}>
                    {d}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>

          <Field label="Model" htmlFor="model">
            <Input
              id="model"
              value={model}
              onChange={(e) => setModel(e.target.value)}
              placeholder="cnn"
              maxLength={128}
              required
            />
          </Field>

          <Field label="Epochs" htmlFor="epochs">
            <Input
              id="epochs"
              type="number"
              min={1}
              step={1}
              value={epochs}
              onChange={(e) => setEpochs(e.target.value)}
              className="font-data"
            />
          </Field>

          <Field label="Batch size" htmlFor="batch_size">
            <Input
              id="batch_size"
              type="number"
              min={1}
              step={1}
              value={batchSize}
              onChange={(e) => setBatchSize(e.target.value)}
              className="font-data"
            />
          </Field>

          <Field label="Learning rate" htmlFor="learning_rate">
            <Input
              id="learning_rate"
              type="number"
              min={0}
              step="any"
              value={learningRate}
              onChange={(e) => setLearningRate(e.target.value)}
              className="font-data"
            />
          </Field>

          <Field label="World size" htmlFor="world_size">
            <Input
              id="world_size"
              type="number"
              min={1}
              step={1}
              value={worldSize}
              onChange={(e) => setWorldSize(e.target.value)}
              className="font-data"
            />
          </Field>
        </div>

        <Field label="Scheduler" htmlFor="scheduler_name">
          <Select
            value={schedulerName}
            onValueChange={(v) => setSchedulerName(v as typeof schedulerName)}
          >
            <SelectTrigger id="scheduler_name" className="sm:w-64">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {SCHEDULERS.map((s) => (
                <SelectItem key={s} value={s}>
                  {s}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>

        <Field label="Submitted by" htmlFor="submitted_by">
          <Input
            id="submitted_by"
            value={submittedBy}
            onChange={(e) => setSubmittedBy(e.target.value)}
            maxLength={255}
            required
          />
        </Field>

        <Collapsible open={advancedOpen} onOpenChange={setAdvancedOpen}>
          <CollapsibleTrigger className="flex items-center gap-2 rounded-md border border-hairline bg-panel px-3 py-2 text-left text-sm font-medium text-secondary outline-none hover:text-primary focus-visible:ring-2 focus-visible:ring-accent">
            <Settings2 className="size-3.5" aria-hidden="true" />
            Advanced options
            <ChevronRight
              className={cn(
                "ml-auto size-3.5 transition-transform motion-reduce:transition-none",
                advancedOpen && "rotate-90",
              )}
              aria-hidden="true"
            />
          </CollapsibleTrigger>
          <CollapsibleContent className="mt-2 rounded-md border border-hairline bg-panel p-3">
            <Field label="Min GPU memory (bytes)" htmlFor="min_gpu_mem_bytes">
              <Input
                id="min_gpu_mem_bytes"
                type="number"
                min={0}
                step={1}
                value={minGpuMemBytes}
                onChange={(e) => setMinGpuMemBytes(e.target.value)}
                placeholder="Leave blank for CPU-eligible"
                className="font-data"
              />
              <p className="mt-1 text-xs text-tertiary">
                Leave blank to allow CPU-only nodes. A value is a hard minimum a
                candidate node's largest single GPU must meet.
              </p>
            </Field>
          </CollapsibleContent>
        </Collapsible>

        <div className="rounded-md border border-hairline bg-panel px-3 py-2 text-sm text-secondary">
          {eligibleCount === null ? (
            "Checking node eligibility…"
          ) : (
            <>
              <span className="font-data font-medium text-primary">{eligibleCount}</span>{" "}
              eligible node{eligibleCount === 1 ? "" : "s"} available right now.{" "}
              <span className="text-xs text-tertiary">
                Approximation computed client-side from live node/job data (online,
                fresh heartbeat, not already occupied, GPU memory if set) — the
                orchestrator's own placement pass is the source of truth.
              </span>
            </>
          )}
        </div>

        {eligibleCount !== null && Number.isInteger(Number(worldSize)) && Number(worldSize) > eligibleCount && (
          <div className="rounded-md border border-warn/40 bg-warn/10 px-3 py-2 text-sm text-primary">
            This job needs {worldSize} peers but only {eligibleCount} are eligible right
            now. It will still be submitted, but it will sit queued until enough
            peers are online and free — it will not block or fail on submit.
          </div>
        )}

        {formError && (
          <div className="rounded-md border border-bad/40 bg-bad/10 px-3 py-2 text-sm text-primary">
            {formError}
          </div>
        )}

        <div className="flex justify-end">
          <Button type="submit" disabled={submitMutation.isPending}>
            {submitMutation.isPending ? "Submitting…" : "Submit job"}
          </Button>
        </div>
      </form>
    </div>
  );
}

function Field({
  label,
  htmlFor,
  children,
}: {
  label: string;
  htmlFor: string;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={htmlFor}>{label}</Label>
      {children}
    </div>
  );
}
