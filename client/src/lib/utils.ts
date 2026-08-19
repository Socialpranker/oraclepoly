import { type ClassValue, clsx } from "clsx"
import { twMerge } from "tailwind-merge"

/** Утилита для слияния Tailwind классов с корректным разрешением конфликтов */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}
