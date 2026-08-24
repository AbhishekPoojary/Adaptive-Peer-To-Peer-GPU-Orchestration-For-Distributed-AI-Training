import { useRef, useState, type FormEvent } from "react";
import {
  useDatasetsQuery,
  useDeleteDatasetMutation,
  useUploadDatasetMutation,
  type Dataset,
} from "@/api/datasets";
import { ApiError } from "@/api/client";
import { isAdmin } from "@/api/session";
import { EmptyState } from "@/components/EmptyState";
import { ErrorState } from "@/components/ErrorState";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "@/hooks/use-toast";
import { formatBytes } from "@/lib/format";

/**
 * Datasets page (ADR-014).
 *
 * Uploading is ADMIN-only server-side; a non-admin sees the list without the
 * form. The button being hidden is a convenience, not the control — the server
 * re-checks the role, so a tampered client can reveal the form but not use it.
 */
export function Datasets() {
  const { data, isPending, error, refetch } = useDatasetsQuery();
  const upload = useUploadDatasetMutation();
  const remove = useDeleteDatasetMutation();
  const admin = isAdmin();

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  // A file input is uncontrolled: React state cannot clear the chosen
  // filename after a successful upload, so it is reset through the node.
  const fileInputRef = useRef<HTMLInputElement>(null);

  async function handleUpload(e: FormEvent) {
    e.preventDefault();
    setFormError(null);
    if (!file) {
      setFormError("Choose a .zip file to upload.");
      return;
    }
    try {
      const result = await upload.mutateAsync({
        name,
        description: description || undefined,
        file,
      });
      toast({
        title: `Uploaded “${result.dataset.name}”`,
        description: result.summary,
      });
      setName("");
      setDescription("");
      setFile(null);
      if (fileInputRef.current) fileInputRef.current.value = "";
    } catch (err) {
      setFormError(
        err instanceof ApiError ? err.message : "Couldn't upload that dataset.",
      );
    }
  }

  async function handleDelete(dataset: Dataset) {
    if (
      !window.confirm(
        `Delete “${dataset.name}”? Jobs that already trained on it keep their ` +
          `record, but it can't be used for new jobs and the stored archive is removed.`,
      )
    ) {
      return;
    }
    try {
      await remove.mutateAsync(dataset.id);
      toast({ title: `Deleted “${dataset.name}”` });
    } catch (err) {
      toast({
        title: "Couldn't delete that dataset",
        description: err instanceof ApiError ? err.message : undefined,
        variant: "destructive",
      });
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-xl font-semibold text-primary">Datasets</h1>
        <p className="mt-1 text-sm text-secondary">
          Your own image sets, available to train on alongside the built-in
          CIFAR-10 and MNIST.
        </p>
      </div>

      {admin && (
        <form
          onSubmit={(e) => void handleUpload(e)}
          className="flex flex-col gap-4 rounded-lg border border-hairline bg-panel p-5"
        >
          <div>
            <h2 className="text-sm font-semibold text-primary">Upload a dataset</h2>
            <p className="mt-1 text-xs text-secondary">
              A <code className="font-data">.zip</code> of class folders, with
              both splits:
            </p>
            <pre className="mt-2 overflow-x-auto rounded border border-hairline bg-base p-3 text-xs text-secondary font-data">
{`train/cat/anything.png
train/dog/anything.jpg
test/cat/held-out.png
test/dog/held-out.png`}
            </pre>
            <p className="mt-2 text-xs text-tertiary">
              You choose the test split yourself — it is what the reported
              accuracy is measured on, so it is never carved out of{" "}
              <code className="font-data">train/</code> behind your back.
            </p>
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="dataset-name">Name</Label>
              <Input
                id="dataset-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="flowers"
                pattern="[A-Za-z0-9][A-Za-z0-9._-]{2,127}"
                title="Letters, digits, dot, dash, underscore. At least 3 characters."
                required
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="dataset-description">Description (optional)</Label>
              <Input
                id="dataset-description"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="102 flower species, 64px"
              />
            </div>
          </div>

          <div className="flex flex-col gap-1.5">
            <Label htmlFor="dataset-file">Archive</Label>
            <Input
              id="dataset-file"
              ref={fileInputRef}
              type="file"
              accept=".zip,application/zip"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              required
            />
          </div>

          {formError && (
            <div
              role="alert"
              className="rounded-md border border-bad/40 bg-bad/10 px-3 py-2 text-sm text-primary"
            >
              {formError}
            </div>
          )}

          <div className="flex items-center gap-3">
            <Button type="submit" disabled={upload.isPending}>
              {upload.isPending ? "Uploading…" : "Upload dataset"}
            </Button>
            {upload.isPending && (
              <span className="text-xs text-secondary">
                Large archives take a while — the file is checked before it is stored.
              </span>
            )}
          </div>
        </form>
      )}

      {isPending && <Skeleton className="h-32 w-full" />}

      {error && (
        <ErrorState error={error} onRetry={() => void refetch()} />
      )}

      {data && data.length === 0 && (
        <EmptyState
          title="No datasets yet"
          description={
            admin
              ? "Upload one above to train on your own images."
              : "Ask an admin to upload one. Jobs can still use the built-in CIFAR-10 and MNIST."
          }
        />
      )}

      {data && data.length > 0 && (
        <div className="flex flex-col gap-3">
          {data.map((dataset) => (
            <article
              key={dataset.id}
              className="rounded-lg border border-hairline bg-panel p-4"
            >
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <h3 className="text-sm font-semibold text-primary">
                    {dataset.name}
                  </h3>
                  {dataset.description && (
                    <p className="mt-0.5 text-xs text-secondary">
                      {dataset.description}
                    </p>
                  )}
                  <p className="mt-1 text-xs text-secondary">
                    {dataset.num_classes} classes ·{" "}
                    {dataset.train_images.toLocaleString()} train ·{" "}
                    {dataset.test_images.toLocaleString()} test ·{" "}
                    {formatBytes(dataset.size_bytes)}
                  </p>
                  <p className="mt-1 text-xs text-tertiary">
                    by {dataset.created_by} · sha256{" "}
                    <span className="font-data">
                      {dataset.sha256.slice(0, 12)}…
                    </span>
                  </p>
                </div>
                {admin && (
                  <Button
                    variant="ghost"
                    onClick={() => void handleDelete(dataset)}
                    disabled={remove.isPending}
                  >
                    Delete
                  </Button>
                )}
              </div>

              <div className="mt-3 flex flex-wrap gap-1.5">
                {dataset.classes.map((className) => (
                  <span
                    key={className}
                    className="rounded border border-hairline px-1.5 py-0.5 text-xs text-secondary font-data"
                  >
                    {className}
                    <span className="ml-1 text-tertiary">
                      {dataset.per_class_counts.train?.[className] ?? 0}
                    </span>
                  </span>
                ))}
              </div>
            </article>
          ))}
        </div>
      )}
    </div>
  );
}
