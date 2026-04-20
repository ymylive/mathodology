import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

// Canonical shadcn-vue `cn()` helper: merge conditional class lists with
// clsx, then resolve conflicting Tailwind utilities with tailwind-merge.
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
