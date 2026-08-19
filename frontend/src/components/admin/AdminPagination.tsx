"use client";

import { ChevronLeft, ChevronRight } from "lucide-react";

export default function AdminPagination({
  offset,
  limit,
  total,
  onChange,
  onLimitChange,
}: {
  offset: number;
  limit: number;
  total: number;
  onChange: (offset: number) => void;
  onLimitChange?: (limit: number) => void;
}) {
  const currentPage = total === 0 ? 1 : Math.floor(offset / limit) + 1;
  const pageCount = Math.max(1, Math.ceil(total / limit));
  return (
    <nav aria-label="列表分页" className="flex flex-wrap items-center justify-end gap-2">
      {onLimitChange && (
        <label className="mr-auto flex items-center gap-2 text-xs text-text-muted">
          每页
          <select
            aria-label="每页数量"
            value={limit}
            onChange={(event) => onLimitChange(Number(event.target.value))}
            className="h-8 rounded-md border border-glass-border bg-elevated px-2 text-xs text-foreground"
          >
            {[20, 30, 50, 100].map((value) => <option key={value} value={value}>{value}</option>)}
          </select>
        </label>
      )}
      <span className="min-w-24 text-center font-mono text-xs text-text-muted">
        第 {currentPage} / {pageCount} 页
      </span>
      <button
        type="button"
        aria-label="上一页"
        title="上一页"
        disabled={offset === 0}
        onClick={() => onChange(Math.max(0, offset - limit))}
        className="grid h-8 w-8 place-items-center rounded-md border border-glass-border disabled:opacity-30"
      >
        <ChevronLeft size={15} />
      </button>
      <button
        type="button"
        aria-label="下一页"
        title="下一页"
        disabled={offset + limit >= total}
        onClick={() => onChange(offset + limit)}
        className="grid h-8 w-8 place-items-center rounded-md border border-glass-border disabled:opacity-30"
      >
        <ChevronRight size={15} />
      </button>
    </nav>
  );
}
