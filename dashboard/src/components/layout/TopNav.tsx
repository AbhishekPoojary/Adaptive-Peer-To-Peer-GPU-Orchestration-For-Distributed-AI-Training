import { useEffect, useRef, useState } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { isAdmin } from "@/api/session";
import { cn } from "@/lib/utils";

/**
 * Primary navigation as a horizontal pill row.
 *
 * Replaces the 220px left sidebar. Two reasons, in order: the dense pages —
 * Runs, Machines, the per-job scheduling audit — are tables that want the full
 * width, and a sidebar spends a fifth of a laptop screen permanently
 * advertising six places the visitor is not going. The primary user has one
 * path (upload, watch, collect) and does not need a standing directory.
 *
 * No icons. Seven labels at this size read faster as words than as
 * word-plus-glyph pairs, and an icon set here would be decoration competing
 * with the state dots that actually carry meaning.
 *
 * The active pill is a single element that slides between destinations rather
 * than a background toggled per link. That is the one piece of navigation
 * motion worth having: it shows *which* item you left and which you arrived
 * at, so the change of place is legible instead of instantaneous.
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
  const listRef = useRef<HTMLUListElement>(null);
  const [pill, setPill] = useState<{ left: number; width: number } | null>(null);
  const location = useLocation();

  // Measured from the DOM rather than computed from an index, because the
  // labels have different widths and the row scrolls on small screens.
  useEffect(() => {
    const list = listRef.current;
    if (!list) return;

    const measure = () => {
      const active = list.querySelector<HTMLElement>("[aria-current='page']");
      if (!active) {
        setPill(null);
        return;
      }
      setPill({ left: active.offsetLeft, width: active.offsetWidth });
    };

    measure();
    // Re-measure when the row reflows: a font landing late or a viewport
    // change would otherwise leave the pill behind the wrong label.
    const observer = new ResizeObserver(measure);
    observer.observe(list);
    return () => observer.disconnect();
  }, [location.pathname, items.length]);

  return (
    // Scrolls rather than collapsing into a hamburger: one mental model at
    // every width, and the current page stays visible instead of hiding
    // behind a menu button. The mask fades the overflow edge so a cut-off
    // pill reads as "more this way" rather than as a rendering bug.
    <nav
      aria-label="Primary"
      className="-mx-1 overflow-x-auto px-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden [mask-image:linear-gradient(to_right,transparent,black_8px,black_calc(100%-8px),transparent)] sm:mask-none"
    >
      <ul ref={listRef} className="relative flex items-center gap-1">
        {/* One pill, moved. Transform-driven so it composites, and skipped
            entirely before the first measurement so it never animates in
            from the left edge on load. */}
        {pill && (
          <li
            aria-hidden="true"
            className="absolute top-0 bottom-0 left-0 rounded-full bg-ink motion-reduce:transition-none"
            style={{
              width: pill.width,
              transform: `translateX(${pill.left}px)`,
              transition:
                "transform 320ms var(--ease-out-expo), width 320ms var(--ease-out-expo)",
            }}
          />
        )}
        {items.map(({ to, label, end }) => (
          <li key={to} className="relative shrink-0">
            <NavLink
              to={to}
              end={end}
              className={({ isActive }) =>
                cn(
                  "block rounded-full px-3.5 py-1.5 text-[0.8125rem] font-medium whitespace-nowrap transition-colors duration-150 ease-out",
                  // The incoming label waits for the pill. Turning it white the
                  // instant it becomes active leaves it white-on-canvas — and
                  // so unreadable — for the whole 320ms the pill is travelling.
                  // The outgoing label has no delay: the pill is leaving it, so
                  // going muted immediately is the correct reading.
                  isActive
                    ? "text-white delay-200 motion-reduce:delay-0"
                    : "text-muted delay-0 hover:text-ink",
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
