import * as React from "react";

/** Minimal shadcn-pattern toast store: an in-memory reducer plus a
 * module-level listener list so `toast()` can be called from anywhere
 * (mutation success handlers, etc.) without prop-drilling a dispatcher. */

export interface ToastItem {
  id: string;
  title?: string;
  description?: string;
  variant?: "default" | "success" | "destructive";
  open: boolean;
}

type Listener = (toasts: ToastItem[]) => void;

let toasts: ToastItem[] = [];
const listeners: Listener[] = [];
let idCounter = 0;

function emit() {
  for (const listener of listeners) listener(toasts);
}

function dismiss(id: string) {
  toasts = toasts.map((t) => (t.id === id ? { ...t, open: false } : t));
  emit();
  window.setTimeout(() => {
    toasts = toasts.filter((t) => t.id !== id);
    emit();
  }, 200);
}

export function toast(input: Omit<ToastItem, "id" | "open">) {
  const id = String(++idCounter);
  toasts = [...toasts, { ...input, id, open: true }];
  emit();
  window.setTimeout(() => dismiss(id), 5000);
  return id;
}

export function useToast() {
  const [state, setState] = React.useState<ToastItem[]>(toasts);

  React.useEffect(() => {
    listeners.push(setState);
    return () => {
      const idx = listeners.indexOf(setState);
      if (idx >= 0) listeners.splice(idx, 1);
    };
  }, []);

  return { toasts: state, dismiss };
}
