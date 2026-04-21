// Minimal IntersectionObserver wrapper. Once-only by default: after the
// first intersection we disconnect so the callback can't re-fire.
//
// Not using @vueuse/core — one caller, 20 lines of raw code, zero deps.
import { onBeforeUnmount, onMounted, type Ref } from "vue";

interface Options {
  threshold?: number;
  once?: boolean;
}

export function useInView(
  target:
    | Ref<Element | null>
    | Ref<Element[] | null>
    | Ref<HTMLElement[]>
    | Ref<HTMLElement | null>,
  cb: (el: Element) => void,
  opts: Options = {},
) {
  const threshold = opts.threshold ?? 0.25;
  const once = opts.once ?? true;
  let observer: IntersectionObserver | null = null;

  onMounted(() => {
    if (typeof window === "undefined" || typeof IntersectionObserver === "undefined") {
      // SSR / old browser fallback: fire immediately.
      const v = target.value;
      if (Array.isArray(v)) v.forEach((el) => cb(el));
      else if (v) cb(v);
      return;
    }

    observer = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (!e.isIntersecting) continue;
          cb(e.target);
          if (once) observer?.unobserve(e.target);
        }
      },
      { threshold },
    );

    const v = target.value;
    if (Array.isArray(v)) v.forEach((el) => observer?.observe(el));
    else if (v) observer.observe(v);
  });

  onBeforeUnmount(() => {
    observer?.disconnect();
    observer = null;
  });
}
