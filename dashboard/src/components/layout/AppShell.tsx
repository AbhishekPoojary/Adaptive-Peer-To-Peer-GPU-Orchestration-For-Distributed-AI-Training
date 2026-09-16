import { Outlet, useNavigate } from "react-router-dom";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { ChevronDown, LogOut } from "lucide-react";
import { useLogout } from "@/api/auth";
import { getUser } from "@/api/session";
import { TopNav } from "@/components/layout/TopNav";
import { Toaster } from "@/components/ui/toaster";

/**
 * Application shell.
 *
 * One header band on the canvas, then content. The header does not float, does
 * not blur and is not sticky: this is a surface people read tables on, and a
 * translucent bar over a scrolling table is a decoration that costs
 * legibility. It sits on the tinted canvas with a single hairline under it.
 */
export function AppShell() {
  return (
    <div className="flex min-h-screen flex-col bg-canvas text-ink">
      <header className="border-b border-hairline bg-canvas">
        <div className="mx-auto flex max-w-[1240px] flex-col gap-3 px-4 py-3 sm:px-6 md:flex-row md:items-center md:gap-6">
          <div className="flex items-center justify-between gap-3">
            <Brand />
            <div className="md:hidden">
              <AccountMenu />
            </div>
          </div>
          <div className="min-w-0 md:flex-1">
            <TopNav />
          </div>
          <div className="hidden md:block">
            <AccountMenu />
          </div>
        </div>
      </header>

      <main className="min-w-0 flex-1">
        <div className="mx-auto max-w-[1240px] px-4 py-7 sm:px-6">
          <Outlet />
        </div>
      </main>

      <Toaster />
    </div>
  );
}

function Brand() {
  return (
    <div className="flex items-center gap-2">
      {/* Drawn, not an emoji or a glyph standing in for a mark: three bars of
          unequal height, which is what this product is — borrowed machines of
          unequal capability doing one job together. */}
      <svg
        viewBox="0 0 20 20"
        className="size-[19px] shrink-0"
        aria-hidden="true"
        fill="none"
      >
        <rect x="2" y="9" width="4" height="9" rx="1.4" fill="var(--nosignal)" />
        <rect x="8" y="5" width="4" height="13" rx="1.4" fill="var(--accent)" />
        <rect x="14" y="2" width="4" height="16" rx="1.4" fill="var(--ink)" />
      </svg>
      <span className="text-[0.9375rem] font-semibold tracking-[-0.015em] text-ink">
        Orchestrator
      </span>
    </div>
  );
}

/**
 * Who you are signed in as, and the way out.
 *
 * The role is shown because it changes what the interface offers — only an
 * ADMIN can upload a dataset or enrol a machine. Without it, someone hitting a
 * 403 would have no way to tell why.
 */
function AccountMenu() {
  const user = getUser();
  const logout = useLogout();
  const navigate = useNavigate();

  if (!user) return null;

  const initial = user.username.slice(0, 1).toUpperCase();

  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger className="group flex items-center gap-2 rounded-full py-1 pr-2 pl-1 text-left transition-colors duration-150 ease-out hover:bg-sunken">
        <span
          aria-hidden="true"
          className="grid size-7 shrink-0 place-items-center rounded-full bg-ink text-[0.6875rem] font-semibold text-white"
        >
          {initial}
        </span>
        <span className="hidden min-w-0 sm:block">
          <span className="block max-w-[11ch] truncate text-[0.8125rem] leading-4 font-medium text-ink">
            {user.username}
          </span>
          <span className="block text-[0.6875rem] leading-3 text-faint">
            {user.role.toLowerCase()}
          </span>
        </span>
        <ChevronDown
          className="size-3.5 shrink-0 text-faint transition-transform duration-150 ease-out group-data-[state=open]:rotate-180"
          aria-hidden="true"
        />
      </DropdownMenu.Trigger>

      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={8}
          className="pop-in z-50 w-56 origin-[var(--radix-dropdown-menu-content-transform-origin)] rounded-[var(--radius-control)] border border-hairline bg-surface p-1 shadow-raised"
        >
          <div className="px-2.5 py-2">
            <p className="truncate text-[0.8125rem] font-medium text-ink">
              {user.username}
            </p>
            <p className="text-xs text-muted">
              Signed in as {user.role.toLowerCase()}
            </p>
          </div>
          <DropdownMenu.Separator className="my-1 h-px bg-hairline" />
          <DropdownMenu.Item
            onSelect={() => {
              logout();
              navigate("/login", { replace: true });
            }}
            className="flex cursor-pointer items-center gap-2 rounded-[7px] px-2.5 py-2 text-[0.8125rem] text-ink outline-none select-none data-[highlighted]:bg-sunken"
          >
            <LogOut className="size-4 shrink-0 text-muted" aria-hidden="true" />
            Sign out
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
