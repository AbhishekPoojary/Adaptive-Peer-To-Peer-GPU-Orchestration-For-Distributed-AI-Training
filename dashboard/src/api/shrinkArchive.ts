/**
 * Shrink a dataset archive before uploading it, or leave it alone.
 *
 * Wraps the worker in the one guarantee that matters for something running on
 * hardware nobody here has seen: **it cannot lose you an upload.** Any failure
 * — an unsupported browser, a decode error, a worker that never loads — returns
 * the original file, and the upload proceeds exactly as it would have. Shrinking
 * is an optimisation, and an optimisation that can break the thing it optimises
 * is not one.
 */

import type { ShrinkMessage, ShrinkRequest } from "@/workers/shrinkArchive.worker";

export interface ShrinkOutcome {
  /** What to upload — the shrunk archive, or the original if it was left alone. */
  file: File | Blob;
  /** Empty when nothing was changed; the upload is then the file as chosen. */
  notes: string[];
}

export interface ShrinkProgress {
  entriesDone: number;
  bytesRead: number;
  totalBytes: number;
}

/**
 * True when this browser can do the work at all.
 *
 * `OffscreenCanvas.convertToBlob` is the narrow one — Safari was late to it,
 * and a missing method there would otherwise surface as every image failing to
 * encode, one slow failure at a time.
 */
export function canShrinkArchives(): boolean {
  return (
    typeof Worker !== "undefined" &&
    typeof OffscreenCanvas !== "undefined" &&
    typeof createImageBitmap === "function" &&
    typeof OffscreenCanvas.prototype.convertToBlob === "function"
  );
}

export function shrinkArchive(
  file: File,
  imageSize: number,
  onProgress?: (progress: ShrinkProgress) => void,
): Promise<ShrinkOutcome> {
  if (!canShrinkArchives()) return Promise.resolve({ file, notes: [] });

  return new Promise<ShrinkOutcome>((resolve) => {
    let worker: Worker;
    try {
      worker = new Worker(
        new URL("../workers/shrinkArchive.worker.ts", import.meta.url),
        { type: "module" },
      );
    } catch {
      resolve({ file, notes: [] });
      return;
    }

    // Resolve once, whichever way this ends.
    let settled = false;
    const finish = (outcome: ShrinkOutcome) => {
      if (settled) return;
      settled = true;
      worker.terminate();
      resolve(outcome);
    };

    worker.onmessage = (event: MessageEvent<ShrinkMessage>) => {
      const message = event.data;
      if (message.type === "progress") {
        onProgress?.(message);
        return;
      }
      if (message.type === "error") {
        finish({ file, notes: [] });
        return;
      }
      // Bigger than we started with means the re-encode helped nothing — an
      // archive of already-tiny images, say. Sending the original is then both
      // faster and honest, since nothing needs disclosing.
      if (message.afterBytes >= message.beforeBytes) {
        finish({ file, notes: [] });
        return;
      }
      finish({
        file: message.blob,
        notes: [
          `images resized to ${imageSize}x${imageSize} before upload, the size ` +
            `training uses (${formatMb(message.beforeBytes)} to ` +
            `${formatMb(message.afterBytes)})`,
        ],
      });
    };
    worker.onerror = () => finish({ file, notes: [] });
    worker.onmessageerror = () => finish({ file, notes: [] });

    worker.postMessage({ file, imageSize } satisfies ShrinkRequest);
  });
}

function formatMb(bytes: number): string {
  return `${Math.round(bytes / (1024 * 1024))} MB`;
}
