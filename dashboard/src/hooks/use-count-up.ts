import { useEffect, useRef, useState } from "react";

/**
 * Count a figure up to its value on mount and on change.
 *
 * The idiom this dashboard was rebuilt into animates its headline numbers, and
 * the motion earns its place here for a specific reason: these figures change
 * on a poll, and a digit that counts to its new value reads as a measurement
 * arriving, where a digit that simply swaps reads as the page reloading.
 *
 * Exponential ease-out, matching `--ease-out-expo`, so it decelerates into the
 * real value rather than ticking linearly.
 *
 * The animated value lives in state only while a run is in flight; the rest of
 * the time the target is returned straight through. That keeps the hook from
 * writing state synchronously inside its own effect, which would cascade
 * renders, and it means reduced-motion and non-numeric cases cost nothing.
 */
export function useCountUp(target: number | null, durationMs = 620): number | null {
  const [inFlight, setInFlight] = useState<number | null>(null);
  const fromRef = useRef(0);

  useEffect(() => {
    if (target === null || !Number.isFinite(target)) return;

    const reduced =
      typeof window !== "undefined" &&
      window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    const from = fromRef.current;
    fromRef.current = target;

    // Below this a count-up is a twitch rather than a reading, so the value
    // simply arrives.
    if (reduced || Math.abs(target - from) < 2) return;

    let frame = 0;
    const started = performance.now();
    const tick = (now: number) => {
      const t = Math.min(1, (now - started) / durationMs);
      const eased = 1 - Math.pow(1 - t, 4);
      if (t < 1) {
        setInFlight(from + (target - from) * eased);
        frame = requestAnimationFrame(tick);
      } else {
        // Hand the figure back to the target rather than leaving a rounded
        // copy of it in state.
        setInFlight(null);
      }
    };
    frame = requestAnimationFrame(tick);

    return () => {
      cancelAnimationFrame(frame);
      setInFlight(null);
    };
  }, [target, durationMs]);

  return inFlight ?? target;
}
