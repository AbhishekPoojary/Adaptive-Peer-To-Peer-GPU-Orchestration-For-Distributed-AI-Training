import { NavLink } from "react-router-dom";
import { isAdmin } from "@/api/session";
import { cn } from "@/lib/utils";

/**
 * Primary navigation as a horizontal pill row.
 *
 * Replaces the 220px left sidebar. Two reasons, in order: the dense pages —
 * Jobs, Nodes, the per-job scheduling audit — are tables that want the full
 * width, and a sidebar spends a fifth of a laptop screen permanently
 * advertising six places the visitor is not going. The primary user has one
 * path (upload, watch, collect) and does not need a standing directory of
 * everything else.
 *
 * No icons. Seven labels at this size read faster as words than as
 * word-plus-glyph pairs, and an icon set here would be decoration competing
 * with the state dots that actually carry meaning.
 */
const NAV_ITEMS: {
  to: string;
  label: string;
  end?: boolean;
  /** Hidden from non-admins. Cosmetic: every route behind this is enforced
   *  server-side, so hiding it saves a pointless click rather than granting
   *  any access control. */
  adminOnly?: boolean;
}[] = [
  { to: "/", label: "Overview", end: true },
  { to: "/datasets", label: "Datasets" },
  { to: "/submit", label: "Train" },
  { to: "/jobs", label: "Runs" },
  { to: "/nodes", label: "Machines" },
  { to: "/benchmarks", label: "Benchmarks" },
  { to: "/users", label: "People", adminOnly: true },
];

export function TopNav() {
  const items = NAV_ITEMS.filter((item) => !item.adminOnly || isAdmin());

  return (
    // Scrolls rather than collapsing into a hamburger: one mental model at
    // every width, and the current page stays visible instead of hiding
    // behind a menu button. The mask fades the overflow edge so a cut-off
    // pill reads as "more this way" rather than as a rendering bug.
    <nav
      aria-label="Primary"
      className="-mx-1 overflow-x-auto px-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden [mask-image:linear-gradient(to_right,transparent,black_8px,black_calc(100%-8px),transparent)] sm:mask-none"
    >
      <ul className="flex items-center gap-1">
        {items.map(({ to, label, end }) => (
          <li key={to} className="shrink-0">
            <NavLink
              to={to}
              end={end}
              className={({ isActive }) =>
                cn(
                  "block rounded-full px-3.5 py-1.5 text-[0.8125rem] font-medium whitespace-nowrap transition-[color,background-color] duration-150 ease-out",
                  isActive
                    ? "bg-ink text-white"
                    : "text-muted hover:bg-sunken hover:text-ink",
                )
              }
            >
              {label}
            </NavLink>
          </li>
        ))}
      </ul>
    </nav>
  );
}
