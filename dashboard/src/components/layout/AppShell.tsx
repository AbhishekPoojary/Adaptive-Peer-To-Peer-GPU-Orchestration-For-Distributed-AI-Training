import { useState } from "react";
import { Outlet } from "react-router-dom";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { Menu, X } from "lucide-react";
import { SidebarNav } from "@/components/layout/Sidebar";
import { Toaster } from "@/components/ui/toaster";

/**
 * Navigation shell: persistent ~220px left sidebar at >=768px (md), collapsing
 * to a hamburger + slide-in drawer below that (our call for the "collapsible
 * on narrow viewports" requirement — documented in ADR-011 / the milestone
 * report rather than a bottom-nav bar, since 5 destinations read better as a
 * list than as cramped bottom-nav icons at 375px).
 */
export function AppShell() {
  const [drawerOpen, setDrawerOpen] = useState(false);

  return (
    <div className="flex min-h-screen bg-base text-primary">
      {/* Persistent sidebar, >=768px */}
      <aside className="hidden md:flex md:w-[220px] md:shrink-0 md:flex-col md:border-r md:border-hairline md:bg-panel">
        <Brand />
        <SidebarNav />
      </aside>

      {/* Mobile top bar, <768px */}
      <div className="fixed inset-x-0 top-0 z-40 flex h-12 items-center gap-2 border-b border-hairline bg-panel px-3 md:hidden">
        <button
          type="button"
          aria-label="Open navigation menu"
          onClick={() => setDrawerOpen(true)}
          className="rounded p-1.5 text-secondary outline-none hover:text-primary focus-visible:ring-2 focus-visible:ring-accent"
        >
          <Menu className="size-5" />
        </button>
        <span className="text-sm font-semibold text-primary">GPU Orchestrator</span>
      </div>

      {/* Mobile drawer */}
      <DialogPrimitive.Root open={drawerOpen} onOpenChange={setDrawerOpen}>
        <DialogPrimitive.Portal>
          <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-black/60 md:hidden" />
          <DialogPrimitive.Content
            className="fixed inset-y-0 left-0 z-50 flex w-64 flex-col border-r border-hairline bg-panel outline-none md:hidden"
            aria-describedby={undefined}
          >
            <DialogPrimitive.Title className="sr-only">
              Navigation menu
            </DialogPrimitive.Title>
            <div className="flex items-center justify-between px-3 py-3">
              <Brand compact />
              <DialogPrimitive.Close
                aria-label="Close navigation menu"
                className="rounded p-1.5 text-secondary outline-none hover:text-primary focus-visible:ring-2 focus-visible:ring-accent"
              >
                <X className="size-5" />
              </DialogPrimitive.Close>
            </div>
            <SidebarNav onNavigate={() => setDrawerOpen(false)} />
          </DialogPrimitive.Content>
        </DialogPrimitive.Portal>
      </DialogPrimitive.Root>

      <main className="min-w-0 flex-1 pt-12 md:pt-0">
        <div className="mx-auto max-w-6xl px-4 py-5 sm:px-6">
          <Outlet />
        </div>
      </main>

      <Toaster />
    </div>
  );
}

function Brand({ compact }: { compact?: boolean }) {
  return (
    <div className={compact ? "" : "border-b border-hairline px-3 py-3.5"}>
      <span className="text-sm font-semibold tracking-tight text-primary">
        GPU Orchestrator
      </span>
    </div>
  );
}
