import { useCallback } from "react";
import { useDropzone, type FileRejection } from "react-dropzone";
import { FileArchive, UploadCloud, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { formatBytes } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * Drop target for a dataset archive.
 *
 * Adapted from the file-upload idiom on 21st.dev — `react-dropzone`'s
 * `isDragActive`, the dashed drop area, the chosen-file row with its size and
 * a remove control. Three things are deliberately different:
 *
 * - **One archive, not many.** A dataset is a single zip. A multi-file list
 *   would offer a capability the endpoint does not have.
 * - **Real constraints, from the server.** The reference prints "All file types
 *   are allowed" and a hardcoded 50 MB. This states the actual rule: a `.zip`,
 *   under the orchestrator's configured ceiling, which is passed in rather
 *   than guessed at.
 * - **Our tokens.** The reference paints with shadcn's `primary`/`border`
 *   defaults, which on this palette would introduce a colour belonging to no
 *   part of the system.
 *
 * A rejected drop says why. `react-dropzone` hands back the reason, and
 * discarding it would leave someone dropping a `.tar.gz` with a target that
 * simply refuses to respond.
 */
export function ArchiveDropzone({
  id,
  file,
  onFile,
  maxBytes,
  disabled,
  error,
  onError,
}: {
  /**
   * Id for the hidden file input, so a `<Label htmlFor>` outside this
   * component still opens the picker. Omitting it is what broke the click:
   * the label pointed at an input that no longer existed.
   */
  id?: string;
  file: File | null;
  onFile: (file: File | null) => void;
  /** The orchestrator's own upload ceiling, so the copy cannot drift from it. */
  maxBytes: number;
  disabled?: boolean;
  error?: string | null;
  onError?: (message: string | null) => void;
}) {
  const onDrop = useCallback(
    (accepted: File[], rejected: FileRejection[]) => {
      if (rejected.length > 0) {
        const reason = rejected[0].errors[0];
        onError?.(
          reason?.code === "file-too-large"
            ? `That archive is larger than ${formatBytes(maxBytes)}.`
            : reason?.code === "file-invalid-type"
              ? "That is not a .zip archive."
              : (reason?.message ?? "That file cannot be uploaded."),
        );
        return;
      }
      if (accepted[0]) {
        onError?.(null);
        onFile(accepted[0]);
      }
    },
    [maxBytes, onError, onFile],
  );

  const { getRootProps, getInputProps, isDragActive, isDragReject } = useDropzone({
    onDrop,
    multiple: false,
    maxSize: maxBytes,
    accept: { "application/zip": [".zip"], "application/x-zip-compressed": [".zip"] },
    disabled,
  });

  if (file) {
    return (
      <div className="flex items-center gap-3 rounded-[var(--radius-control)] bg-sunken p-3">
        <input {...getInputProps({ id })} />
        <span
          aria-hidden="true"
          className="grid size-9 shrink-0 place-items-center rounded-[8px] bg-surface text-muted"
        >
          <FileArchive className="size-4" />
        </span>
        <div className="min-w-0 flex-1">
          <p className="truncate text-[0.8125rem] font-medium text-ink">{file.name}</p>
          <p className="font-data text-xs text-muted">{formatBytes(file.size)}</p>
        </div>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label={`Remove ${file.name}`}
          disabled={disabled}
          onClick={() => {
            onFile(null);
            onError?.(null);
          }}
        >
          <X aria-hidden="true" />
        </Button>
      </div>
    );
  }

  return (
    <div>
      <div
        {...getRootProps()}
        className={cn(
          "flex cursor-pointer flex-col items-center justify-center rounded-[var(--radius-control)] border border-dashed px-6 py-9 text-center transition-[background-color,border-color] duration-200 ease-out motion-reduce:transition-none",
          isDragReject
            ? "border-fault bg-fault-wash"
            : isDragActive
              ? "border-accent bg-accent-wash"
              : "border-hairline-strong bg-sunken/60 hover:border-accent hover:bg-accent-wash/50",
          disabled && "pointer-events-none opacity-60",
        )}
      >
        <input {...getInputProps({ id })} />
        <UploadCloud
          className={cn(
            "size-6 transition-colors duration-200",
            isDragActive ? "text-accent" : "text-faint",
          )}
          aria-hidden="true"
        />
        <p className="mt-3 text-[0.8125rem] text-ink">
          {isDragReject
            ? "That is not a .zip archive"
            : isDragActive
              ? "Drop it here"
              : "Drag your archive here, or click to choose"}
        </p>
        <p className="mt-1 text-xs text-muted">
          A single <code className="font-data">.zip</code>, up to{" "}
          {formatBytes(maxBytes)}
        </p>
      </div>
      {error && (
        <p role="alert" className="mt-2 text-xs text-fault">
          {error}
        </p>
      )}
    </div>
  );
}
