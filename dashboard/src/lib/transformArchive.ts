/**
 * Rebuild a zip, entry by entry, without holding it in memory.
 *
 * Split out from the worker that uses it for one reason: everything here is
 * plain streams and bytes, so it can be tested. The image decoding it exists to
 * serve cannot — `createImageBitmap` and `OffscreenCanvas` are browser-only —
 * and folding the two together would have made the zip plumbing untestable by
 * association. The transform is injected instead, so the hard part is exercised
 * against a real archive and the browser part stays a thin shell.
 *
 * ## Why streaming
 *
 * The obvious version unzips into an array, maps it, and re-zips. That holds
 * the whole decompressed archive at once, which for a 363 MB dataset on the
 * modest laptop this project is *for* is a tab crash rather than an upload.
 * Here, source chunks are pushed through a streaming unzip, each entry is
 * transformed as it completes, and the reader waits on that work before pulling
 * more. Peak memory is roughly the output plus a handful of entries.
 */

import { Unzip, UnzipInflate, Zip, ZipPassThrough } from "fflate";

/** What to do with one entry. Return the new name and bytes, or null to drop it. */
export type EntryTransform = (
  name: string,
  bytes: Uint8Array,
) => Promise<{ name: string; bytes: Uint8Array } | null>;

export interface TransformProgress {
  entriesDone: number;
  bytesRead: number;
  totalBytes: number;
}

export interface TransformResult {
  parts: Uint8Array[];
  bytesOut: number;
  entriesIn: number;
  entriesOut: number;
}

function concat(parts: Uint8Array[], total: number): Uint8Array {
  const out = new Uint8Array(total);
  let at = 0;
  for (const part of parts) {
    out.set(part, at);
    at += part.length;
  }
  return out;
}

export async function transformArchive(
  source: Blob,
  transform: EntryTransform,
  onProgress?: (progress: TransformProgress) => void,
): Promise<TransformResult> {
  const totalBytes = source.size;
  const parts: Uint8Array[] = [];
  let bytesOut = 0;
  let entriesIn = 0;
  let entriesOut = 0;
  let bytesRead = 0;
  let failure: Error | null = null;

  const zip = new Zip((err, chunk) => {
    if (err) {
      failure ??= err;
      return;
    }
    if (chunk.length) {
      parts.push(chunk);
      bytesOut += chunk.length;
    }
  });

  async function handleEntry(name: string, bytes: Uint8Array): Promise<void> {
    entriesIn += 1;
    const replacement = await transform(name, bytes);
    if (!replacement) return;
    // Stored, not deflated: the entries this is used for are already-compressed
    // images, where deflate spends CPU per file for a fraction of a percent.
    const entry = new ZipPassThrough(replacement.name);
    zip.add(entry);
    entry.push(replacement.bytes, true);
    entriesOut += 1;
  }

  const unzip = new Unzip();
  unzip.register(UnzipInflate);

  // Chained, not fired in parallel, so the reader below can wait on it. That
  // chain *is* the backpressure: without it a fast disk read would queue every
  // entry in the archive before the first transform finished, which is exactly
  // the memory blow-up this design exists to avoid.
  let chain: Promise<void> = Promise.resolve();

  unzip.onfile = (entry) => {
    if (entry.name.endsWith("/")) return;
    const chunks: Uint8Array[] = [];
    let length = 0;
    entry.ondata = (err, chunk, final) => {
      if (err) {
        failure ??= err;
        return;
      }
      if (chunk.length) {
        chunks.push(chunk);
        length += chunk.length;
      }
      if (final) {
        const bytes = concat(chunks, length);
        chunks.length = 0;
        chain = chain.then(() => handleEntry(entry.name, bytes));
      }
    };
    entry.start();
  };

  const reader = source.stream().getReader();
  for (;;) {
    const { done, value } = await reader.read();
    if (failure) throw failure;
    if (done) {
      unzip.push(new Uint8Array(0), true);
      break;
    }
    bytesRead += value.length;
    unzip.push(value);
    await chain;
    onProgress?.({ entriesDone: entriesIn, bytesRead, totalBytes });
  }

  await chain;
  if (failure) throw failure;
  zip.end();
  if (failure) throw failure;

  return { parts, bytesOut, entriesIn, entriesOut };
}
