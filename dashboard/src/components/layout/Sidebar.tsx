import { NavLink } from "react-router-dom";
import { FlaskConical, Gauge, ListTodo, Server, UploadCloud } from "lucide-react";
import { cn } from "@/lib/utils";

const NAV_ITEMS: {
  to: string;
  label: string;
  icon: typeof Gauge;
  end?: boolean;
}[] = [
  { to: "/", label: "Overview", icon: Gauge, end: true },
  { to: "/nodes", label: "Nodes", icon: Server },
  { to: "/jobs", label: "Jobs", icon: ListTodo },
  { to: "/submit", label: "Submit", icon: UploadCloud },
  { to: "/benchmarks", label: "Benchmarks", icon: FlaskConical },
];

export function SidebarNav({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <nav className="flex flex-1 flex-col gap-0.5 p-2">
      {NAV_ITEMS.map(({ to, label, icon: Icon, end }) => (
        <NavLink
          key={to}
          to={to}
          end={end}
          onClick={onNavigate}
          className={({ isActive }) =>
            cn(
              "flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm font-medium outline-none transition-colors motion-reduce:transition-none focus-visible:ring-2 focus-visible:ring-accent",
              isActive
                ? "bg-elevated text-accent"
                : "text-secondary hover:bg-elevated hover:text-primary",
            )
          }
        >
          <Icon className="size-4 shrink-0" aria-hidden="true" />
          {label}
        </NavLink>
      ))}
    </nav>
  );
}
