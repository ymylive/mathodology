// Tween a number from its current displayed value to a new target with
// easeOutCubic. Re-calling with a new target resumes from the currently
// displayed value (not 0) so live cost/token updates feel continuous
// rather than snapping back to zero.
//
// Honors prefers-reduced-motion: snaps directly to target.
import { onBeforeUnmount, ref, watch, type Ref } from "vue";

interface Options {
  duration?: number;
  // If a `start` trigger ref is provided, the tween only runs once the
  // trigger flips truthy. Useful for "count up when the strip enters the
  // viewport" where the numeric target is already known at mount.
  trigger?: Ref<boolean>;
}

function prefersReducedMotion(): boolean {
  if (typeof window === "undefined") return false;
  return window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
}

function easeOutCubic(t: number): number {
  const p = 1 - t;
  return 1 - p * p * p;
}

export function useCountUp(target: Ref<number>, opts: Options = {}) {
  const duration = opts.duration ?? 700;
  const value = ref<number>(0);

  let raf: number | null = null;
  let from = 0;
  let to = 0;
  let t0 = 0;

  function cancel() {
    if (raf !== null) {
      cancelAnimationFrame(raf);
      raf = null;
    }
  }

  function step(now: number) {
    const elapsed = now - t0;
    const progress = Math.min(1, elapsed / duration);
    value.value = from + (to - from) * easeOutCubic(progress);
    if (progress < 1) {
      raf = requestAnimationFrame(step);
    } else {
      raf = null;
    }
  }

  function run(next: number) {
    cancel();
    if (prefersReducedMotion() || duration <= 0) {
      value.value = next;
      return;
    }
    from = value.value;
    to = next;
    t0 = performance.now();
    raf = requestAnimationFrame(step);
  }

  // If the caller gates the first run on a viewport trigger, stay at 0
  // until the trigger flips true. After that we mirror target changes.
  const gated = opts.trigger;
  if (gated) {
    watch(
      [gated, target],
      ([g, t]) => {
        if (!g) return;
        run(t);
      },
      { immediate: true },
    );
  } else {
    watch(
      target,
      (t) => {
        run(t);
      },
      { immediate: true },
    );
  }

  onBeforeUnmount(cancel);

  return value;
}
