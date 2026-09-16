import { useState, type FormEvent } from "react";
import {
  useDatasetsQuery,
  useDeleteDatasetMutation,
  useUploadDatasetMutation,
  useUploadLimitsQuery,
  servedThroughQuickTunnel,
  type Dataset,
  type UploadProgress,
} from "@/api/datasets";
import { canShrinkArchives } from "@/api/shrinkArchive";
import { ArchiveDropzone } from "@/components/ArchiveDropzone";
import { Switch } from "@/components/ui/switch";
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
 * Above this, mention that the public link will be slow.
 *
 * Nothing to do with whether the upload succeeds — chunking settled that. This
 * is about the clock. Measured on the link this was built for, the tunnel ran
 * at ~150 KB/s, so 25 MB is around three minutes and anything larger is worth
 * knowing about before starting rather than during.
 */
const TUNNEL_SLOW_BYTES = 25 * 1024 * 1024;

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
  const limits = useUploadLimitsQuery();
  const maxUploadBytes = limits.data?.max_upload_bytes ?? 2 * 1024 * 1024 * 1024;
  const admin = isAdmin();

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [dropError, setDropError] = useState<string | null>(null);
  // Kept on the page rather than left in a toast: these say what the server did
  // to someone's data, and a message that disappears after four seconds is not
  // a disclosure. The same text is written onto the dataset's description, so
  // dismissing this loses nothing permanent.
  const [layoutNotes, setLayoutNotes] = useState<string[]>([]);
  const [progress, setProgress] = useState<UploadProgress | null>(null);
  // Default on: for the connection this feature exists for it is a ~6x saving
  // at no cost to the model, and someone who has never thought about image
  // resolution should get the fast path without having to ask for it.
  const [shrinkImages, setShrinkImages] = useState(true);

  async function handleUpload(e: FormEvent) {
    e.preventDefault();
    setFormError(null);
    setLayoutNotes([]);
    setProgress(null);
    if (!file) {
      setFormError("Choose a .zip file to upload.");
      return;
    }
    try {
      const result = await upload.mutateAsync({
        name,
        description: description || undefined,
        file,
        shrinkImages: shrinkImages && canShrinkArchives(),
        onProgress: setProgress,
      });
      toast({
        title: `Uploaded “${result.dataset.name}”`,
        description: result.summary,
      });
      setLayoutNotes(result.layout_notes ?? []);
      setName("");
      setDescription("");
      setFile(null);
    } catch (err) {
      setFormError(
        err instanceof ApiError ? err.message : "Couldn't upload that dataset.",
      );
    } finally {
      // Cleared whichever way it ended, so a failed attempt never leaves a bar
      // sitting at 90% next to the error explaining it stopped.
      setProgress(null);
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
        <h1 className="text-[1.375rem] font-semibold tracking-[-0.02em] text-ink">Datasets</h1>
        <p className="mt-1.5 max-w-[68ch] text-[0.8125rem] leading-relaxed text-muted">
          Your own image sets, available to train on alongside the built-in
          CIFAR-10 and MNIST.
        </p>
      </div>

      {admin && (
        <form
          onSubmit={(e) => void handleUpload(e)}
          className="flex flex-col gap-4 rounded-[var(--radius-panel)] bg-surface shadow-panel p-5"
        >
          <div>
            <h2 className="text-[0.9375rem] font-semibold tracking-[-0.01em] text-ink">Upload a dataset</h2>
            <p className="max-w-[70ch] mt-1 text-xs text-secondary">
              A <code className="font-data">.zip</code> of class folders. This
              is the layout it is stored in:
            </p>
            <pre className="mt-3 overflow-x-auto rounded-[var(--radius-control)] bg-sunken p-3.5 font-data text-xs leading-relaxed text-muted">
{`train/cat/anything.png
train/dog/anything.jpg
test/cat/held-out.png
test/dog/held-out.png`}
            </pre>
            <p className="max-w-[70ch] mt-2 text-xs text-tertiary">
              Your archive does not have to look like that. Common layouts —{" "}
              <code className="font-data">seg_train/</code>,{" "}
              <code className="font-data">training/</code> and{" "}
              <code className="font-data">valid/</code>, a wrapping folder, or
              labels in the filenames — are rearranged into it for you, and you
              are told exactly what changed.
            </p>
            <p className="max-w-[70ch] mt-2 text-xs text-tertiary">
              Supply a test split if you can: it is what the reported accuracy
              is measured on, so it is worth choosing deliberately. If there
              isn&rsquo;t one, a portion of <code className="font-data">train/</code>{" "}
              is held out — and that is written onto the dataset, so anyone
              reading a result later can see the split was picked for you.
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

          <div className="flex flex-col gap-4">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="dataset-file">Archive</Label>
              <ArchiveDropzone
                id="dataset-file"
                file={file}
                onFile={setFile}
                maxBytes={maxUploadBytes}
                disabled={upload.isPending}
                error={dropError}
                onError={setDropError}
              />
            </div>

            {canShrinkArchives() && (
              <div className="flex items-start gap-3">
                <Switch
                  id="shrink-images"
                  checked={shrinkImages}
                  onCheckedChange={setShrinkImages}
                  disabled={upload.isPending}
                  className="mt-0.5"
                />
                <Label
                  htmlFor="shrink-images"
                  className="max-w-[72ch] cursor-pointer text-xs leading-relaxed font-normal text-muted"
                >
                  <span className="font-medium text-ink">
                    Shrink images before uploading.
                  </span>{" "}
                  Training resizes every image to a small fixed size anyway, so
                  doing it here sends far less over the network and changes
                  nothing the model sees. It is recorded on the dataset. Turn
                  this off to upload the archive exactly as it is.
                </Label>
              </div>
            )}

            {/*
              Not a warning that it will fail — it will not, since the upload is
              sent in pieces small enough that the tunnel's cut-off never
              applies. A warning about the clock, and only when shrinking is not
              going to deal with it anyway.
            */}
            {file &&
              !shrinkImages &&
              servedThroughQuickTunnel() &&
              file.size > TUNNEL_SLOW_BYTES && (
                <p className="text-xs text-warn">
                  {formatBytes(file.size)} over the public link will take a
                  while — it is sent in pieces so it will not be cut off, but
                  the link is only as fast as the host&rsquo;s upload speed.
                  Leaving the box above ticked is usually the better answer.
                </p>
              )}
          </div>

          {formError && (
            <div
              role="alert"
              className="rounded-[var(--radius-control)] bg-fault-wash px-3 py-2 text-sm text-primary"
            >
              {formError}
            </div>
          )}

          {layoutNotes.length > 0 && (
            <div
              role="status"
              className="rounded-[var(--radius-control)] border border-hairline bg-base px-3 py-2 text-sm text-primary"
            >
              <p className="font-semibold">
                Your archive was rearranged to fit the required layout
              </p>
              <ul className="mt-1.5 list-disc space-y-1 pl-5 text-xs text-secondary">
                {layoutNotes.map((note) => (
                  <li key={note}>{note}</li>
                ))}
              </ul>
              <p className="max-w-[70ch] mt-2 text-xs text-tertiary">
                This is recorded on the dataset&rsquo;s description as well, so it
                stays visible next to any accuracy measured against it.
              </p>
            </div>
          )}

          {upload.isPending && <UploadProgressBar progress={progress} />}

          <div className="flex items-center gap-3">
            <Button type="submit" disabled={upload.isPending}>
              {upload.isPending ? "Uploading…" : "Upload dataset"}
            </Button>
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
              className="rounded-[var(--radius-panel)] bg-surface shadow-panel p-5"
            >
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <h3 className="text-[0.9375rem] font-semibold tracking-[-0.01em] text-ink">
                    {dataset.name}
                  </h3>
                  {dataset.description && (
                    <p className="mt-1 max-w-[80ch] text-xs leading-relaxed text-muted">
                      {dataset.description}
                    </p>
                  )}
                  <p className="max-w-[70ch] mt-1 text-xs text-secondary">
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
                    variant="destructive"
                    size="sm"
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
                    className="rounded-full bg-sunken px-2 py-0.5 font-data text-xs text-muted"
                  >
                    {className}
                    <span className="ml-1.5 text-faint">
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

/**
 * How far an upload has got.
 *
 * Two states, because an archive of any size spends real time in each and only
 * one of them has a percentage. While bytes are going out there is a genuine
 * fraction to show; once they have all gone, the server is validating the zip,
 * possibly rearranging its layout, and pushing it to storage, and none of that
 * reports progress. Holding the bar at 100% through that second stretch would
 * answer "has it frozen?" with the exact picture of something frozen, so it
 * says what it is waiting for instead.
 */
function UploadProgressBar({ progress }: { progress: UploadProgress | null }) {
  const measurable =
    (progress?.phase === "uploading" || progress?.phase === "preparing") &&
    progress.total > 0;
  const percent = measurable
    ? Math.min(100, Math.round((progress.loaded / progress.total) * 100))
    : null;
  // Three distinct waits, told apart so none of them claims to be another.
  // Before the first progress event nothing has been sent yet, and saying the
  // archive is being checked at that point would be describing a step that has
  // not started.
  const label =
    progress === null
      ? "Starting…"
      : progress.phase === "preparing"
        ? measurable
          ? `Shrinking images — ${formatBytes(progress.loaded)} of ${formatBytes(progress.total)} read`
          : "Shrinking images…"
        : progress.phase === "processing"
          ? "Checking the archive and storing it…"
          : measurable
            ? `Uploading — ${formatBytes(progress.loaded)} of ${formatBytes(progress.total)}`
            : "Uploading…";

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-baseline justify-between gap-3 text-xs">
        <span className="text-secondary">{label}</span>
        {percent !== null && (
          <span className="font-data text-secondary tabular-nums">{percent}%</span>
        )}
      </div>
      <div
        role="progressbar"
        aria-label="Dataset upload"
        // Omitted entirely while indeterminate: a progressbar with no
        // aria-valuenow is how the platform expresses "working, extent
        // unknown", whereas leaving a stale number there would announce a
        // figure that has stopped being true.
        aria-valuemin={percent === null ? undefined : 0}
        aria-valuemax={percent === null ? undefined : 100}
        aria-valuenow={percent ?? undefined}
        className="h-1.5 w-full overflow-hidden rounded-full bg-base"
      >
        <div
          className={
            percent === null
              ? "h-full w-1/3 animate-pulse rounded-full bg-accent/70"
              : "h-full rounded-full bg-accent transition-[width] duration-200"
          }
          style={percent === null ? undefined : { width: `${percent}%` }}
        />
      </div>
      <p className="text-xs text-tertiary">
        A large archive takes a while. Leave this page open until it finishes —
        it resumes where it left off if a piece fails, but not if the tab closes.
      </p>
    </div>
  );
}
