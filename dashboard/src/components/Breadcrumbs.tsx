import { Fragment } from "react";
import { Link } from "react-router-dom";
import { ChevronRight } from "lucide-react";

export interface Crumb {
  label: string;
  to?: string;
}

export function Breadcrumbs({ items }: { items: Crumb[] }) {
  return (
    <nav aria-label="Breadcrumb" className="mb-3 flex items-center gap-1.5 text-sm">
      {items.map((item, i) => {
        const isLast = i === items.length - 1;
        return (
          <Fragment key={`${item.label}-${i}`}>
            {i > 0 && (
              <ChevronRight className="size-3.5 text-tertiary" aria-hidden="true" />
            )}
            {item.to && !isLast ? (
              <Link
                to={item.to}
                className="text-secondary hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent rounded"
              >
                {item.label}
              </Link>
            ) : (
              <span
                className={isLast ? "font-medium text-primary" : "text-secondary"}
                aria-current={isLast ? "page" : undefined}
              >
                {item.label}
              </span>
            )}
          </Fragment>
        );
      })}
    </nav>
  );
}
