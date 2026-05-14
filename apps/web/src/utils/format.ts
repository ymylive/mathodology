// Centralised i18n-aware formatters. Use these instead of ad-hoc
// `toLocaleTimeString("en-GB", ...)` or `n.toFixed(2)` so the en/zh
// switch in the top-right toggle actually propagates to the numbers and
// dates the user sees in the header, dashboard, and feed.
//
// Lang argument is passed explicitly rather than read from the i18n
// store so these stay usable from plain functions (no need to be a
// composable). Callers pass `i18n.lang`.

import type { Lang } from "@/composables/useI18n";

const BCP47: Record<Lang, string> = {
  en: "en-GB",
  zh: "zh-CN",
};

// HH:mm:ss in 24-hour form. The original EventLog used a hardcoded
// "en-GB" — same shape regardless of UI lang. We keep the 24-hour
// convention (engineers expect logs in 24h regardless of locale) but
// route through Intl so the digit script follows the active locale.
const TIME_HMS_FMT = new Map<Lang, Intl.DateTimeFormat>();
export function formatTimeHMS(iso: string, lang: Lang): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "--:--:--";
  let f = TIME_HMS_FMT.get(lang);
  if (!f) {
    f = new Intl.DateTimeFormat(BCP47[lang], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
    TIME_HMS_FMT.set(lang, f);
  }
  return f.format(d);
}

// HH:mm only. Used as the "started at" label in the run header.
const TIME_HM_FMT = new Map<Lang, Intl.DateTimeFormat>();
export function formatTimeHM(iso: string, lang: Lang): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "--:--";
  let f = TIME_HM_FMT.get(lang);
  if (!f) {
    f = new Intl.DateTimeFormat(BCP47[lang], {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
    TIME_HM_FMT.set(lang, f);
  }
  return f.format(d);
}

// "¥1,234.56" style. Numbers below 1000 don't show separators (Intl
// handles that). Fraction digits are explicit so callers control
// precision (header wants 3, dashboard wants 2).
const CURRENCY_FMT = new Map<string, Intl.NumberFormat>();
export function formatCurrency(
  n: number,
  lang: Lang,
  fractionDigits: number = 2,
): string {
  if (typeof n !== "number" || Number.isNaN(n)) return "¥—";
  const key = `${lang}:${fractionDigits}`;
  let f = CURRENCY_FMT.get(key);
  if (!f) {
    f = new Intl.NumberFormat(BCP47[lang], {
      minimumFractionDigits: fractionDigits,
      maximumFractionDigits: fractionDigits,
    });
    CURRENCY_FMT.set(key, f);
  }
  return `¥${f.format(n)}`;
}

// Percent display. We format the raw number (e.g. 87.5) and append %
// rather than using Intl's percent style (which expects 0..1).
const PERCENT_FMT = new Map<string, Intl.NumberFormat>();
export function formatPercent(
  n: number,
  lang: Lang,
  fractionDigits: number = 1,
): string {
  if (typeof n !== "number" || Number.isNaN(n)) return "—";
  const key = `${lang}:${fractionDigits}`;
  let f = PERCENT_FMT.get(key);
  if (!f) {
    f = new Intl.NumberFormat(BCP47[lang], {
      minimumFractionDigits: fractionDigits,
      maximumFractionDigits: fractionDigits,
    });
    PERCENT_FMT.set(key, f);
  }
  return `${f.format(n)}%`;
}

// Compact token-count display: "1.2k" past a thousand, raw integer
// below. Used in the per-agent usage chips.
const TOKEN_FMT = new Map<Lang, Intl.NumberFormat>();
export function formatTokenCount(n: number, lang: Lang): string {
  if (typeof n !== "number" || Number.isNaN(n)) return "—";
  let f = TOKEN_FMT.get(lang);
  if (!f) {
    f = new Intl.NumberFormat(BCP47[lang], {
      minimumFractionDigits: 1,
      maximumFractionDigits: 1,
    });
    TOKEN_FMT.set(lang, f);
  }
  if (n < 1000) return n.toString();
  return `${f.format(n / 1000)}k`;
}
