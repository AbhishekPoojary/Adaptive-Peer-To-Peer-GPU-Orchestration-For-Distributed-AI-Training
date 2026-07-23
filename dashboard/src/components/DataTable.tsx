import type { ReactNode } from "react";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

export interface DataTableColumn<T> {
  key: string;
  header: string;
  render: (row: T) => ReactNode;
  className?: string;
  headerClassName?: string;
}

export interface DataTableProps<T> {
  columns: DataTableColumn<T>[];
  rows: T[];
  getRowKey: (row: T) => string;
  onRowClick?: (row: T) => void;
  /** rendered instead of body rows while the first fetch is in flight */
  isLoading?: boolean;
  skeletonRows?: number;
  className?: string;
}

/**
 * Shared table shell used by both the Nodes and Jobs list pages. Every row is
 * fully clickable (not just a link fragment) and keyboard-reachable via
 * tabIndex + Enter/Space, per the M3.5 quality floor. Sorting is not wired up
 * this milestone but the column shape is ready for it later.
 */
export function DataTable<T>({
  columns,
  rows,
  getRowKey,
  onRowClick,
  isLoading,
  skeletonRows = 5,
  className,
}: DataTableProps<T>) {
  return (
    <div className={cn("rounded-md border border-hairline bg-panel", className)}>
      <Table>
        <TableHeader>
          <TableRow>
            {columns.map((col) => (
              <TableHead key={col.key} className={col.headerClassName}>
                {col.header}
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {isLoading
            ? Array.from({ length: skeletonRows }).map((_, i) => (
                <TableRow key={`skeleton-${i}`}>
                  {columns.map((col) => (
                    <TableCell key={col.key} className={col.className}>
                      <Skeleton className="h-4 w-full max-w-32" />
                    </TableCell>
                  ))}
                </TableRow>
              ))
            : rows.map((row) => {
                const key = getRowKey(row);
                return (
                  <TableRow
                    key={key}
                    clickable={Boolean(onRowClick)}
                    tabIndex={onRowClick ? 0 : undefined}
                    onClick={onRowClick ? () => onRowClick(row) : undefined}
                    onKeyDown={
                      onRowClick
                        ? (e) => {
                            if (e.key === "Enter" || e.key === " ") {
                              e.preventDefault();
                              onRowClick(row);
                            }
                          }
                        : undefined
                    }
                    className={
                      onRowClick
                        ? "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-inset"
                        : undefined
                    }
                  >
                    {columns.map((col) => (
                      <TableCell key={col.key} className={col.className}>
                        {col.render(row)}
                      </TableCell>
                    ))}
                  </TableRow>
                );
              })}
        </TableBody>
      </Table>
    </div>
  );
}
