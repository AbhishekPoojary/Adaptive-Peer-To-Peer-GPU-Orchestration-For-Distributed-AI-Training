import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** shadcn/ui's canonical class-combining helper. */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
