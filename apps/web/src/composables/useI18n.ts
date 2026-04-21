// Bilingual switcher ported from reference `i18n.js`.
//
// Exposes a Pinia store that persists the choice under
// `localStorage['mathodology.lang']` and mirrors the active language onto
// `document.body.classList` so the `body.zh` CSS rules (font swaps, etc.)
// activate alongside the reactive t() helper used in templates.
//
// Usage:
//   const i18n = useI18n();
//   i18n.t('Overview', '概览')     // -> current string
//   i18n.toggle('zh')              // -> flip language + persist
//
// The `<T en zh />` component in components/T.vue is a thin template
// wrapper around `t()` for convenience.

import { defineStore } from "pinia";

export type Lang = "en" | "zh";

const LS_KEY = "mathodology.lang";

function loadInitial(): Lang {
  if (typeof window === "undefined") return "en";
  const saved = window.localStorage.getItem(LS_KEY);
  return saved === "zh" ? "zh" : "en";
}

function applyBodyClass(lang: Lang): void {
  if (typeof document === "undefined") return;
  document.body.classList.toggle("zh", lang === "zh");
}

export const useI18n = defineStore("i18n", {
  state: () => ({
    lang: loadInitial() as Lang,
  }),
  actions: {
    // Bootstrap from SSR-safe state. Idempotent; call once at app start.
    init() {
      applyBodyClass(this.lang);
    },
    toggle(next: Lang) {
      if (this.lang === next) return;
      this.lang = next;
      if (typeof window !== "undefined") {
        window.localStorage.setItem(LS_KEY, next);
      }
      applyBodyClass(next);
    },
    // Translate a literal pair. Picks the current language. Caller owns
    // the source strings — keeping t() pair-shaped (rather than a key
    // lookup table) means new strings don't require a second edit.
    t(en: string, zh: string): string {
      return this.lang === "zh" ? zh : en;
    },
  },
});
