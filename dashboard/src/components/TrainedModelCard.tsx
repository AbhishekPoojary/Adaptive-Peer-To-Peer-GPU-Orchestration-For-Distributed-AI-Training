import { useState } from "react";
import { useJobCheckpointQuery } from "@/api/jobs";
import { ApiError } from "@/api/client";
import { getToken } from "@/api/session";
import { Button } from "@/components/ui/button";
import { toast } from "@/hooks/use-toast";
import { formatBytes } from "@/lib/format";

/**
 * The trained model a job produced, with a way to actually get it
 * (ADR-006 addendum 2).
 *
 * Renders nothing when the job has no checkpoint. That is an ordinary state —
 * checkpointing needs object storage configured on the peer that ran the job —
 * and an empty "no model" card on every such job page would be noise rather
 * than information.
 *
 * The download goes through the orchestrator rather than a presigned storage
 * URL, because the orchestrator signs against its own S3 endpoint, which in the
 * compose topology is `http://minio:9000`: correct for a peer on that network
 * and unresolvable from a browser. Fetching as a blob (rather than pointing an
 * `<a>` at the path) is what lets the request carry the bearer token, since the
 * API requires auth and a plain navigation would not send it.
 */
export function TrainedModelCard({ jobId }: { jobId: string }) {
  const { data, error } = useJobCheckpointQuery(jobId);
  const [downloading, setDownloading] = useState(false);

  // Storage being unreachable is worth saying; "no checkpoint" is not.
  if (error) {
    return (
      <section className="rounded-md border border-hairline bg-panel p-4">
        <h2 className="mb-1 text-sm font-semibold text-primary">Trained model</h2>
        <p className="text-xs text-secondary">
          {error instanceof ApiError
            ? error.message
            : "Couldn't check whether this job saved a model."}
        </p>
      </section>
    );
  }

  if (!data) return null;

  async function handleDownload() {
    if (!data) return;
    setDownloading(true);
    try {
      const token = getToken();
      const response = await fetch(`/api${data.download_path}`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (!response.ok) {
        throw new ApiError(
          "The model couldn't be downloaded. It may have been removed from storage.",
          response.status,
        );
      }
      const blob = await response.blob();

      // Object URL + synthetic click: the only way to name a downloaded blob.
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = data.filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      // Revoked on the next tick — revoking synchronously can cancel the
      // download in some browsers before it has started reading.
      setTimeout(() => URL.revokeObjectURL(url), 0);
    } catch (err) {
      toast({
        title: "Download failed",
        description: err instanceof ApiError ? err.message : undefined,
        variant: "destructive",
      });
    } finally {
      setDownloading(false);
    }
  }

  return (
    <section className="rounded-md border border-hairline bg-panel p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold text-primary">Trained model</h2>
          <p className="mt-1 text-xs text-secondary">
            Saved at epoch {data.epoch}, step {data.step.toLocaleString()}
            {data.loss !== null && ` · loss ${data.loss.toFixed(4)}`}
            {data.size_bytes !== null && ` · ${formatBytes(data.size_bytes)}`}
          </p>
          <p className="mt-1 text-xs text-tertiary">{data.format}</p>
        </div>

        <Button onClick={() => void handleDownload()} disabled={downloading}>
          {downloading ? "Downloading…" : "Download model"}
        </Button>
      </div>

      <details className="mt-3">
        <summary className="cursor-pointer text-xs text-tertiary">
          How to load it
        </summary>
        <pre className="mt-2 overflow-x-auto rounded border border-hairline bg-base p-3 text-xs text-secondary font-data">
{`import torch
ckpt = torch.load("${data.filename}", map_location="cpu")
model.load_state_dict(ckpt["model_state"])`}
        </pre>
        <p className="mt-2 text-xs text-tertiary">
          A <code className="font-data">.pt</code> file is a Python pickle, so
          loading one executes code. Only load checkpoints you produced.
        </p>
        <p className="mt-1 text-xs text-tertiary font-data break-all">
          {data.key}
        </p>
      </details>
    </section>
  );
}
