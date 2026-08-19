"use client";

import { useCallback, useEffect, useId, useState, type FormEvent } from "react";
import {
  Archive,
  ArrowDown,
  ArrowUp,
  Eye,
  EyeOff,
  ImagePlus,
  Loader2,
  Pencil,
  Plus,
  RefreshCw,
  X,
} from "lucide-react";

import {
  getSafeApiError,
  systemSceneAdminApi,
  type AdminPage,
  type SystemScene,
  type SystemScenePayload,
} from "@/lib/api";
import { adminFilter, replaceAdminFilters } from "@/lib/adminRoute";
import { toast } from "@/store/toastStore";
import AdminPagination from "./AdminPagination";
import { AdminReasonDialog } from "./AdminDialogs";

const EMPTY_SCENE: SystemScenePayload = {
  name: "",
  description: "",
  category: "",
  tags: [],
  prompt: "",
  negative_prompt: "",
  style: "通用",
  aspect_ratio: "16:9",
  visibility: "disabled",
  sort_order: 100,
  schema_version: 1,
  cover_media_id: null,
};

const EMPTY_PAGE: AdminPage<SystemScene> = { items: [], total: 0, offset: 0, limit: 30 };
const HAS_CHINESE = /[\u3400-\u9fff]/;

function Cover({ scene, className = "h-20 w-32" }: { scene: SystemScene; className?: string }) {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!scene.cover_media_id) { setUrl(null); return; }
    let active = true;
    systemSceneAdminApi.mediaAccess(String(scene.cover_media_id))
      .then((result) => { if (active) setUrl(result.url); })
      .catch(() => { if (active) setUrl(null); });
    return () => { active = false; };
  }, [scene.cover_media_id]);
  return (
    <div className={`${className} shrink-0 overflow-hidden rounded-md border border-glass-border bg-surface-inset`}>
      {url ? <img src={url} alt={`${scene.name}封面`} className="h-full w-full object-cover" /> : <div className="grid h-full place-items-center text-xs text-text-muted">无封面</div>}
    </div>
  );
}

function LocalCoverPreview({ file }: { file: File }) {
  const [url, setUrl] = useState("");
  useEffect(() => {
    const next = URL.createObjectURL(file);
    setUrl(next);
    return () => URL.revokeObjectURL(next);
  }, [file]);
  return url ? <img src={url} alt="待上传封面预览" className="h-24 w-40 rounded-md border border-glass-border object-cover" /> : null;
}

type SceneErrors = Partial<Record<"name" | "description" | "category" | "prompt" | "tags" | "file" | "reason", string>>;

function SceneEditor({
  scene,
  onClose,
  onSaved,
  onConflict,
}: {
  scene?: SystemScene | null;
  onClose: () => void;
  onSaved: (scene: SystemScene) => void;
  onConflict: (scene: SystemScene) => void;
}) {
  const [value, setValue] = useState<SystemScenePayload>(scene ? {
    name: scene.name,
    description: scene.description,
    category: scene.category,
    tags: scene.tags,
    prompt: scene.prompt,
    negative_prompt: scene.negative_prompt,
    style: scene.style,
    aspect_ratio: scene.aspect_ratio,
    visibility: scene.visibility,
    sort_order: scene.sort_order,
    schema_version: scene.schema_version,
    cover_media_id: scene.cover_media_id,
  } : EMPTY_SCENE);
  const [tags, setTags] = useState(value.tags.join("，"));
  const [reason, setReason] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [errors, setErrors] = useState<SceneErrors>({});
  const [busy, setBusy] = useState(false);
  const titleId = useId();
  const update = <K extends keyof SystemScenePayload>(key: K, next: SystemScenePayload[K]) => {
    setValue((current) => ({ ...current, [key]: next }));
    setErrors((current) => ({ ...current, [key]: undefined }));
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const normalizedTags = tags.split(/[，,]/).map((item) => item.trim()).filter(Boolean);
    const nextErrors: SceneErrors = {};
    if (!HAS_CHINESE.test(value.name)) nextErrors.name = "名称必须包含中文";
    if (!HAS_CHINESE.test(value.description)) nextErrors.description = "描述必须包含中文";
    if (!value.category.trim()) nextErrors.category = "请填写场景分类";
    if (!value.prompt.trim()) nextErrors.prompt = "请填写生成提示词";
    if (normalizedTags.length > 20 || normalizedTags.some((item) => item.length > 40)) nextErrors.tags = "最多 20 个标签，每个不超过 40 个字符";
    if (!HAS_CHINESE.test(reason)) nextErrors.reason = "操作原因必须包含中文";
    if (file && (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type) || file.size > 10 * 1024 * 1024)) nextErrors.file = "仅支持 10MB 以内的 PNG、JPEG 或 WebP 图片";
    setErrors(nextErrors);
    if (Object.keys(nextErrors).length > 0) return;

    setBusy(true);
    try {
      let mediaId = value.cover_media_id;
      if (file) mediaId = Number((await systemSceneAdminApi.uploadMedia(file, reason.trim())).media_id);
      const payload = { ...value, cover_media_id: mediaId, tags: normalizedTags };
      const saved = scene
        ? await systemSceneAdminApi.update(scene.id, payload, scene.version, reason.trim())
        : await systemSceneAdminApi.create(payload, reason.trim());
      toast.success(scene ? "系统场景已更新" : "系统场景已创建");
      onSaved(saved);
    } catch (error) {
      const safe = getSafeApiError(error);
      if (scene && safe.code === "SYSTEM_SCENE_VERSION_CONFLICT") {
        const current = await systemSceneAdminApi.get(scene.id);
        toast.error("场景已被其他操作更新，已载入最新版本");
        onConflict(current);
      } else toast.error("系统场景保存失败", { body: safe.message });
    } finally {
      setBusy(false);
    }
  };

  const fieldError = (field: keyof SceneErrors) => errors[field] ? <p role="alert" className="mt-1 text-xs text-red-200">{errors[field]}</p> : null;
  return (
    <div className="fixed inset-0 z-[150] grid place-items-center bg-black/75 px-4 py-6 backdrop-blur-sm">
      <section role="dialog" aria-modal="true" aria-labelledby={titleId} className="max-h-full w-full max-w-3xl overflow-y-auto rounded-md border border-glass-border bg-elevated shadow-2xl">
        <header className="sticky top-0 z-10 flex items-start justify-between border-b border-glass-border bg-elevated px-5 py-4">
          <div><h2 id={titleId} className="font-semibold">{scene ? "编辑系统场景" : "新建系统场景"}</h2><p className="mt-1 text-sm text-text-secondary">平台模板对普通用户只读，复制后保留当前版本快照。</p></div>
          <button type="button" aria-label="关闭" title="关闭" onClick={onClose} disabled={busy} className="grid h-8 w-8 place-items-center rounded-md hover:bg-hover-bg"><X size={17} /></button>
        </header>
        <form onSubmit={submit} className="space-y-5 px-5 py-5">
          <div className="grid gap-4 sm:grid-cols-2">
            <label className="text-sm">中文名称<input autoFocus aria-invalid={!!errors.name} value={value.name} onChange={(event) => update("name", event.target.value)} maxLength={120} className="mt-2 h-10 w-full rounded-md border border-glass-border bg-input-bg px-3" />{fieldError("name")}</label>
            <label className="text-sm">分类<input aria-invalid={!!errors.category} value={value.category} onChange={(event) => update("category", event.target.value)} maxLength={80} className="mt-2 h-10 w-full rounded-md border border-glass-border bg-input-bg px-3" />{fieldError("category")}</label>
          </div>
          <label className="block text-sm">中文描述<textarea aria-invalid={!!errors.description} value={value.description} onChange={(event) => update("description", event.target.value)} rows={3} className="mt-2 w-full resize-none rounded-md border border-glass-border bg-input-bg px-3 py-2" />{fieldError("description")}</label>
          <label className="block text-sm">生成提示词<textarea aria-invalid={!!errors.prompt} value={value.prompt} onChange={(event) => update("prompt", event.target.value)} rows={4} className="mt-2 w-full resize-y rounded-md border border-glass-border bg-input-bg px-3 py-2 font-mono text-xs" />{fieldError("prompt")}</label>
          <label className="block text-sm">负面提示词<textarea value={value.negative_prompt} onChange={(event) => update("negative_prompt", event.target.value)} rows={2} className="mt-2 w-full resize-y rounded-md border border-glass-border bg-input-bg px-3 py-2 font-mono text-xs" /></label>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <label className="text-sm">风格<input value={value.style} onChange={(event) => update("style", event.target.value)} className="mt-2 h-10 w-full rounded-md border border-glass-border bg-input-bg px-3" /></label>
            <label className="text-sm">画幅<select value={value.aspect_ratio} onChange={(event) => update("aspect_ratio", event.target.value as SystemScenePayload["aspect_ratio"])} className="mt-2 h-10 w-full rounded-md border border-glass-border bg-elevated px-3"><option>16:9</option><option>9:16</option><option>1:1</option><option>4:3</option><option>3:4</option></select></label>
            <label className="text-sm">初始状态<select value={value.visibility} onChange={(event) => update("visibility", event.target.value as "enabled" | "disabled")} className="mt-2 h-10 w-full rounded-md border border-glass-border bg-elevated px-3"><option value="disabled">停用</option><option value="enabled">启用</option></select></label>
            <label className="text-sm">排序<input type="number" min={0} max={1_000_000} value={value.sort_order} onChange={(event) => update("sort_order", Number(event.target.value))} className="mt-2 h-10 w-full rounded-md border border-glass-border bg-input-bg px-3 font-mono" /></label>
          </div>
          <label className="block text-sm">标签<input aria-invalid={!!errors.tags} value={tags} onChange={(event) => { setTags(event.target.value); setErrors((current) => ({ ...current, tags: undefined })); }} placeholder="用逗号分隔，最多 20 个" className="mt-2 h-10 w-full rounded-md border border-glass-border bg-input-bg px-3" />{fieldError("tags")}</label>
          <div className="grid gap-4 sm:grid-cols-[auto_1fr] sm:items-end">
            {file ? <LocalCoverPreview file={file} /> : scene ? <Cover scene={scene} className="h-24 w-40" /> : <div className="grid h-24 w-40 place-items-center rounded-md border border-dashed border-glass-border text-xs text-text-muted">无封面</div>}
            <label className="block text-sm">{scene?.cover_media_id ? "替换场景封面" : "场景封面"}<input type="file" accept="image/png,image/jpeg,image/webp" onChange={(event) => { setFile(event.target.files?.[0] || null); setErrors((current) => ({ ...current, file: undefined })); }} className="mt-2 block w-full rounded-md border border-dashed border-glass-border bg-input-bg px-3 py-3 text-sm file:mr-3 file:rounded file:border-0 file:bg-primary/15 file:px-3 file:py-1.5 file:text-primary" />{fieldError("file")}</label>
          </div>
          <label className="block text-sm">操作原因<textarea aria-invalid={!!errors.reason} value={reason} onChange={(event) => { setReason(event.target.value); setErrors((current) => ({ ...current, reason: undefined })); }} rows={2} className="mt-2 w-full resize-none rounded-md border border-glass-border bg-input-bg px-3 py-2" />{fieldError("reason")}</label>
          <footer className="flex justify-end gap-2 border-t border-glass-border pt-4"><button type="button" onClick={onClose} disabled={busy} className="h-9 rounded-md border border-glass-border px-3 text-sm">取消</button><button type="submit" disabled={busy} className="inline-flex h-9 items-center gap-2 rounded-md bg-primary px-3 text-sm font-medium text-on-accent disabled:opacity-40">{busy ? <Loader2 size={15} className="animate-spin" /> : <ImagePlus size={15} />}保存场景</button></footer>
        </form>
      </section>
    </div>
  );
}

type SceneAction = { kind: "enable" | "disable" | "archive" | "reorder_up" | "reorder_down"; scene: SystemScene };

export default function SystemScenesAdminPage({ sceneId, queryString = "" }: { sceneId?: string | null; queryString?: string }) {
  const initialOffset = Number(adminFilter(queryString, "offset"));
  const initialLimit = Number(adminFilter(queryString, "limit"));
  const [page, setPage] = useState<AdminPage<SystemScene>>({
    ...EMPTY_PAGE,
    offset: Number.isSafeInteger(initialOffset) && initialOffset >= 0 ? initialOffset : 0,
    limit: [20, 30, 50, 100].includes(initialLimit) ? initialLimit : 30,
  });
  const [filterId, setFilterId] = useState(() => adminFilter(queryString, "scene_id"));
  const [query, setQuery] = useState(() => adminFilter(queryString, "query"));
  const [category, setCategory] = useState(() => adminFilter(queryString, "category"));
  const [tag, setTag] = useState(() => adminFilter(queryString, "tag"));
  const [visibility, setVisibility] = useState(() => adminFilter(queryString, "visibility"));
  const [lifecycle, setLifecycle] = useState(() => adminFilter(queryString, "lifecycle"));
  const [schemaVersion, setSchemaVersion] = useState(() => adminFilter(queryString, "schema_version"));
  const [startAt, setStartAt] = useState(() => adminFilter(queryString, "start_at"));
  const [endAt, setEndAt] = useState(() => adminFilter(queryString, "end_at"));
  const [loading, setLoading] = useState(true);
  const [editor, setEditor] = useState<SystemScene | "new" | null>(null);
  const [action, setAction] = useState<SceneAction | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const result = await systemSceneAdminApi.list({
        scene_id: filterId || undefined,
        query: query.trim() || undefined,
        category: category.trim() || undefined,
        tag: tag.trim() || undefined,
        visibility: visibility as "enabled" | "disabled" || undefined,
        lifecycle: lifecycle as "active" | "archived" || undefined,
        schema_version: schemaVersion || undefined,
        start_at: startAt ? `${startAt}T00:00:00` : undefined,
        end_at: endAt ? `${endAt}T23:59:59` : undefined,
        offset: page.offset,
        limit: page.limit,
      });
      setPage(result);
      if (sceneId) setEditor(result.items.find((item) => item.id === sceneId) || await systemSceneAdminApi.get(sceneId));
    } catch (error) {
      toast.error("系统场景加载失败", { body: getSafeApiError(error).message });
    } finally { setLoading(false); }
  }, [category, endAt, filterId, lifecycle, page.limit, page.offset, query, sceneId, schemaVersion, startAt, tag, visibility]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    if (sceneId) return;
    replaceAdminFilters("system-scenes", {
      scene_id: filterId, query: query.trim(), category: category.trim(), tag: tag.trim(),
      visibility, lifecycle, schema_version: schemaVersion, start_at: startAt, end_at: endAt,
      offset: page.offset || undefined, limit: page.limit === 30 ? undefined : page.limit,
    });
  }, [category, endAt, filterId, lifecycle, page.limit, page.offset, query, sceneId, schemaVersion, startAt, tag, visibility]);

  const runAction = async (reason: string) => {
    if (!action) return;
    setBusy(true);
    try {
      const count = action.scene.usage_count || 0;
      if (action.kind === "archive") await systemSceneAdminApi.archive(action.scene.id, action.scene.version, reason, count);
      else if (action.kind === "enable" || action.kind === "disable") await systemSceneAdminApi.visibility(action.scene.id, action.kind === "enable" ? "enabled" : "disabled", action.scene.version, reason, count);
      else await systemSceneAdminApi.reorder(action.scene.id, Math.max(0, action.scene.sort_order + (action.kind === "reorder_up" ? -10 : 10)), action.scene.version, reason);
      toast.success("系统场景状态已更新");
      setAction(null);
      await load();
    } catch (error) {
      const safe = getSafeApiError(error);
      if (safe.code === "SYSTEM_SCENE_VERSION_CONFLICT") {
        setAction(null);
        await load();
        toast.error("场景已发生变化，已刷新最新版本");
      } else toast.error("系统场景操作失败", { body: safe.message });
    } finally { setBusy(false); }
  };

  const resetOffset = () => setPage((current) => ({ ...current, offset: 0 }));
  const actionTitle = action?.kind === "enable" ? "启用系统场景" : action?.kind === "disable" ? "停用系统场景" : action?.kind === "archive" ? "归档系统场景" : "调整系统场景排序";
  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-4 border-b border-glass-border pb-5"><div><p className="font-mono text-xs text-primary">内容与资产</p><h1 className="mt-1 text-xl font-semibold">系统场景</h1><p className="mt-1 text-sm text-text-secondary">维护平台场景模板、封面、排序和用户引用保护</p></div><div className="flex gap-2"><button type="button" aria-label="刷新" title="刷新" onClick={() => void load()} className="grid h-9 w-9 place-items-center rounded-md border border-glass-border"><RefreshCw size={16} className={loading ? "animate-spin" : ""} /></button><button type="button" onClick={() => setEditor("new")} className="inline-flex h-9 items-center gap-2 rounded-md bg-primary px-3 text-sm text-on-accent"><Plus size={15} />新建场景</button></div></header>
      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
        <input inputMode="numeric" value={filterId} onChange={(event) => { setFilterId(event.target.value.replace(/\D/g, "")); resetOffset(); }} placeholder="场景 ID" className="h-9 rounded-md border border-glass-border bg-input-bg px-3 text-sm" />
        <input value={query} onChange={(event) => { setQuery(event.target.value); resetOffset(); }} placeholder="场景名称" className="h-9 rounded-md border border-glass-border bg-input-bg px-3 text-sm" />
        <input value={category} onChange={(event) => { setCategory(event.target.value); resetOffset(); }} placeholder="分类" className="h-9 rounded-md border border-glass-border bg-input-bg px-3 text-sm" />
        <input value={tag} onChange={(event) => { setTag(event.target.value); resetOffset(); }} placeholder="标签" className="h-9 rounded-md border border-glass-border bg-input-bg px-3 text-sm" />
        <select value={visibility} onChange={(event) => { setVisibility(event.target.value); resetOffset(); }} className="h-9 rounded-md border border-glass-border bg-elevated px-3 text-sm"><option value="">全部可见状态</option><option value="enabled">已启用</option><option value="disabled">已停用</option></select>
        <select value={lifecycle} onChange={(event) => { setLifecycle(event.target.value); resetOffset(); }} className="h-9 rounded-md border border-glass-border bg-elevated px-3 text-sm"><option value="">全部生命周期</option><option value="active">正常</option><option value="archived">已归档</option></select>
        <input inputMode="numeric" value={schemaVersion} onChange={(event) => { setSchemaVersion(event.target.value.replace(/\D/g, "")); resetOffset(); }} placeholder="Schema 版本" className="h-9 rounded-md border border-glass-border bg-input-bg px-3 text-sm" />
        <label className="flex items-center gap-2 text-xs text-text-muted">创建开始<input type="date" value={startAt} onChange={(event) => { setStartAt(event.target.value); resetOffset(); }} className="h-9 min-w-0 flex-1 rounded-md border border-glass-border bg-elevated px-2 text-sm text-foreground" /></label>
        <label className="flex items-center gap-2 text-xs text-text-muted">创建结束<input type="date" value={endAt} onChange={(event) => { setEndAt(event.target.value); resetOffset(); }} className="h-9 min-w-0 flex-1 rounded-md border border-glass-border bg-elevated px-2 text-sm text-foreground" /></label>
      </div>
      <p className="font-mono text-xs text-text-muted">共 {page.total} 个系统场景</p>
      {loading ? <div className="grid min-h-56 place-items-center text-text-muted">正在加载系统场景</div> : page.items.length === 0 ? <div className="grid min-h-44 place-items-center border-y border-glass-border text-sm text-text-muted">没有符合条件的系统场景</div> : <div className="divide-y divide-glass-border border-y border-glass-border">{page.items.map((scene) => <article key={scene.id} className="flex flex-col gap-4 py-4 xl:flex-row xl:items-center"><Cover scene={scene} /><div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-2"><h2 className="text-sm font-medium">{scene.name}</h2><span className={`rounded border px-2 py-0.5 text-xs ${scene.visibility === "enabled" ? "border-emerald-400/25 bg-emerald-400/10 text-emerald-200" : "border-glass-border text-text-muted"}`}>{scene.visibility === "enabled" ? "已启用" : "已停用"}</span>{scene.lifecycle === "archived" && <span className="rounded border border-red-400/25 px-2 py-0.5 text-xs text-red-200">已归档</span>}</div><p className="mt-1 line-clamp-2 text-sm text-text-secondary">{scene.description}</p><p className="mt-2 font-mono text-xs text-text-muted">#{scene.id} · {scene.category} · Schema {scene.schema_version} · V{scene.version} · 排序 {scene.sort_order} · 引用 {scene.usage_count || 0}</p><p className="mt-1 text-xs text-text-muted">{scene.tags.length ? scene.tags.join(" · ") : "无标签"} · 创建于 {new Date(scene.created_at).toLocaleDateString("zh-CN")}</p></div><div className="flex flex-wrap gap-2 xl:justify-end"><button type="button" title="向前排序" aria-label={`向前排序${scene.name}`} disabled={scene.lifecycle === "archived" || scene.sort_order === 0} onClick={() => setAction({ kind: "reorder_up", scene })} className="grid h-9 w-9 place-items-center rounded-md border border-glass-border disabled:opacity-30"><ArrowUp size={15} /></button><button type="button" title="向后排序" aria-label={`向后排序${scene.name}`} disabled={scene.lifecycle === "archived"} onClick={() => setAction({ kind: "reorder_down", scene })} className="grid h-9 w-9 place-items-center rounded-md border border-glass-border disabled:opacity-30"><ArrowDown size={15} /></button><button type="button" title="编辑" aria-label={`编辑${scene.name}`} disabled={scene.lifecycle === "archived"} onClick={() => setEditor(scene)} className="grid h-9 w-9 place-items-center rounded-md border border-glass-border hover:bg-hover-bg disabled:opacity-30"><Pencil size={15} /></button>{scene.visibility === "enabled" ? <button type="button" title="停用" aria-label={`停用${scene.name}`} onClick={() => setAction({ kind: "disable", scene })} className="grid h-9 w-9 place-items-center rounded-md border border-amber-400/25 text-amber-200"><EyeOff size={15} /></button> : <button type="button" title="启用" aria-label={`启用${scene.name}`} disabled={scene.lifecycle === "archived"} onClick={() => setAction({ kind: "enable", scene })} className="grid h-9 w-9 place-items-center rounded-md border border-emerald-400/25 text-emerald-200 disabled:opacity-30"><Eye size={15} /></button>}<button type="button" title="归档" aria-label={`归档${scene.name}`} disabled={scene.lifecycle === "archived"} onClick={() => setAction({ kind: "archive", scene })} className="grid h-9 w-9 place-items-center rounded-md border border-red-400/25 text-red-200 disabled:opacity-30"><Archive size={15} /></button></div></article>)}</div>}
      <AdminPagination offset={page.offset} limit={page.limit} total={page.total} onChange={(offset) => setPage((current) => ({ ...current, offset }))} onLimitChange={(limit) => setPage((current) => ({ ...current, limit, offset: 0 }))} />
      {editor && <SceneEditor key={editor === "new" ? "new" : `${editor.id}:${editor.version}`} scene={editor === "new" ? null : editor} onClose={() => { setEditor(null); if (sceneId) window.location.hash = "#/admin/system-scenes"; }} onSaved={() => { setEditor(null); if (sceneId) window.location.hash = "#/admin/system-scenes"; void load(); }} onConflict={(current) => setEditor(current)} />}
      {action && <AdminReasonDialog title={actionTitle} description={action.scene.usage_count && ["disable", "archive"].includes(action.kind) ? `当前已有 ${action.scene.usage_count} 个用户副本。现有副本及版本快照不会被修改或删除。` : action.kind.startsWith("reorder") ? `排序值将从 ${action.scene.sort_order} 调整为 ${Math.max(0, action.scene.sort_order + (action.kind === "reorder_up" ? -10 : 10))}。` : "此操作将记录场景版本和审计信息。"} confirmLabel={action.kind === "enable" ? "确认启用" : action.kind === "disable" ? "确认停用" : action.kind === "archive" ? "确认归档" : "确认排序"} danger={action.kind === "disable" || action.kind === "archive"} busy={busy} onClose={() => setAction(null)} onConfirm={runAction} />}
    </div>
  );
}
