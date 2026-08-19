"use client";

import { useState } from "react";
import { Search } from "lucide-react";

export default function AdminResourcesPage() {
  const [userId, setUserId] = useState("");
  const open = () => { if (/^\d+$/.test(userId)) window.location.hash = `#/admin/users/${userId}`; };
  return <div className="space-y-5"><header className="border-b border-glass-border pb-5"><p className="font-mono text-xs text-primary">内容与资产</p><h1 className="mt-1 text-xl font-semibold">用户资源审查</h1><p className="mt-1 text-sm text-text-secondary">从明确的目标用户进入只读审查，不提供代登录或资产修改</p></header><section className="max-w-xl border-y border-glass-border py-6"><label className="text-sm font-medium">目标用户 ID</label><div className="mt-3 flex gap-2"><input autoFocus inputMode="numeric" value={userId} onChange={(event) => setUserId(event.target.value.replace(/\D/g, ""))} onKeyDown={(event) => { if (event.key === "Enter") open(); }} placeholder="输入用户整数 ID" className="h-10 min-w-0 flex-1 rounded-md border border-glass-border bg-input-bg px-3 font-mono text-sm outline-none focus:border-primary/60" /><button type="button" disabled={!/^\d+$/.test(userId)} onClick={open} className="inline-flex h-10 items-center gap-2 rounded-md bg-primary px-4 text-sm font-medium text-on-accent disabled:opacity-35"><Search size={15} />打开用户详情</button></div></section></div>;
}
