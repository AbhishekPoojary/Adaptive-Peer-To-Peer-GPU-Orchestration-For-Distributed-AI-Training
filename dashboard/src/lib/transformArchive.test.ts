/**
 * Streaming zip rebuild.
 *
 * This is the part of the shrink-before-upload path that can go wrong quietly.
 * The image codec either works or throws, and the wrapper falls back to the
 * original file when it throws. The zip surgery is different: a bug here
 * produces an archive that *looks* fine and is subtly wrong — entries lost,
 * names mangled, bytes reordered — and the first sign of it would be a training
 * run on a dataset that is not the one someone uploaded.
 *
 * Every case therefore reads the rebuilt archive back with an independent
 * unzip and compares against what went in, rather than trusting the writer's
 * own accounting.
 */

import { describe, expect, it } from "vitest";
import { unzipSync, zipSync, strToU8, strFromU8 } from "fflate";
import { transformArchive } from "./transformArchive";

function makeZip(entries: Record<string, Uint8Array>): Blob {
  return new Blob([zipSync(entries) as unknown as BlobPart]);
}

async function readBack(parts: Uint8Array[]): Promise<Record<string, Uint8Array>> {
  const blob = new Blob(parts as unknown as BlobPart[]);
  return unzipSync(new Uint8Array(await blob.arrayBuffer()));
}

const identity = async (name: string, bytes: Uint8Array) => ({ name, bytes });

describe("transformArchive", () => {
  it("rebuilds an archive entry for entry", async () => {
    const source = makeZip({
      "train/cat/a.png": strToU8("first"),
      "train/dog/b.png": strToU8("second"),
      "test/cat/c.png": strToU8("third"),
    });

    const result = await transformArchive(source, identity);
    const out = await readBack(result.parts);

    expect(Object.keys(out).sort()).toEqual([
      "test/cat/c.png",
      "train/cat/a.png",
      "train/dog/b.png",
    ]);
    expect(strFromU8(out["train/dog/b.png"])).toBe("second");
    expect(result.entriesIn).toBe(3);
    expect(result.entriesOut).toBe(3);
  });

  it("applies the rename and the new bytes", async () => {
    const source = makeZip({ "train/cat/a.png": strToU8("original") });

    const result = await transformArchive(source, async (name) => ({
      name: `${name.slice(0, name.lastIndexOf("."))}.jpg`,
      bytes: strToU8("replaced"),
    }));
    const out = await readBack(result.parts);

    expect(Object.keys(out)).toEqual(["train/cat/a.jpg"]);
    expect(strFromU8(out["train/cat/a.jpg"])).toBe("replaced");
  });

  it("drops an entry when the transform returns null", async () => {
    const source = makeZip({
      "train/cat/a.png": strToU8("keep"),
      "notes.txt": strToU8("drop"),
    });

    const result = await transformArchive(source, async (name, bytes) =>
      name.endsWith(".txt") ? null : { name, bytes },
    );
    const out = await readBack(result.parts);

    expect(Object.keys(out)).toEqual(["train/cat/a.png"]);
    expect(result.entriesIn).toBe(2);
    expect(result.entriesOut).toBe(1);
  });

  it("preserves bytes exactly across many and large entries", async () => {
    // Larger than one stream chunk, and incompressible, so this exercises the
    // multi-chunk path rather than a single convenient buffer.
    const entries: Record<string, Uint8Array> = {};
    for (let i = 0; i < 40; i += 1) {
      const payload = new Uint8Array(40_000);
      for (let j = 0; j < payload.length; j += 1) payload[j] = (i * 31 + j) % 251;
      entries[`train/class${i % 4}/img${i}.png`] = payload;
    }
    const source = makeZip(entries);

    const result = await transformArchive(source, identity);
    const out = await readBack(result.parts);

    expect(Object.keys(out)).toHaveLength(40);
    for (const [name, original] of Object.entries(entries)) {
      expect(Array.from(out[name])).toEqual(Array.from(original));
    }
  });

  it("reports progress that advances to the whole source", async () => {
    const entries: Record<string, Uint8Array> = {};
    for (let i = 0; i < 20; i += 1) {
      entries[`train/c/img${i}.png`] = new Uint8Array(30_000).fill(i);
    }
    const source = makeZip(entries);

    const seen: number[] = [];
    await transformArchive(source, identity, (p) => seen.push(p.bytesRead));

    expect(seen.length).toBeGreaterThan(0);
    expect(seen[seen.length - 1]).toBe(source.size);
    // Monotonic: a bar that goes backwards is worse than no bar.
    for (let i = 1; i < seen.length; i += 1) {
      expect(seen[i]).toBeGreaterThanOrEqual(seen[i - 1]);
    }
  });

  it("surfaces a transform failure instead of writing a partial archive", async () => {
    const source = makeZip({
      "train/cat/a.png": strToU8("one"),
      "train/dog/b.png": strToU8("two"),
    });

    await expect(
      transformArchive(source, async (name) => {
        if (name.endsWith("b.png")) throw new Error("codec exploded");
        return { name, bytes: strToU8("x") };
      }),
    ).rejects.toThrow("codec exploded");
  });

  it("handles an archive with no entries", async () => {
    const result = await transformArchive(makeZip({}), identity);
    expect(result.entriesIn).toBe(0);
    expect(await readBack(result.parts)).toEqual({});
  });
});
