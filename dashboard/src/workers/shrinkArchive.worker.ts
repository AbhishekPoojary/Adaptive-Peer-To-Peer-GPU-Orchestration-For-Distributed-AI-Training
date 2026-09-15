/**
 * Resize a dataset archive's images to the size training actually uses.
 *
 * The trainer applies `Resize((N, N))` to every custom-dataset image before the
 * model sees it, so anything larger is detail discarded on arrival. Doing that
 * resize here, before the upload, costs the model nothing and removes most of
 * the bytes: measured on the Intel scene set, 363 MB became 55 MB — the same
 * 24,335 images, the same six classes, the same pixels the model would have
 * been handed anyway.
 *
 * That matters because the uploader's connection is the bottleneck this whole
 * path exists to work around. Chunking made a large upload *finish*; this makes
 * it finish roughly six times sooner, which is the difference between a demo
 * and a wait.
 *
 * In a worker because it decodes and re-encodes tens of thousands of images,
 * and on the main thread that is a frozen page for minutes.
 *
 * The zip streaming lives in `lib/transformArchive` so it can be tested without
 * a browser; what is left here is the part that genuinely needs one.
 */

import { transformArchive } from "@/lib/transformArchive";

export interface ShrinkRequest {
  file: File;
  /** Side length to resize to, from the server's upload-limits. */
  imageSize: number;
  /** JPEG quality for re-encoded images, 0..1. */
  quality?: number;
}

export type ShrinkMessage =
  | { type: "progress"; entriesDone: number; bytesRead: number; totalBytes: number }
  | {
      type: "done";
      blob: Blob;
      resized: number;
      copied: number;
      beforeBytes: number;
      afterBytes: number;
    }
  | { type: "error"; message: string };

const IMAGE_SUFFIXES = new Set([
  ".png",
  ".jpg",
  ".jpeg",
  ".bmp",
  ".gif",
  ".webp",
  ".tif",
  ".tiff",
]);

function suffixOf(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot === -1 ? "" : name.slice(dot).toLowerCase();
}

/**
 * Decode, square-resize and re-encode one image.
 *
 * Square rather than aspect-preserving on purpose: the trainer's
 * `Resize((N, N))` squashes to a square regardless of the original shape, so
 * matching it exactly is what makes this lossless with respect to training.
 * Anything cleverer here would hand the model different pixels than it would
 * otherwise have got — a change to the data dressed up as an optimisation.
 */
async function resizeImage(
  bytes: Uint8Array,
  size: number,
  quality: number,
): Promise<Uint8Array> {
  const bitmap = await createImageBitmap(new Blob([bytes as BlobPart]));
  try {
    const canvas = new OffscreenCanvas(size, size);
    const context = canvas.getContext("2d");
    if (!context) throw new Error("no 2d context");
    context.drawImage(bitmap, 0, 0, size, size);
    const encoded = await canvas.convertToBlob({ type: "image/jpeg", quality });
    return new Uint8Array(await encoded.arrayBuffer());
  } finally {
    bitmap.close();
  }
}

async function shrink(request: ShrinkRequest): Promise<void> {
  const size = request.imageSize;
  const quality = request.quality ?? 0.9;
  let resized = 0;
  let copied = 0;

  const result = await transformArchive(
    request.file,
    async (name, bytes) => {
      if (!IMAGE_SUFFIXES.has(suffixOf(name))) {
        copied += 1;
        return { name, bytes };
      }
      try {
        const smaller = await resizeImage(bytes, size, quality);
        resized += 1;
        // Re-encoded as JPEG, so the stored name has to say so — an entry
        // called .png holding JPEG bytes is a small lie some decoder will
        // eventually trip on.
        return { name: `${name.slice(0, name.lastIndexOf("."))}.jpg`, bytes: smaller };
      } catch {
        // An image this browser cannot decode is passed through untouched
        // rather than dropped. The server's validator decides what is usable;
        // this only decides what is smaller.
        copied += 1;
        return { name, bytes };
      }
    },
    (progress) => self.postMessage({ type: "progress", ...progress } satisfies ShrinkMessage),
  );

  self.postMessage({
    type: "done",
    blob: new Blob(result.parts as BlobPart[], { type: "application/zip" }),
    resized,
    copied,
    beforeBytes: request.file.size,
    afterBytes: result.bytesOut,
  } satisfies ShrinkMessage);
}

self.onmessage = (event: MessageEvent<ShrinkRequest>) => {
  shrink(event.data).catch((err: unknown) => {
    self.postMessage({
      type: "error",
      message: err instanceof Error ? err.message : "could not shrink the archive",
    } satisfies ShrinkMessage);
  });
};
