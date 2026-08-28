"use client";

import { useCallback, useEffect, useId, useMemo, useState, type FormEvent, type ReactNode } from "react";
import {
  Activity,
  CircleAlert,
  Ban,
  ChevronRight,
  ClipboardList,
  FileClock,
  FolderSearch,
  Gauge,
  Images,
  KeyRound,
  Loader2,
  LogOut,
  RefreshCw,
  ReceiptText,
  RotateCcw,
  Search,
  Settings2,
  ShieldCheck,
  Ticket,
  UserCheck,
  UserPlus,
  Users,
  WalletCards,
  X,
} from "lucide-react";

import {
  adminPlatformApi,
  adminAuthApi,
  getSafeApiError,
  type AdminAuditEventItem,
  type AdminPage,
  type AdminTaskItem,
  type AdminUsagePage,
  type AdminUserItem,
  type AdminUserStatus,
} from "@/lib/api";
import { adminFilter, replaceAdminFilters, type AdminSection } from "@/lib/adminRoute";
import { toast } from "@/store/toastStore";
import { useAdminAuthStore } from "@/store/adminAuthStore";

import ConfigurationAdminPage from "./ConfigurationAdminPage";
import ImportAdminPage from "./ImportAdminPage";
import InvitationAdminPage from "./InvitationAdminPage";
import TicketAdminPage from "./TicketAdminPage";
import AdminDashboardPage from "./AdminDashboardPage";
import AdminExceptionsPage from "./AdminExceptionsPage";
import AdminResourcesPage from "./AdminResourcesPage";
import ManualRechargeAdminPage from "./ManualRechargeAdminPage";
import SystemScenesAdminPage from "./SystemScenesAdminPage";
import UserDetailAdminPage from "./UserDetailAdminPage";
import { AdminOneTimeResultDialog, AdminReasonDialog } from "./AdminDialogs";
import AdminPagination from "./AdminPagination";

const EMPTY_PAGE = { items: [], total: 0, offset: 0, limit: 30 };
const ADMIN_PAGE_LIMITS = new Set([20, 30, 50, 100]);

function initialAdminPage(queryString: string) {
  const parsedOffset = Number(adminFilter(queryString, "offset"));
  const parsedLimit = Number(adminFilter(queryString, "limit"));
  return {
    ...EMPTY_PAGE,
    offset: Number.isInteger(parsedOffset) && parsedOffset >= 0 ? parsedOffset : 0,
    limit: ADMIN_PAGE_LIMITS.has(parsedLimit) ? parsedLimit : EMPTY_PAGE.limit,
  };
}

const SECTION_ITEMS: Array<{
  id: AdminSection;
  label: string;
  description: string;
  icon: typeof Users;
}> = [
  { id: "dashboard", label: "运营概览", description: "关键指标与待办异常", icon: Gauge },
  { id: "users", label: "用户", description: "账号状态与安全处置", icon: Users },
  { id: "invitations", label: "注册邀请", description: "内测邀请码签发与撤销", icon: KeyRound },
  { id: "recharge-orders", label: "充值订单", description: "人工到账、退款与对账", icon: ReceiptText },
  { id: "system-scenes", label: "系统场景", description: "平台场景模板与引用", icon: Images },
  { id: "resources", label: "资源审查", description: "用户内容与资产只读审查", icon: FolderSearch },
  { id: "exceptions", label: "异常队列", description: "失败、停滞与数据差异", icon: CircleAlert },
  { id: "tasks", label: "AI 任务", description: "执行状态与计费复核", icon: Activity },
  { id: "usage", label: "用量", description: "计量令牌与算力券结算", icon: WalletCards },
  { id: "tickets", label: "算力券调整", description: "赠送、扣减与补偿", icon: Ticket },
  { id: "configuration", label: "模型与配置", description: "路由、参数与换算规则", icon: Settings2 },
  { id: "audit-events", label: "审计事件", description: "管理操作与关联标识", icon: ClipboardList },
  { id: "import-batches", label: "导入批次", description: "本地数据迁移状态", icon: FileClock },
];

const SECTION_GROUPS: Array<{ label: string; sections: AdminSection[] }> = [
  { label: "概览", sections: ["dashboard"] },
  { label: "用户", sections: ["users", "invitations"] },
  { label: "财务", sections: ["recharge-orders", "tickets", "usage"] },
  { label: "内容与资产", sections: ["resources", "system-scenes"] },
  { label: "AI 运营", sections: ["tasks", "exceptions"] },
  { label: "平台与运维", sections: ["configuration", "audit-events", "import-batches"] },
];

const CAPABILITY_ZH: Record<string, string> = {
  llm: "文本生成",
  image: "图像生成",
  video: "视频生成",
  speech: "语音生成",
  audio: "音频生成",
  image_generation: "图像生成",
  video_generation: "视频生成",
  script_analysis: "剧本分析",
  prompt_polish: "提示词润色",
  text_to_speech: "语音生成",
};

function formatDate(value: string | null | undefined): string {
  if (!value) return "-";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function shortId(value: string | null | undefined): string {
  if (!value) return "-";
  return value.length > 16 ? `${value.slice(0, 8)}…${value.slice(-4)}` : value;
}

function maskPhone(phone: string | null | undefined): string {
  return phone?.replace(/(\+86\d{3})\d{4}(\d{4})/, "$1****$2") || "未绑定手机号";
}

function capabilityLabel(capability: string): string {
  return CAPABILITY_ZH[capability] || capability;
}

function StateBadge({ children, tone = "neutral" }: { children: ReactNode; tone?: "neutral" | "success" | "warning" | "danger" }) {
  const tones = {
    neutral: "border-glass-border bg-glass text-text-secondary",
    success: "border-emerald-400/25 bg-emerald-400/10 text-emerald-200",
    warning: "border-amber-400/25 bg-amber-400/10 text-amber-200",
    danger: "border-red-400/25 bg-red-400/10 text-red-200",
  };
  return <span className={`inline-flex items-center rounded px-2 py-1 text-xs ${tones[tone]}`}>{children}</span>;
}

function LoadingState() {
  return (
    <div className="grid min-h-56 place-items-center text-text-secondary" role="status">
      <div className="flex items-center gap-2 text-sm"><Loader2 size={16} className="animate-spin" />正在加载平台数据</div>
    </div>
  );
}

function EmptyState({ text }: { text: string }) {
  return <div className="grid min-h-44 place-items-center border-y border-glass-border text-sm text-text-muted">{text}</div>;
}

function SectionHeader({ title, description, total, onRefresh, loading, action }: { title: string; description: string; total?: number; onRefresh: () => void; loading: boolean; action?: ReactNode }) {
  return (
    <header className="flex flex-wrap items-end justify-between gap-4 border-b border-glass-border pb-5">
      <div>
        <p className="text-xs text-primary">平台管理</p>
        <h2 className="mt-1 text-xl font-semibold text-foreground">{title}</h2>
        <p className="mt-1 text-sm text-text-secondary">{description}</p>
      </div>
      <div className="flex items-center gap-3">
        {typeof total === "number" && <span className="font-mono text-xs text-text-muted">共 {total} 条</span>}
        {action}
        <button type="button" onClick={onRefresh} disabled={loading} title="刷新" aria-label="刷新" className="grid h-9 w-9 place-items-center rounded-md border border-glass-border bg-glass text-text-secondary hover:bg-hover-bg hover:text-foreground disabled:opacity-50">
          <RefreshCw size={16} className={loading ? "animate-spin" : ""} />
        </button>
      </div>
    </header>
  );
}

function CreateUserDialog({ onClose, onCreated }: { onClose: () => void; onCreated: () => Promise<void> }) {
  const [phone, setPhone] = useState("");
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [reason, setReason] = useState("后台创建用户");
  const [submitting, setSubmitting] = useState(false);
  const phoneId = useId();
  const passwordId = useId();
  const confirmationId = useId();
  const reasonId = useId();

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const normalizedPhone = phone.replace(/\D/g, "").slice(0, 11);
    if (normalizedPhone.length !== 11) {
      toast.error("请输入 11 位中国大陆手机号");
      return;
    }
    if (password.length < 10 || !/[A-Za-z]/.test(password) || !/\d/.test(password)) {
      toast.error("密码至少 10 位，并包含字母和数字");
      return;
    }
    if (password !== confirmation) {
      toast.error("两次输入的密码不一致");
      return;
    }
    if (!reason.trim()) {
      toast.error("请填写创建原因");
      return;
    }
    setSubmitting(true);
    try {
      await adminPlatformApi.createUser(`+86${normalizedPhone}`, password, reason.trim());
      toast.success("用户已创建", { body: "默认工作区和算力券钱包已初始化" });
      await onCreated();
      onClose();
    } catch (error) {
      toast.error("用户创建失败", { body: getSafeApiError(error).message });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-[120] grid place-items-center bg-black/70 px-4 py-8 backdrop-blur-sm" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !submitting) onClose(); }}>
      <section role="dialog" aria-modal="true" aria-labelledby="create-user-title" className="w-full max-w-lg rounded-lg border border-glass-border bg-elevated shadow-2xl shadow-black/40">
        <header className="flex items-start justify-between gap-4 border-b border-glass-border px-5 py-4">
          <div>
            <p className="text-xs font-medium text-primary">用户管理</p>
            <h3 id="create-user-title" className="mt-1 text-base font-semibold text-foreground">添加用户</h3>
            <p className="mt-1 text-sm text-text-secondary">创建后即可使用手机号和初始密码登录。</p>
          </div>
          <button type="button" onClick={onClose} disabled={submitting} title="关闭" aria-label="关闭" className="grid h-8 w-8 flex-none place-items-center rounded-md text-text-muted hover:bg-hover-bg hover:text-foreground disabled:opacity-50"><X size={17} /></button>
        </header>
        <form onSubmit={submit} className="space-y-4 px-5 py-5">
          <div>
            <label htmlFor={phoneId} className="block text-sm font-medium text-foreground">手机号</label>
            <div className="mt-2 flex h-10 overflow-hidden rounded-md border border-glass-border bg-glass focus-within:border-primary/60">
              <span className="grid w-14 flex-none place-items-center border-r border-glass-border font-mono text-xs text-text-secondary">+86</span>
              <input id={phoneId} type="tel" inputMode="numeric" autoComplete="off" value={phone} onChange={(event) => setPhone(event.target.value.replace(/\D/g, "").slice(0, 11))} placeholder="请输入手机号" disabled={submitting} autoFocus className="min-w-0 flex-1 bg-transparent px-3 text-sm outline-none placeholder:text-text-muted" />
            </div>
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            <div><label htmlFor={passwordId} className="block text-sm font-medium text-foreground">初始密码</label><input id={passwordId} type="password" autoComplete="new-password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="至少 10 位" disabled={submitting} className="mt-2 h-10 w-full rounded-md border border-glass-border bg-glass px-3 text-sm outline-none placeholder:text-text-muted focus:border-primary/60" /></div>
            <div><label htmlFor={confirmationId} className="block text-sm font-medium text-foreground">确认密码</label><input id={confirmationId} type="password" autoComplete="new-password" value={confirmation} onChange={(event) => setConfirmation(event.target.value)} placeholder="再次输入" disabled={submitting} className="mt-2 h-10 w-full rounded-md border border-glass-border bg-glass px-3 text-sm outline-none placeholder:text-text-muted focus:border-primary/60" /></div>
          </div>
          <div><label htmlFor={reasonId} className="block text-sm font-medium text-foreground">创建原因</label><input id={reasonId} value={reason} onChange={(event) => setReason(event.target.value)} maxLength={1000} disabled={submitting} className="mt-2 h-10 w-full rounded-md border border-glass-border bg-glass px-3 text-sm outline-none focus:border-primary/60" /></div>
          <p className="text-xs leading-5 text-text-muted">系统会同时创建默认工作区和算力券钱包，初始赠送遵循当前平台配置。</p>
          <footer className="flex justify-end gap-2 border-t border-glass-border pt-4">
            <button type="button" onClick={onClose} disabled={submitting} className="h-9 rounded-md border border-glass-border bg-glass px-3 text-sm text-text-secondary hover:bg-hover-bg hover:text-foreground disabled:opacity-50">取消</button>
            <button type="submit" disabled={submitting} className="inline-flex h-9 items-center gap-2 rounded-md bg-primary px-3 text-sm font-medium text-on-accent hover:bg-primary-hover disabled:opacity-50">{submitting ? <Loader2 size={15} className="animate-spin" /> : <UserPlus size={15} />}创建用户</button>
          </footer>
        </form>
      </section>
    </div>
  );
}

function SearchField({ value, onChange, onSubmit, placeholder }: { value: string; onChange: (value: string) => void; onSubmit: () => void; placeholder: string }) {
  return (
    <div className="relative min-w-0 flex-1 sm:max-w-sm">
      <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-text-muted" />
      <input value={value} onChange={(event) => onChange(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") onSubmit(); }} placeholder={placeholder} className="h-9 w-full rounded-md border border-glass-border bg-glass pl-9 pr-3 text-sm outline-none focus:border-primary/60" />
    </div>
  );
}

function UsersView({ queryString = "" }: { queryString?: string }) {
  const [page, setPage] = useState<AdminPage<AdminUserItem>>(() => initialAdminPage(queryString));
  const [query, setQuery] = useState(() => adminFilter(queryString, "query"));
  const [userId, setUserId] = useState(() => adminFilter(queryString, "user_id"));
  const [status, setStatus] = useState<"" | AdminUserStatus>(() => adminFilter(queryString, "status") as "" | AdminUserStatus);
  const [walletException, setWalletException] = useState(() => adminFilter(queryString, "wallet_exception"));
  const [startAt, setStartAt] = useState(() => adminFilter(queryString, "start_at"));
  const [endAt, setEndAt] = useState(() => adminFilter(queryString, "end_at"));
  const [loading, setLoading] = useState(true);
  const [busyUser, setBusyUser] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [pendingAction, setPendingAction] = useState<
    | { kind: "status"; user: AdminUserItem; nextStatus: AdminUserStatus }
    | { kind: "sessions"; user: AdminUserItem }
    | { kind: "reset"; user: AdminUserItem }
    | null
  >(null);
  const [resetResult, setResetResult] = useState<{ credential: string; expiresAt: string } | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setPage(await adminPlatformApi.listUsers({
        query: query.trim() || undefined,
        user_id: userId || undefined,
        status: status || undefined,
        wallet_exception: walletException === "" ? undefined : walletException === "true",
        start_at: startAt ? `${startAt}T00:00:00` : undefined,
        end_at: endAt ? `${endAt}T23:59:59` : undefined,
        offset: page.offset,
        limit: page.limit,
      }));
    } catch (error) {
      toast.error("用户列表加载失败", { body: getSafeApiError(error).message });
    } finally {
      setLoading(false);
    }
  }, [endAt, page.limit, page.offset, query, startAt, status, userId, walletException]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    replaceAdminFilters("users", {
      query: query.trim(),
      user_id: userId,
      status,
      wallet_exception: walletException,
      start_at: startAt,
      end_at: endAt,
      offset: page.offset || undefined,
      limit: page.limit === 30 ? undefined : page.limit,
    });
  }, [endAt, page.limit, page.offset, query, startAt, status, userId, walletException]);

  const resetOffset = () => setPage((current) => ({ ...current, offset: 0 }));

  const runPendingAction = async (reason: string) => {
    if (!pendingAction) return;
    const { user } = pendingAction;
    const actionLabel = pendingAction.kind === "status"
      ? pendingAction.nextStatus === "active" ? "恢复用户" : "停用用户"
      : pendingAction.kind === "sessions" ? "撤销全部会话" : "签发重置凭据";
    setBusyUser(user.id);
    try {
      if (pendingAction.kind === "status") {
        const result = await adminPlatformApi.updateUserStatus(user.id, pendingAction.nextStatus, reason);
        toast.success(result.message);
        await load();
      } else if (pendingAction.kind === "sessions") {
        const result = await adminPlatformApi.revokeUserSessions(user.id, reason);
        toast.success(result.message);
      } else {
        const result = await adminPlatformApi.issueResetCredential(user.id, reason);
        setResetResult({ credential: result.credential, expiresAt: result.expires_at });
      }
      setPendingAction(null);
    } catch (error) {
      toast.error(`${actionLabel}失败`, { body: getSafeApiError(error).message });
    } finally {
      setBusyUser(null);
    }
  };

  return (
    <div className="space-y-5">
      <SectionHeader
        title="用户"
        description="查看账号状态、余额并执行有审计记录的安全处置"
        total={page.total}
        onRefresh={() => void load()}
        loading={loading}
        action={<button type="button" onClick={() => setCreateOpen(true)} className="inline-flex h-9 items-center gap-2 rounded-md bg-primary px-3 text-sm font-medium text-on-accent hover:bg-primary-hover"><UserPlus size={15} />添加用户</button>}
      />
      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
        <SearchField value={query} onChange={(value) => { setQuery(value); resetOffset(); }} onSubmit={() => void load()} placeholder="搜索用户名或手机号" />
        <input aria-label="用户 ID" inputMode="numeric" value={userId} onChange={(event) => { setUserId(event.target.value.replace(/\D/g, "")); resetOffset(); }} placeholder="用户 ID" className="h-9 rounded-md border border-glass-border bg-glass px-3 text-sm outline-none focus:border-primary/60" />
        <select value={status} aria-label="账号状态" onChange={(event) => { setStatus(event.target.value as "" | AdminUserStatus); resetOffset(); }} className="h-9 rounded-md border border-glass-border bg-elevated px-3 text-sm outline-none focus:border-primary/60">
          <option value="">全部状态</option><option value="active">正常</option><option value="suspended">已停用</option>
        </select>
        <select value={walletException} aria-label="钱包状态" onChange={(event) => { setWalletException(event.target.value); resetOffset(); }} className="h-9 rounded-md border border-glass-border bg-elevated px-3 text-sm"><option value="">全部钱包状态</option><option value="true">钱包异常</option><option value="false">钱包正常</option></select>
        <label className="flex items-center gap-2 text-xs text-text-muted">注册开始<input type="date" value={startAt} onChange={(event) => { setStartAt(event.target.value); resetOffset(); }} className="h-9 min-w-0 flex-1 rounded-md border border-glass-border bg-elevated px-2 text-sm text-foreground" /></label>
        <label className="flex items-center gap-2 text-xs text-text-muted">注册结束<input type="date" value={endAt} onChange={(event) => { setEndAt(event.target.value); resetOffset(); }} className="h-9 min-w-0 flex-1 rounded-md border border-glass-border bg-elevated px-2 text-sm text-foreground" /></label>
      </div>
      {loading ? <LoadingState /> : page.items.length === 0 ? <EmptyState text="没有符合条件的用户" /> : (
        <div className="divide-y divide-glass-border border-y border-glass-border">
          {page.items.map((user) => (
            <article key={user.id} className="grid gap-4 py-4 lg:grid-cols-[minmax(180px,1.2fr)_minmax(170px,.8fr)_minmax(170px,.8fr)_minmax(210px,1fr)] lg:items-center">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2"><span className="text-sm font-medium text-foreground">{user.account_label || maskPhone(user.phone)}</span><StateBadge tone={user.status === "active" ? "success" : "danger"}>{user.status_zh}</StateBadge>{user.wallet_exception && <StateBadge tone="danger">钱包异常</StateBadge>}</div>
                {user.phone && <p className="mt-1 font-mono text-xs text-text-secondary">{maskPhone(user.phone)}</p>}
                <p className="mt-1 truncate font-mono text-xs text-text-muted" title={user.id}>{user.id}</p>
                <p className="mt-1 text-xs text-text-muted">注册于 {formatDate(user.created_at)}</p>
              </div>
              <div className="grid grid-cols-2 gap-3 text-sm">
                <div><span className="block text-xs text-text-muted">可用算力券</span><strong className="mt-1 block font-mono text-foreground">{user.available_tickets}</strong></div>
                <div><span className="block text-xs text-text-muted">预扣算力券</span><strong className="mt-1 block font-mono text-foreground">{user.held_tickets}</strong></div>
              </div>
              <div className="grid grid-cols-2 gap-3 text-sm">
                <div><span className="block text-xs text-text-muted">工作区</span><strong className="mt-1 block font-mono text-foreground">{user.workspace_count}</strong></div>
                <div><span className="block text-xs text-text-muted">AI 任务</span><strong className="mt-1 block font-mono text-foreground">{user.task_summary.total}</strong>{user.task_summary.exceptions > 0 && <span className="mt-1 block text-xs text-amber-200">异常 {user.task_summary.exceptions}</span>}</div>
              </div>
              <div className="flex flex-wrap gap-2 lg:justify-end">
                <button type="button" title="查看用户详情" aria-label={`查看${user.account_label || maskPhone(user.phone)}详情`} onClick={() => { window.location.hash = `#/admin/users/${user.id}`; }} className="grid h-8 w-8 place-items-center rounded-md border border-glass-border bg-glass text-text-secondary hover:bg-hover-bg hover:text-foreground"><ChevronRight size={14} /></button>
                <button type="button" disabled={busyUser === user.id} onClick={() => setPendingAction({ kind: "status", user, nextStatus: user.status === "active" ? "suspended" : "active" })} className="inline-flex h-8 items-center gap-1.5 rounded-md border border-glass-border bg-glass px-2.5 text-xs text-text-secondary hover:bg-hover-bg hover:text-foreground disabled:opacity-40">{user.status === "active" ? <Ban size={13} /> : <UserCheck size={13} />}{user.status === "active" ? "停用" : "恢复"}</button>
                <button type="button" disabled={busyUser === user.id} onClick={() => setPendingAction({ kind: "sessions", user })} className="inline-flex h-8 items-center gap-1.5 rounded-md border border-glass-border bg-glass px-2.5 text-xs text-text-secondary hover:bg-hover-bg hover:text-foreground disabled:opacity-40"><RotateCcw size={13} />撤销会话</button>
                <button type="button" disabled={busyUser === user.id} onClick={() => setPendingAction({ kind: "reset", user })} className="inline-flex h-8 items-center gap-1.5 rounded-md border border-glass-border bg-glass px-2.5 text-xs text-text-secondary hover:bg-hover-bg hover:text-foreground disabled:opacity-40"><KeyRound size={13} />重置凭据</button>
              </div>
            </article>
          ))}
        </div>
      )}
      <AdminPagination offset={page.offset} limit={page.limit} total={page.total} onChange={(offset) => setPage((current) => ({ ...current, offset }))} onLimitChange={(limit) => setPage((current) => ({ ...current, limit, offset: 0 }))} />
      {createOpen && <CreateUserDialog onClose={() => setCreateOpen(false)} onCreated={load} />}
      {pendingAction && (
        <AdminReasonDialog
          title={pendingAction.kind === "status" ? pendingAction.nextStatus === "active" ? "恢复用户" : "停用用户" : pendingAction.kind === "sessions" ? "撤销全部会话" : "签发重置凭据"}
          description={pendingAction.kind === "status" ? "账号状态变更会立即影响登录和 AI 任务权限。" : pendingAction.kind === "sessions" ? "该用户当前的所有登录会话将立即失效。" : "系统将生成一份短期、单次使用的重置凭据，并且只显示一次。"}
          confirmLabel="确认执行"
          danger={pendingAction.kind !== "reset"}
          busy={busyUser === pendingAction.user.id}
          onClose={() => setPendingAction(null)}
          onConfirm={runPendingAction}
        />
      )}
      {resetResult && <AdminOneTimeResultDialog title="一次性重置凭据" description="请通过安全渠道交给用户。关闭后系统不会再次显示。" value={resetResult.credential} expiresAt={resetResult.expiresAt} onClose={() => setResetResult(null)} />}
    </div>
  );
}

function TasksView({ queryString = "" }: { queryString?: string }) {
  const [page, setPage] = useState<AdminPage<AdminTaskItem>>(() => initialAdminPage(queryString));
  const [status, setStatus] = useState(() => adminFilter(queryString, "status"));
  const [userId, setUserId] = useState(() => adminFilter(queryString, "user_id"));
  const [workspaceId, setWorkspaceId] = useState(() => adminFilter(queryString, "workspace_id"));
  const [startAt, setStartAt] = useState(() => adminFilter(queryString, "start_at"));
  const [endAt, setEndAt] = useState(() => adminFilter(queryString, "end_at"));
  const [loading, setLoading] = useState(true);
  const load = useCallback(async () => {
    setLoading(true);
    try { setPage(await adminPlatformApi.listTasks({ status: status || undefined, user_id: userId || undefined, workspace_id: workspaceId || undefined, start_at: startAt ? `${startAt}T00:00:00` : undefined, end_at: endAt ? `${endAt}T23:59:59` : undefined, offset: page.offset, limit: page.limit })); }
    catch (error) { toast.error("AI 任务加载失败", { body: getSafeApiError(error).message }); }
    finally { setLoading(false); }
  }, [endAt, page.limit, page.offset, startAt, status, userId, workspaceId]);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => { replaceAdminFilters("tasks", { status, user_id: userId, workspace_id: workspaceId, start_at: startAt, end_at: endAt, offset: page.offset || undefined, limit: page.limit === 30 ? undefined : page.limit }); }, [endAt, page.limit, page.offset, startAt, status, userId, workspaceId]);
  const resetOffset = () => setPage((current) => ({ ...current, offset: 0 }));
  return (
    <div className="space-y-5">
      <SectionHeader title="AI 任务" description="检查任务执行、预扣算力券和计费待复核状态" total={page.total} onRefresh={() => void load()} loading={loading} />
      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4"><select value={status} onChange={(event) => { setStatus(event.target.value); resetOffset(); }} className="h-9 rounded-md border border-glass-border bg-elevated px-3 text-sm outline-none focus:border-primary/60"><option value="">全部状态</option><option value="queued">排队中</option><option value="running">执行中</option><option value="succeeded">已完成</option><option value="failed">已失败</option><option value="cancelled">已取消</option><option value="support_review">计费待复核</option></select><input inputMode="numeric" value={userId} onChange={(event) => { setUserId(event.target.value.replace(/\D/g, "")); resetOffset(); }} placeholder="用户 ID" className="h-9 rounded-md border border-glass-border bg-input-bg px-3 text-sm" /><input inputMode="numeric" value={workspaceId} onChange={(event) => { setWorkspaceId(event.target.value.replace(/\D/g, "")); resetOffset(); }} placeholder="工作区 ID" className="h-9 rounded-md border border-glass-border bg-input-bg px-3 text-sm" /><label className="flex items-center gap-2 text-xs text-text-muted">开始<input type="date" value={startAt} onChange={(event) => { setStartAt(event.target.value); resetOffset(); }} className="h-9 min-w-0 flex-1 rounded-md border border-glass-border bg-elevated px-2 text-foreground" /></label><label className="flex items-center gap-2 text-xs text-text-muted">结束<input type="date" value={endAt} onChange={(event) => { setEndAt(event.target.value); resetOffset(); }} className="h-9 min-w-0 flex-1 rounded-md border border-glass-border bg-elevated px-2 text-foreground" /></label></div>
      {loading ? <LoadingState /> : page.items.length === 0 ? <EmptyState text="暂无 AI 任务" /> : (
        <div className="relative divide-y divide-glass-border border-y border-glass-border before:absolute before:bottom-0 before:left-[7px] before:top-0 before:w-px before:bg-primary/30">
          {page.items.map((task) => (
            <article key={task.id} className="relative grid gap-3 py-4 pl-7 lg:grid-cols-[minmax(0,1.2fr)_minmax(150px,.65fr)_minmax(180px,.8fr)] lg:items-center">
              <span className={`absolute left-1 top-6 h-[7px] w-[7px] rounded-full ${task.status === "support_review" ? "bg-amber-300" : task.status === "succeeded" ? "bg-emerald-300" : "bg-primary"}`} />
              <div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><span className="text-sm font-medium">{capabilityLabel(task.capability)}</span><StateBadge tone={task.status === "support_review" ? "warning" : task.status === "failed" ? "danger" : task.status === "succeeded" ? "success" : "neutral"}>{task.status_zh}</StateBadge></div><p className="mt-1 font-mono text-xs text-text-muted">{maskPhone(task.user_phone)} · {shortId(task.id)}</p>{(task.support_review_reason || task.safe_error_message) && <p className="mt-1 text-sm text-amber-200">{task.support_review_reason || task.safe_error_message}</p>}</div>
              <div><span className="block text-xs text-text-muted">预扣算力券</span><strong className="mt-1 block font-mono text-sm">{task.quoted_tickets}</strong><span className="mt-1 block text-xs text-text-muted">{formatDate(task.created_at)}</span></div>
              <div className="lg:text-right"><span className="block text-sm">{task.actual_model.display_name}</span><span className="mt-1 block truncate font-mono text-xs text-text-muted" title={task.actual_model.model_id}>{task.actual_model.model_id || "未记录模型 ID"}</span></div>
            </article>
          ))}
        </div>
      )}
      <AdminPagination offset={page.offset} limit={page.limit} total={page.total} onChange={(offset) => setPage((current) => ({ ...current, offset }))} onLimitChange={(limit) => setPage((current) => ({ ...current, limit, offset: 0 }))} />
    </div>
  );
}

function UsageView({ queryString = "" }: { queryString?: string }) {
  const [page, setPage] = useState<AdminUsagePage>(() => ({
    ...initialAdminPage(queryString),
    total_metering_tokens: "0",
    total_charged_tickets: "0",
  }));
  const [outcome, setOutcome] = useState(() => adminFilter(queryString, "outcome"));
  const [userId, setUserId] = useState(() => adminFilter(queryString, "user_id"));
  const [workspaceId, setWorkspaceId] = useState(() => adminFilter(queryString, "workspace_id"));
  const [startAt, setStartAt] = useState(() => adminFilter(queryString, "start_at"));
  const [endAt, setEndAt] = useState(() => adminFilter(queryString, "end_at"));
  const [loading, setLoading] = useState(true);
  const load = useCallback(async () => {
    setLoading(true);
    try { setPage(await adminPlatformApi.listUsage({ outcome: outcome || undefined, user_id: userId || undefined, workspace_id: workspaceId || undefined, start_at: startAt ? `${startAt}T00:00:00` : undefined, end_at: endAt ? `${endAt}T23:59:59` : undefined, offset: page.offset, limit: page.limit })); }
    catch (error) { toast.error("用量数据加载失败", { body: getSafeApiError(error).message }); }
    finally { setLoading(false); }
  }, [endAt, outcome, page.limit, page.offset, startAt, userId, workspaceId]);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => { replaceAdminFilters("usage", { outcome, user_id: userId, workspace_id: workspaceId, start_at: startAt, end_at: endAt, offset: page.offset || undefined, limit: page.limit === 30 ? undefined : page.limit }); }, [endAt, outcome, page.limit, page.offset, startAt, userId, workspaceId]);
  const resetOffset = () => setPage((current) => ({ ...current, offset: 0 }));
  return (
    <div className="space-y-5">
      <SectionHeader title="用量" description="按用户和任务核对计量令牌与实际算力券结算" total={page.total} onRefresh={() => void load()} loading={loading} />
      <div className="grid border-y border-glass-border sm:grid-cols-2"><div className="py-4 sm:pr-6"><span className="text-xs text-text-muted">累计计量令牌</span><strong className="mt-1 block font-mono text-2xl">{page.total_metering_tokens}</strong></div><div className="border-t border-glass-border py-4 sm:border-l sm:border-t-0 sm:pl-6"><span className="text-xs text-text-muted">累计消耗算力券</span><strong className="mt-1 block font-mono text-2xl">{page.total_charged_tickets}</strong></div></div>
      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4"><select value={outcome} onChange={(event) => { setOutcome(event.target.value); resetOffset(); }} className="h-9 rounded-md border border-glass-border bg-elevated px-3 text-sm outline-none focus:border-primary/60"><option value="">全部结算结果</option><option value="succeeded">已结算</option><option value="nonbillable_failure">失败未计费</option><option value="billable_failure">失败已计费</option><option value="cancelled">已取消</option></select><input inputMode="numeric" value={userId} onChange={(event) => { setUserId(event.target.value.replace(/\D/g, "")); resetOffset(); }} placeholder="用户 ID" className="h-9 rounded-md border border-glass-border bg-input-bg px-3 text-sm" /><input inputMode="numeric" value={workspaceId} onChange={(event) => { setWorkspaceId(event.target.value.replace(/\D/g, "")); resetOffset(); }} placeholder="工作区 ID" className="h-9 rounded-md border border-glass-border bg-input-bg px-3 text-sm" /><label className="flex items-center gap-2 text-xs text-text-muted">开始<input type="date" value={startAt} onChange={(event) => { setStartAt(event.target.value); resetOffset(); }} className="h-9 min-w-0 flex-1 rounded-md border border-glass-border bg-elevated px-2 text-foreground" /></label><label className="flex items-center gap-2 text-xs text-text-muted">结束<input type="date" value={endAt} onChange={(event) => { setEndAt(event.target.value); resetOffset(); }} className="h-9 min-w-0 flex-1 rounded-md border border-glass-border bg-elevated px-2 text-foreground" /></label></div>
      {loading ? <LoadingState /> : page.items.length === 0 ? <EmptyState text="暂无用量记录" /> : <div className="divide-y divide-glass-border border-y border-glass-border">{page.items.map((item) => <article key={item.id} className="grid gap-3 py-4 sm:grid-cols-[minmax(0,1fr)_auto_auto] sm:items-center"><div className="min-w-0"><div className="flex items-center gap-2"><span className="text-sm font-medium">{capabilityLabel(item.capability)}</span><StateBadge tone={item.outcome === "billable_failure" ? "warning" : item.outcome === "succeeded" ? "success" : "neutral"}>{item.outcome_zh}</StateBadge></div><p className="mt-1 font-mono text-xs text-text-muted">{maskPhone(item.user_phone)} · 任务 {shortId(item.task_id)}</p></div><div className="sm:text-right"><span className="block text-xs text-text-muted">计量令牌</span><strong className="font-mono text-sm">{item.metering_tokens}</strong></div><div className="sm:min-w-28 sm:text-right"><span className="block text-xs text-text-muted">消耗算力券</span><strong className="font-mono text-sm">{item.charged_tickets}</strong><span className="mt-1 block text-xs text-text-muted">{formatDate(item.created_at)}</span></div></article>)}</div>}
      <AdminPagination offset={page.offset} limit={page.limit} total={page.total} onChange={(offset) => setPage((current) => ({ ...current, offset }))} onLimitChange={(limit) => setPage((current) => ({ ...current, limit, offset: 0 }))} />
    </div>
  );
}

function AuditView({ queryString = "" }: { queryString?: string }) {
  const [page, setPage] = useState<AdminPage<AdminAuditEventItem>>(() => initialAdminPage(queryString));
  const [query, setQuery] = useState(() => adminFilter(queryString, "action"));
  const [actorId, setActorId] = useState(() => adminFilter(queryString, "actor_admin_id"));
  const [targetUserId, setTargetUserId] = useState(() => adminFilter(queryString, "target_user_id"));
  const [workspaceId, setWorkspaceId] = useState(() => adminFilter(queryString, "workspace_id"));
  const [correlationId, setCorrelationId] = useState(() => adminFilter(queryString, "correlation_id"));
  const [loading, setLoading] = useState(true);
  const load = useCallback(async () => { setLoading(true); try { setPage(await adminPlatformApi.listAuditEvents({ action: query.trim() || undefined, actor_admin_id: actorId || undefined, target_user_id: targetUserId || undefined, workspace_id: workspaceId || undefined, correlation_id: correlationId.trim() || undefined, offset: page.offset, limit: page.limit })); } catch (error) { toast.error("审计事件加载失败", { body: getSafeApiError(error).message }); } finally { setLoading(false); } }, [actorId, correlationId, page.limit, page.offset, query, targetUserId, workspaceId]);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => { replaceAdminFilters("audit-events", { action: query.trim(), actor_admin_id: actorId, target_user_id: targetUserId, workspace_id: workspaceId, correlation_id: correlationId.trim(), offset: page.offset || undefined, limit: page.limit === 30 ? undefined : page.limit }); }, [actorId, correlationId, page.limit, page.offset, query, targetUserId, workspaceId]);
  const resetOffset = () => setPage((current) => ({ ...current, offset: 0 }));
  return <div className="space-y-5"><SectionHeader title="审计事件" description="追踪管理动作、操作原因和请求关联标识" total={page.total} onRefresh={() => void load()} loading={loading} /><div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4"><SearchField value={query} onChange={(value) => { setQuery(value); resetOffset(); }} onSubmit={() => void load()} placeholder="动作名称" /><input inputMode="numeric" value={actorId} onChange={(event) => { setActorId(event.target.value.replace(/\D/g, "")); resetOffset(); }} placeholder="管理员 ID" className="h-9 rounded-md border border-glass-border bg-input-bg px-3 text-sm" /><input inputMode="numeric" value={targetUserId} onChange={(event) => { setTargetUserId(event.target.value.replace(/\D/g, "")); resetOffset(); }} placeholder="目标用户 ID" className="h-9 rounded-md border border-glass-border bg-input-bg px-3 text-sm" /><input inputMode="numeric" value={workspaceId} onChange={(event) => { setWorkspaceId(event.target.value.replace(/\D/g, "")); resetOffset(); }} placeholder="工作区 ID" className="h-9 rounded-md border border-glass-border bg-input-bg px-3 text-sm" /><input value={correlationId} onChange={(event) => { setCorrelationId(event.target.value); resetOffset(); }} placeholder="请求关联标识" className="h-9 rounded-md border border-glass-border bg-input-bg px-3 text-sm" /></div>{loading ? <LoadingState /> : page.items.length === 0 ? <EmptyState text="暂无审计事件" /> : <div className="divide-y divide-glass-border border-y border-glass-border">{page.items.map((event) => <article key={event.id} className="grid gap-3 py-4 md:grid-cols-[minmax(0,1fr)_minmax(160px,.6fr)] md:items-center"><div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><ShieldCheck size={15} className="text-primary" /><span className="font-mono text-sm">{event.action}</span><StateBadge>{event.target_type}</StateBadge></div><p className="mt-1 text-sm text-text-secondary">{event.reason || "系统记录"}</p><p className="mt-1 truncate font-mono text-xs text-text-muted">目标 {shortId(event.target_id)} · 管理员 {shortId(event.actor_admin_id || event.actor_user_id)}</p></div><div className="md:text-right"><span className="block font-mono text-xs text-text-muted">{event.correlation_id}</span><span className="mt-1 block text-xs text-text-muted">{formatDate(event.created_at)}</span></div></article>)}</div>}<AdminPagination offset={page.offset} limit={page.limit} total={page.total} onChange={(offset) => setPage((current) => ({ ...current, offset }))} onLimitChange={(limit) => setPage((current) => ({ ...current, limit, offset: 0 }))} /></div>;
}

export default function PlatformAdminPage({ section, resourceId = null, queryString = "" }: { section: AdminSection; resourceId?: string | null; queryString?: string }) {
  const admin = useAdminAuthStore((state) => state.admin);
  const setAnonymous = useAdminAuthStore((state) => state.setAnonymous);
  const [loggingOut, setLoggingOut] = useState(false);
  const current = useMemo(() => SECTION_ITEMS.find((item) => item.id === section) || SECTION_ITEMS[0], [section]);
  const rechargeUserId = useMemo(() => new URLSearchParams(queryString).get("user_id"), [queryString]);
  const content = section === "dashboard" ? <AdminDashboardPage />
    : section === "users" ? resourceId ? <UserDetailAdminPage userId={resourceId} /> : <UsersView queryString={queryString} />
      : section === "invitations" ? <InvitationAdminPage />
        : section === "recharge-orders" ? <ManualRechargeAdminPage key={`${resourceId || "list"}:${rechargeUserId || "all"}`} orderId={resourceId} initialUserId={rechargeUserId} queryString={queryString} />
          : section === "system-scenes" ? <SystemScenesAdminPage sceneId={resourceId} queryString={queryString} />
            : section === "resources" ? <AdminResourcesPage />
              : section === "exceptions" ? <AdminExceptionsPage />
                : section === "tasks" ? <TasksView queryString={queryString} />
                  : section === "usage" ? <UsageView queryString={queryString} />
                    : section === "tickets" ? <TicketAdminPage />
                      : section === "configuration" ? <ConfigurationAdminPage />
                        : section === "audit-events" ? <AuditView queryString={queryString} />
                          : <ImportAdminPage />;
  const logout = async () => {
    setLoggingOut(true);
    try {
      await adminAuthApi.logout();
      setAnonymous();
    } catch (error) {
      toast.error("退出失败", { body: getSafeApiError(error).message });
    } finally {
      setLoggingOut(false);
    }
  };
  return (
    <div className="flex h-full min-h-0 bg-background text-foreground">
      <aside className="hidden w-64 shrink-0 flex-col border-r border-glass-border bg-surface/45 lg:flex">
        <div className="border-b border-glass-border px-5 py-5"><p className="font-display text-lg font-semibold">漫屿AIGC 系统后台</p><p className="mt-1 text-xs text-text-muted">运营、安全与计费控制面</p></div>
        <nav className="min-h-0 flex-1 space-y-5 overflow-y-auto px-3 py-4" aria-label="系统管理分区">
          {SECTION_GROUPS.map((group) => <section key={group.label} aria-label={group.label}><p className="px-3 pb-1.5 font-mono text-[10px] text-text-muted">{group.label}</p><div className="space-y-1">{group.sections.map((id) => { const item = SECTION_ITEMS.find((candidate) => candidate.id === id)!; const Icon = item.icon; return <button key={id} type="button" aria-current={section === id ? "page" : undefined} onClick={() => { window.location.hash = `#/admin/${id}`; }} className={`flex w-full items-center gap-3 rounded-md px-3 py-2 text-left transition-colors ${section === id ? "bg-primary/12 text-foreground" : "text-text-secondary hover:bg-hover-bg hover:text-foreground"}`}><Icon size={16} className={section === id ? "text-primary" : "text-text-muted"} /><span className="min-w-0"><span className="block text-sm font-medium">{item.label}</span><span className="mt-0.5 block truncate text-[11px] text-text-muted">{item.description}</span></span></button>; })}</div></section>)}
        </nav>
        <div className="flex items-center gap-3 border-t border-glass-border px-4 py-3"><div className="min-w-0 flex-1"><div className="flex items-center gap-2 text-xs text-emerald-200"><span className="h-2 w-2 rounded-full bg-emerald-300" />系统管理员已验证</div><p className="mt-1 truncate font-mono text-[11px] text-text-muted">{admin ? <span data-visible-copy-source="user">{admin.username}</span> : "当前管理员"}</p></div><button type="button" onClick={() => void logout()} disabled={loggingOut} title="退出系统后台" aria-label="退出系统后台" className="grid h-8 w-8 shrink-0 place-items-center rounded-md border border-glass-border text-text-secondary hover:bg-hover-bg hover:text-foreground disabled:opacity-50">{loggingOut ? <Loader2 size={14} className="animate-spin" /> : <LogOut size={14} />}</button></div>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 shrink-0 items-center justify-between gap-3 border-b border-glass-border px-4 lg:hidden"><div className="min-w-0"><p className="truncate text-sm font-semibold">漫屿AIGC 系统后台</p><p className="truncate text-[11px] text-text-muted">{current.label}</p></div><div className="flex items-center gap-2"><select aria-label="选择系统管理分区" value={section} onChange={(event) => { window.location.hash = `#/admin/${event.target.value}`; }} className="h-9 max-w-[48vw] rounded-md border border-glass-border bg-elevated px-3 text-sm outline-none focus:border-primary/60">{SECTION_GROUPS.map((group) => <optgroup key={group.label} label={group.label}>{group.sections.map((id) => <option key={id} value={id}>{SECTION_ITEMS.find((item) => item.id === id)?.label}</option>)}</optgroup>)}</select><button type="button" onClick={() => void logout()} disabled={loggingOut} title="退出系统后台" aria-label="退出系统后台" className="grid h-9 w-9 shrink-0 place-items-center rounded-md border border-glass-border text-text-secondary">{loggingOut ? <Loader2 size={15} className="animate-spin" /> : <LogOut size={15} />}</button></div></header>
        <main className={`min-h-0 flex-1 overflow-y-auto ${section === "tickets" || section === "configuration" ? "" : "px-4 py-6 md:px-8"}`} aria-label={current.label}><div className={section === "tickets" || section === "configuration" ? "min-h-full" : "mx-auto max-w-[1380px]"}>{content}</div></main>
      </div>
    </div>
  );
}
