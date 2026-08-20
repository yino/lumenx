"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  addEdge,
  Background,
  BackgroundVariant,
  BaseEdge,
  Controls,
  EdgeLabelRenderer,
  getBezierPath,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useEdgesState,
  useNodesState,
  useReactFlow,
  type Connection,
  type Edge,
  type EdgeProps,
  type Node,
  type NodeProps,
  type OnMoveEnd,
  type Viewport,
} from "@xyflow/react";
import {
  CircleAlert,
  CircleCheckBig,
  Copy,
  ImageIcon,
  Layers3,
  LayoutDashboard,
  LoaderCircle,
  ListTodo,
  Maximize2,
  MessageSquareText,
  Play,
  RotateCcw,
  Sparkles,
  Trash2,
  Type,
  Video,
  WandSparkles,
} from "lucide-react";
import { useTranslations } from "next-intl";
import { clsx } from "clsx";
import {
  CompositionSafeInput,
  CompositionSafeTextarea,
} from "@/components/canvas/CompositionSafeField";
import {
  buildBatchTaskPlan,
  collectUpstreamTextInputs,
  resolveTaskDependencies,
  resolveUpstreamMedia,
  resolveUpstreamPrompt,
} from "@/components/canvas/canvasWorkflow";
import { API_URL, canvasApi, getSafeApiError, playgroundApi, type PlaygroundGenerationResponse } from "@/lib/api";
import { IS_CLOUD_DEPLOYMENT } from "@/lib/deployment";
import { getAssetUrl } from "@/lib/utils";
import { getModelsForMode } from "@/components/modules/playground/playgroundModels";

type CreativeItemKind = "note" | "prompt" | "composer" | "image" | "video" | "task";
type TaskStatus = "pending" | "running" | "done" | "failed";
type CanvasTaskMode = "t2i" | "t2v" | "i2v";
type ComposeTarget = "image" | "video";
type ComposeStatus = "idle" | "running" | "done" | "failed";

interface StudioNodeData extends Record<string, unknown> {
  kind: CreativeItemKind;
  title: string;
  isCustom?: boolean;
  content?: string;
  contentEn?: string;
  composeTarget?: ComposeTarget;
  composeInstruction?: string;
  composeStatus?: ComposeStatus;
  composeError?: string;
  composedInputNodeIds?: string[];
  mediaUrl?: string;
  mediaReference?: string;
  taskStatus?: TaskStatus;
  taskMode?: CanvasTaskMode;
  modelId?: string;
  duration?: number;
  aspectRatio?: string;
  resolution?: string;
  imageSize?: string;
  generationId?: string;
  providerTaskId?: string;
  taskError?: string;
  sourceTaskId?: string;
  sourcePromptNodeId?: string;
  sourceMediaNodeId?: string;
}

type StudioNode = Node<StudioNodeData>;

interface PersistedCanvasLayout {
  version?: 2 | 3;
  positions?: Record<string, { x: number; y: number }>;
  viewport?: Viewport;
  customNodes?: StudioNode[];
  customEdges?: Edge[];
}

interface FreeCanvasPageProps {
  workspaceId?: string | null;
}

interface CanvasNodeActions {
  updateNodeData: (id: string, patch: Partial<StudioNodeData>) => void;
  duplicateNode: (id: string) => void;
  deleteNode: (id: string) => void;
  deleteEdge: (id: string) => void;
  runTask: (id: string) => void;
  composePrompt: (id: string) => Promise<boolean>;
}

const CanvasNodeActionsContext = createContext<CanvasNodeActions | null>(null);

const CREATIVE_NODE_WIDTH = 304;
const CANVAS_TASK_MODELS: Record<CanvasTaskMode, ReturnType<typeof getModelsForMode>> = {
  t2i: getModelsForMode("t2i"),
  t2v: getModelsForMode("t2v"),
  i2v: getModelsForMode("i2v"),
};
const DEFAULT_CANVAS_MODELS: Record<CanvasTaskMode, string> = {
  t2i: "wan2.7-image-pro",
  t2v: "seedance-2.0-t2v",
  i2v: "seedance-2.0-i2v",
};
const TASK_POLL_INTERVAL_MS = 2_000;
const TASK_POLL_LIMIT = 450;

const CREATIVE_NODE_META: Record<
  CreativeItemKind,
  { icon: typeof Type; accent: string; surface: string }
> = {
  note: {
    icon: Type,
    accent: "text-sky-300",
    surface: "border-sky-400/25 bg-sky-400/10",
  },
  prompt: {
    icon: WandSparkles,
    accent: "text-violet-300",
    surface: "border-violet-400/25 bg-violet-400/10",
  },
  composer: {
    icon: Sparkles,
    accent: "text-fuchsia-300",
    surface: "border-fuchsia-400/25 bg-fuchsia-400/10",
  },
  image: {
    icon: ImageIcon,
    accent: "text-emerald-300",
    surface: "border-emerald-400/25 bg-emerald-400/10",
  },
  video: {
    icon: Video,
    accent: "text-cyan-300",
    surface: "border-cyan-400/25 bg-cyan-400/10",
  },
  task: {
    icon: ListTodo,
    accent: "text-amber-300",
    surface: "border-amber-400/25 bg-amber-400/10",
  },
};

function isCustomNode(node: StudioNode): boolean {
  return node.data.isCustom === true;
}

function isCustomEdge(edge: Edge): boolean {
  return edge.id.startsWith("custom-edge:");
}

function asCanvasCurve(edge: Edge): Edge {
  return {
    ...edge,
    type: "deletable",
    animated: false,
    markerEnd: {
      type: MarkerType.ArrowClosed,
      width: 14,
      height: 14,
      color: "var(--color-accent)",
    },
    style: {
      ...edge.style,
      stroke: "var(--color-accent)",
      strokeWidth: 1.6,
      strokeLinecap: "round",
      opacity: 0.78,
    },
  };
}

function waitForNextPoll(): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, TASK_POLL_INTERVAL_MS));
}

function normalizeTaskMode(value: unknown): CanvasTaskMode {
  return value === "t2i" || value === "i2v" || value === "t2v" ? value : "t2v";
}

function getPreferredCanvasResolution(
  model: ReturnType<typeof getModelsForMode>[number] | undefined,
): string {
  if (model?.params.resolution?.options.includes("720p")) return "720p";
  return model?.params.resolution?.default || "720p";
}

function getDefaultTaskData(mode: CanvasTaskMode): Partial<StudioNodeData> {
  const model = CANVAS_TASK_MODELS[mode].find((candidate) => (
    candidate.id === DEFAULT_CANVAS_MODELS[mode]
  )) || CANVAS_TASK_MODELS[mode][0];
  const duration = model?.duration
    ? model.duration.type === "fixed"
      ? model.duration.value
      : model.duration.default
    : 5;

  return {
    taskMode: mode,
    modelId: model?.id || DEFAULT_CANVAS_MODELS[mode],
    duration,
    aspectRatio: model?.params.ratio?.default || "16:9",
    resolution: getPreferredCanvasResolution(model),
    imageSize: model?.params.size?.default || "1280*1280",
  };
}

function resolveCanvasOutput(
  generation: PlaygroundGenerationResponse,
  expectedKind: "image" | "video",
): { mediaUrl: string; kind: "image" | "video"; mediaReference: string } | null {
  const output = generation.outputs.find((item) => item.media_type === expectedKind)
    || generation.outputs[0];
  if (!output) return null;
  const mediaReference = output.media_reference || output.media_path || "";
  if (output.media_url) {
    return { mediaUrl: output.media_url, kind: expectedKind, mediaReference };
  }

  if (mediaReference.startsWith("output/")) {
    return {
      mediaUrl: `${API_URL}/files/${mediaReference.slice("output/".length)}`,
      kind: expectedKind,
      mediaReference,
    };
  }
  const mediaUrl = getAssetUrl(mediaReference);
  return mediaUrl ? { mediaUrl, kind: expectedKind, mediaReference } : null;
}

function resolveCanvasMediaInput(media: {
  mediaUrl: string;
  mediaReference?: string;
}): string {
  if (media.mediaReference) return media.mediaReference;
  const localFilesPrefix = `${API_URL}/files/`;
  if (media.mediaUrl.startsWith(localFilesPrefix)) {
    return `output/${media.mediaUrl.slice(localFilesPrefix.length)}`;
  }
  return media.mediaUrl;
}

function makeId(prefix: string): string {
  const suffix = typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return `${prefix}:${suffix}`;
}

function readLayout(storageKey: string): PersistedCanvasLayout {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(storageKey);
    return raw ? (JSON.parse(raw) as PersistedCanvasLayout) : {};
  } catch {
    return {};
  }
}

function writeLayout(storageKey: string, layout: PersistedCanvasLayout): void {
  try {
    window.localStorage.setItem(storageKey, JSON.stringify(layout));
  } catch {
    // The canvas remains usable when private browsing blocks localStorage.
  }
}

function makeLayoutSnapshot(
  nodes: StudioNode[],
  edges: Edge[],
  viewport?: Viewport,
): PersistedCanvasLayout {
  return {
    version: 3,
    viewport,
    positions: Object.fromEntries(
      nodes.map((node) => [node.id, { x: node.position.x, y: node.position.y }]),
    ),
    customNodes: nodes.filter(isCustomNode).map((node) => ({
      id: node.id,
      type: "creative",
      position: node.position,
      data: node.data,
    })),
    customEdges: edges.filter(isCustomEdge).map(asCanvasCurve),
  };
}

function CreativeCanvasNode({ id, data, selected }: NodeProps<StudioNode>) {
  const t = useTranslations("canvas");
  const actions = useContext(CanvasNodeActionsContext);
  const kind = data.kind as CreativeItemKind;
  const meta = CREATIVE_NODE_META[kind] || CREATIVE_NODE_META.note;
  const KindIcon = meta.icon;
  const taskStatus = data.taskStatus || "pending";
  const taskRunning = taskStatus === "running";
  const composeStatus = data.composeStatus || "idle";
  const composeRunning = composeStatus === "running";
  const taskMode = normalizeTaskMode(data.taskMode);
  const taskModels = CANVAS_TASK_MODELS[taskMode];
  const selectedTaskModel = taskModels.find((model) => model.id === data.modelId)
    || taskModels[0];
  const durationRange = selectedTaskModel?.duration?.type === "slider"
    ? selectedTaskModel.duration
    : null;
  const durationOptions = selectedTaskModel?.duration?.type === "buttons"
    ? selectedTaskModel.duration.options
    : null;
  const resolutionOptions = selectedTaskModel?.params.resolution?.options
    || ["720p", "1080p"];
  const ratioOptions = selectedTaskModel?.params.ratio?.options
    || ["16:9", "9:16", "1:1"];
  const imageSizeOptions = selectedTaskModel?.params.size?.options
    || ["1280*1280", "1280*720", "720*1280"];
  const TaskStatusIcon = taskStatus === "running"
    ? LoaderCircle
    : taskStatus === "done"
      ? CircleCheckBig
      : taskStatus === "failed"
        ? CircleAlert
        : Play;

  const update = (patch: Partial<StudioNodeData>) => actions?.updateNodeData(id, patch);
  const changeTaskMode = (mode: CanvasTaskMode) => update({
    ...getDefaultTaskData(mode),
    taskStatus: "pending",
    taskError: undefined,
    generationId: undefined,
  });
  const changeTaskModel = (modelId: string) => {
    const model = taskModels.find((candidate) => candidate.id === modelId);
    const nextDuration = model?.duration
      ? model.duration.type === "fixed" ? model.duration.value : model.duration.default
      : data.duration || 5;
    update({
      modelId,
      duration: nextDuration,
      resolution: getPreferredCanvasResolution(model),
      aspectRatio: model?.params.ratio?.default || data.aspectRatio || "16:9",
      imageSize: model?.params.size?.default || data.imageSize || "1280*1280",
      taskStatus: "pending",
      taskError: undefined,
      generationId: undefined,
    });
  };

  return (
    <article
      className={clsx(
        "free-canvas-node relative overflow-hidden rounded-2xl border bg-elevated/95 shadow-2xl backdrop-blur-xl transition-[border-color,box-shadow] duration-200",
        selected
          ? "border-accent/70 shadow-[0_0_0_2px_color-mix(in_srgb,var(--color-accent)_18%,transparent),0_24px_60px_-32px_var(--color-accent)]"
          : "border-glass-border hover:border-foreground/25",
      )}
      style={{ width: CREATIVE_NODE_WIDTH }}
    >
      <Handle
        type="target"
        position={Position.Left}
        isConnectableStart={false}
        className="!h-3 !w-3 !border-2 !border-background !bg-text-muted"
      />

      <header className="flex items-center gap-2.5 border-b border-glass-border px-3 py-2.5">
        <div className={clsx("grid h-8 w-8 flex-none place-items-center rounded-lg border", meta.surface, meta.accent)}>
          <KindIcon size={15} />
        </div>
        <CompositionSafeInput
          value={data.title}
          onValueChange={(value) => update({ title: value })}
          className="nodrag nopan nowheel min-w-0 flex-1 bg-transparent text-sm font-semibold text-foreground outline-none placeholder:text-text-muted"
          aria-label={t("nodeTitle")}
        />
        <button
          type="button"
          onClick={() => actions?.duplicateNode(id)}
          className="nodrag grid h-7 w-7 flex-none place-items-center rounded-lg text-text-muted transition-colors hover:bg-hover-bg hover:text-foreground"
          aria-label={`${t("duplicateNode")}：${data.title}`}
          title={t("duplicateNode")}
        >
          <Copy size={13} />
        </button>
        <button
          type="button"
          onClick={() => actions?.deleteNode(id)}
          className="nodrag grid h-7 w-7 flex-none place-items-center rounded-lg text-text-muted transition-colors hover:bg-status-failed-bg hover:text-status-failed-fg"
          aria-label={`${t("deleteNode")}：${data.title}`}
          title={t("deleteNode")}
        >
          <Trash2 size={13} />
        </button>
      </header>

      <div className="p-3">
        {(kind === "note" || kind === "prompt") && (
          <CompositionSafeTextarea
            value={data.content || ""}
            onValueChange={(value) => update({ content: value })}
            placeholder={kind === "prompt" ? t("promptPlaceholder") : t("notePlaceholder")}
            rows={kind === "prompt" ? 7 : 5}
            className="nodrag nopan nowheel block w-full resize-none rounded-xl border border-glass-border bg-input-bg px-3 py-2.5 text-xs leading-5 text-foreground outline-none transition-colors placeholder:text-text-muted focus:border-accent/45"
          />
        )}

        {kind === "composer" && (
          <div className="space-y-2.5">
            <label className="space-y-1">
              <span className="block font-mono text-[9px] uppercase tracking-[0.08em] text-text-muted">
                {t("composeTarget")}
              </span>
              <select
                value={data.composeTarget || "image"}
                onChange={(event) => update({
                  composeTarget: event.target.value as ComposeTarget,
                  composeStatus: "idle",
                  composeError: undefined,
                })}
                disabled={composeRunning}
                className="nodrag nopan h-8 w-full rounded-lg border border-glass-border bg-input-bg px-2 text-[10px] text-foreground outline-none disabled:opacity-55"
                aria-label={t("composeTarget")}
              >
                <option value="image">{t("composeTargetImage")}</option>
                <option value="video">{t("composeTargetVideo")}</option>
              </select>
            </label>

            <CompositionSafeTextarea
              value={data.composeInstruction || ""}
              onValueChange={(value) => update({
                composeInstruction: value,
                composeStatus: "idle",
                composeError: undefined,
              })}
              placeholder={t("composeInstructionPlaceholder")}
              rows={2}
              disabled={composeRunning}
              className="nodrag nopan nowheel block w-full resize-none rounded-xl border border-glass-border bg-input-bg px-3 py-2 text-[10px] leading-4 text-foreground outline-none placeholder:text-text-muted focus:border-accent/45"
            />

            <CompositionSafeTextarea
              value={data.content || ""}
              onValueChange={(value) => update({ content: value })}
              placeholder={t("composeOutputPlaceholder")}
              rows={7}
              disabled={composeRunning}
              className="nodrag nopan nowheel block w-full resize-none rounded-xl border border-glass-border bg-input-bg px-3 py-2.5 text-xs leading-5 text-foreground outline-none placeholder:text-text-muted focus:border-accent/45"
            />

            {data.composedInputNodeIds?.length ? (
              <p className="font-mono text-[9px] text-text-muted">
                {t("composeInputCount", { count: data.composedInputNodeIds.length })}
              </p>
            ) : null}

            {data.composeError ? (
              <div className="flex items-start gap-1.5 rounded-lg border border-status-failed-border bg-status-failed-bg px-2.5 py-2 text-[10px] leading-4 text-status-failed-fg">
                <CircleAlert size={12} className="mt-0.5 flex-none" />
                <span>{data.composeError}</span>
              </div>
            ) : null}

            <button
              type="button"
              onClick={() => void actions?.composePrompt(id)}
              disabled={composeRunning}
              className="nodrag nopan inline-flex h-9 w-full items-center justify-center gap-2 rounded-xl border border-fuchsia-400/35 bg-fuchsia-400/15 px-3 text-xs font-semibold text-fuchsia-200 transition-all hover:bg-fuchsia-400/25 disabled:cursor-wait disabled:opacity-60"
            >
              {composeRunning ? <LoaderCircle size={13} className="animate-spin" /> : <Sparkles size={13} />}
              {composeRunning
                ? t("composeRunning")
                : composeStatus === "done"
                  ? t("composeRunAgain")
                  : t("composeRun")}
            </button>
          </div>
        )}

        {kind === "image" && (
          <div className="space-y-2.5">
            <div className="grid aspect-video place-items-center overflow-hidden rounded-xl border border-glass-border bg-surface-inset">
              {data.mediaUrl ? (
                <img src={data.mediaUrl} alt={data.title} className="h-full w-full object-cover" draggable={false} />
              ) : (
                <ImageIcon size={24} className="text-text-muted" />
              )}
            </div>
            <CompositionSafeInput
              value={data.mediaUrl || ""}
              onValueChange={(value) => update({ mediaUrl: value, mediaReference: undefined })}
              placeholder={t("imageUrlPlaceholder")}
              className="nodrag nopan nowheel w-full rounded-xl border border-glass-border bg-input-bg px-3 py-2 text-xs text-foreground outline-none placeholder:text-text-muted focus:border-accent/45"
            />
          </div>
        )}

        {kind === "video" && (
          <div className="space-y-2.5">
            <div className="grid aspect-video place-items-center overflow-hidden rounded-xl border border-glass-border bg-surface-inset">
              {data.mediaUrl ? (
                <video src={data.mediaUrl} controls className="nodrag nowheel h-full w-full object-cover" />
              ) : (
                <Video size={24} className="text-text-muted" />
              )}
            </div>
            <CompositionSafeInput
              value={data.mediaUrl || ""}
              onValueChange={(value) => update({ mediaUrl: value, mediaReference: undefined })}
              placeholder={t("videoUrlPlaceholder")}
              className="nodrag nopan nowheel w-full rounded-xl border border-glass-border bg-input-bg px-3 py-2 text-xs text-foreground outline-none placeholder:text-text-muted focus:border-accent/45"
            />
          </div>
        )}

        {kind === "task" && (
          <div className="space-y-2.5">
            <label className="space-y-1">
              <span className="block font-mono text-[9px] uppercase tracking-[0.08em] text-text-muted">
                {t("taskMode")}
              </span>
              <select
                value={taskMode}
                onChange={(event) => changeTaskMode(event.target.value as CanvasTaskMode)}
                disabled={taskRunning}
                className="nodrag nopan h-8 w-full rounded-lg border border-glass-border bg-input-bg px-2 text-[10px] text-foreground outline-none disabled:opacity-55"
                aria-label={t("taskMode")}
              >
                <option value="t2i">{t("taskModeT2I")}</option>
                <option value="t2v">{t("taskModeT2V")}</option>
                <option value="i2v">{t("taskModeI2V")}</option>
              </select>
            </label>

            <label className="space-y-1">
              <span className="block font-mono text-[9px] uppercase tracking-[0.08em] text-text-muted">
                {taskMode === "t2i" ? t("taskImageModel") : t("taskVideoModel")}
              </span>
              <select
                value={selectedTaskModel?.id || ""}
                onChange={(event) => changeTaskModel(event.target.value)}
                disabled={taskRunning}
                className="nodrag nopan h-8 w-full rounded-lg border border-glass-border bg-input-bg px-2 text-[10px] text-foreground outline-none disabled:opacity-55"
                aria-label={t("taskModel")}
              >
                {taskModels.map((model) => (
                  <option key={model.id} value={model.id}>{model.displayName}</option>
                ))}
              </select>
            </label>

            {taskMode === "t2i" ? (
              <label className="space-y-1">
                <span className="block font-mono text-[9px] uppercase tracking-[0.08em] text-text-muted">
                  {t("taskImageSize")}
                </span>
                <select
                  value={imageSizeOptions.includes(data.imageSize || "")
                    ? data.imageSize
                    : selectedTaskModel?.params.size?.default || imageSizeOptions[0]}
                  onChange={(event) => update({ imageSize: event.target.value })}
                  disabled={taskRunning}
                  className="nodrag nopan h-8 w-full rounded-lg border border-glass-border bg-input-bg px-2 text-[10px] text-foreground outline-none disabled:opacity-55"
                  aria-label={t("taskImageSize")}
                >
                  {imageSizeOptions.map((size) => <option key={size} value={size}>{size}</option>)}
                </select>
              </label>
            ) : (
              <div className="grid grid-cols-2 gap-2">
                <label className="space-y-1">
                  <span className="block font-mono text-[9px] uppercase tracking-[0.08em] text-text-muted">
                    {t("taskDuration")}
                  </span>
                  <div className="relative">
                    {durationOptions ? (
                      <select
                        value={data.duration || durationOptions[0]}
                        onChange={(event) => update({ duration: Number(event.target.value) })}
                        disabled={taskRunning}
                        className="nodrag nopan h-8 w-full rounded-lg border border-glass-border bg-input-bg px-2 text-[10px] text-foreground outline-none disabled:opacity-55"
                        aria-label={t("taskDuration")}
                      >
                        {durationOptions.map((duration) => <option key={duration} value={duration}>{duration}s</option>)}
                      </select>
                    ) : (
                      <input
                        type="number"
                        min={durationRange?.min || 1}
                        max={durationRange?.max || 15}
                        step={durationRange?.step || 1}
                        value={data.duration || 5}
                        onChange={(event) => update({
                          duration: Math.max(
                            durationRange?.min || 1,
                            Math.min(durationRange?.max || 15, Number(event.target.value) || 5),
                          ),
                        })}
                        disabled={taskRunning || selectedTaskModel?.duration?.type === "fixed"}
                        className="nodrag nopan h-8 w-full rounded-lg border border-glass-border bg-input-bg px-2 pr-6 font-mono text-[10px] text-foreground outline-none disabled:opacity-55"
                        aria-label={t("taskDuration")}
                      />
                    )}
                    {!durationOptions ? (
                      <span className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 font-mono text-[9px] text-text-muted">s</span>
                    ) : null}
                  </div>
                </label>
                <label className="space-y-1">
                  <span className="block font-mono text-[9px] uppercase tracking-[0.08em] text-text-muted">
                    {t("taskResolution")}
                  </span>
                  <select
                    value={resolutionOptions.includes(data.resolution || "")
                      ? data.resolution
                      : selectedTaskModel?.params.resolution?.default || resolutionOptions[0]}
                    onChange={(event) => update({ resolution: event.target.value })}
                    disabled={taskRunning}
                    className="nodrag nopan h-8 w-full rounded-lg border border-glass-border bg-input-bg px-2 text-[10px] text-foreground outline-none disabled:opacity-55"
                    aria-label={t("taskResolution")}
                  >
                    {resolutionOptions.map((resolution) => (
                      <option key={resolution} value={resolution}>{resolution}</option>
                    ))}
                  </select>
                </label>
              </div>
            )}

            {taskMode !== "t2i" ? (
              <label className="space-y-1">
                <span className="block font-mono text-[9px] uppercase tracking-[0.08em] text-text-muted">
                  {t("taskRatio")}
                </span>
                <select
                  value={ratioOptions.includes(data.aspectRatio || "")
                    ? data.aspectRatio
                    : selectedTaskModel?.params.ratio?.default || ratioOptions[0]}
                  onChange={(event) => update({ aspectRatio: event.target.value })}
                  disabled={taskRunning}
                  className="nodrag nopan h-8 w-full rounded-lg border border-glass-border bg-input-bg px-2 text-[10px] text-foreground outline-none disabled:opacity-55"
                  aria-label={t("taskRatio")}
                >
                  {ratioOptions.map((ratio) => <option key={ratio} value={ratio}>{ratio}</option>)}
                </select>
              </label>
            ) : null}

            <CompositionSafeTextarea
              value={data.content || ""}
              onValueChange={(value) => update({ content: value })}
              placeholder={t("taskPlaceholder")}
              rows={3}
              disabled={taskRunning}
              className="nodrag nopan nowheel block w-full resize-none rounded-xl border border-glass-border bg-input-bg px-3 py-2.5 text-xs leading-5 text-foreground outline-none placeholder:text-text-muted focus:border-accent/45"
            />

            {data.taskError ? (
              <div className="flex items-start gap-1.5 rounded-lg border border-status-failed-border bg-status-failed-bg px-2.5 py-2 text-[10px] leading-4 text-status-failed-fg">
                <CircleAlert size={12} className="mt-0.5 flex-none" />
                <span>{data.taskError}</span>
              </div>
            ) : null}

            {data.generationId ? (
              <p className="truncate font-mono text-[9px] text-text-muted" title={data.generationId}>
                ID · {data.generationId}
              </p>
            ) : null}

            {data.providerTaskId ? (
              <p className="truncate font-mono text-[9px] text-text-muted" title={data.providerTaskId}>
                {t("remoteTaskId")} · {data.providerTaskId}
              </p>
            ) : null}

            <button
              type="button"
              onClick={() => actions?.runTask(id)}
              disabled={taskRunning}
              className={clsx(
                "nodrag nopan inline-flex h-9 w-full items-center justify-center gap-2 rounded-xl border px-3 text-xs font-semibold transition-all",
                taskRunning
                  ? "cursor-wait border-accent/25 bg-accent/10 text-accent"
                  : taskStatus === "failed"
                    ? "border-status-failed-border bg-status-failed-bg text-status-failed-fg hover:brightness-110"
                    : "border-accent/35 bg-accent text-on-accent shadow-[0_10px_28px_-16px_var(--color-accent)] hover:brightness-110",
              )}
              aria-label={taskRunning ? t("taskGenerating") : t("taskRun")}
            >
              <TaskStatusIcon size={13} className={taskRunning ? "animate-spin" : undefined} />
              {taskRunning
                ? t("taskGenerating")
                : taskStatus === "done"
                  ? t("taskRunAgain")
                  : taskStatus === "failed"
                    ? t("taskRetry")
                    : t("taskRun")}
            </button>
          </div>
        )}
      </div>

      <footer className="flex items-center justify-between border-t border-glass-border px-3 py-2 font-mono text-[9px] text-text-muted">
        <span>{t(`kind_${kind}`)}</span>
        <span>{t("connectHint")}</span>
      </footer>

      <Handle
        type="source"
        position={Position.Right}
        isConnectableEnd={false}
        className="!h-3 !w-3 !border-2 !border-background !bg-accent"
      />
    </article>
  );
}

function DeletableCanvasEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  markerEnd,
  style,
  selected,
}: EdgeProps) {
  const t = useTranslations("canvas");
  const actions = useContext(CanvasNodeActionsContext);
  const [edgePath, labelX, labelY] = getBezierPath({
    sourceX,
    sourceY,
    targetX,
    targetY,
    sourcePosition,
    targetPosition,
  });

  return (
    <>
      <BaseEdge
        id={id}
        path={edgePath}
        markerEnd={markerEnd}
        interactionWidth={28}
        style={{
          ...style,
          strokeWidth: selected ? 2.6 : style?.strokeWidth,
          opacity: selected ? 1 : style?.opacity,
        }}
      />
      {selected ? (
        <EdgeLabelRenderer>
          <button
            type="button"
            onClick={(event) => {
              event.stopPropagation();
              actions?.deleteEdge(id);
            }}
            className="nodrag nopan pointer-events-auto absolute inline-flex h-8 items-center gap-1.5 rounded-lg border border-status-failed-border bg-elevated px-2.5 text-[10px] font-medium text-status-failed-fg shadow-xl transition-colors hover:bg-status-failed-bg"
            style={{
              transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`,
            }}
            aria-label={t("deleteEdge")}
          >
            <Trash2 size={12} />
            {t("deleteEdge")}
          </button>
        </EdgeLabelRenderer>
      ) : null}
    </>
  );
}

const NODE_TYPES = { creative: CreativeCanvasNode };
const EDGE_TYPES = { deletable: DeletableCanvasEdge };

function FreeCanvasSurface({ workspaceId }: FreeCanvasPageProps) {
  const t = useTranslations("canvas");
  const storageKey = `lumenx-free-canvas:${workspaceId || "local"}`;
  const initialLayout = useMemo(() => readLayout(storageKey), [storageKey]);
  const initialNodes = useMemo(
    () => (initialLayout.customNodes || []).filter(isCustomNode),
    [initialLayout],
  );
  const [nodes, setNodes, onNodesChange] = useNodesState<StudioNode>(initialNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>(
    (initialLayout.customEdges || []).filter(isCustomEdge).map(asCanvasCurve),
  );
  const [isReady, setIsReady] = useState(false);
  const [batchRunning, setBatchRunning] = useState(false);
  const [batchProgress, setBatchProgress] = useState({ completed: 0, total: 0 });
  const hasFitted = useRef(false);
  const viewportRef = useRef<Viewport | undefined>(initialLayout.viewport);
  const nodesRef = useRef(nodes);
  const edgesRef = useRef(edges);
  const mountedRef = useRef(true);
  const activeTaskIdsRef = useRef(new Set<string>());
  const { fitView, screenToFlowPosition } = useReactFlow<StudioNode, Edge>();

  useEffect(() => {
    nodesRef.current = nodes;
  }, [nodes]);

  useEffect(() => {
    edgesRef.current = edges;
  }, [edges]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const persistCanvas = useCallback(
    (nextNodes: StudioNode[], nextEdges: Edge[], viewport = viewportRef.current) => {
      writeLayout(storageKey, makeLayoutSnapshot(nextNodes, nextEdges, viewport));
    },
    [storageKey],
  );

  const commitCanvas = useCallback(
    (nextNodes: StudioNode[], nextEdges: Edge[]) => {
      nodesRef.current = nextNodes;
      edgesRef.current = nextEdges;
      setNodes(nextNodes);
      setEdges(nextEdges);
      persistCanvas(nextNodes, nextEdges);
    },
    [persistCanvas, setEdges, setNodes],
  );

  useEffect(() => {
    if (!isReady || nodes.length === 0 || hasFitted.current || initialLayout.viewport) return;
    hasFitted.current = true;
    const frame = window.requestAnimationFrame(() => {
      void fitView({ padding: 0.18, duration: 500, maxZoom: 1 });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [fitView, initialLayout.viewport, isReady, nodes.length]);

  const addCreativeNode = useCallback(
    (kind: CreativeItemKind) => {
      const defaults: Record<CreativeItemKind, Partial<StudioNodeData> & { title: string }> = {
        note: { title: t("newNoteTitle"), content: "" },
        prompt: { title: t("newPromptTitle"), content: "" },
        composer: {
          title: t("newComposerTitle"),
          content: "",
          composeTarget: "image",
          composeInstruction: "",
          composeStatus: "idle",
        },
        image: { title: t("newImageTitle") },
        video: { title: t("newVideoTitle") },
        task: {
          title: t("newTaskTitle"),
          content: "",
          taskStatus: "pending",
          ...getDefaultTaskData("t2v"),
        },
      };
      const center = screenToFlowPosition({
        x: window.innerWidth / 2,
        y: window.innerHeight / 2,
      });
      const customNodeIndex = nodes.filter(isCustomNode).length;
      const column = customNodeIndex % 3;
      const row = Math.floor(customNodeIndex / 3);
      const newNode: StudioNode = {
        id: makeId(`custom:${kind}`),
        type: "creative",
        position: {
          x: center.x - CREATIVE_NODE_WIDTH / 2 + (column - 1) * (CREATIVE_NODE_WIDTH + 36),
          y: center.y - 120 + row * 320,
        },
        data: { kind, isCustom: true, ...defaults[kind] },
      };
      const nextNodes = [...nodes.map((node) => ({ ...node, selected: false })), { ...newNode, selected: true }];
      setNodes(nextNodes);
      persistCanvas(nextNodes, edges);
    },
    [edges, nodes, persistCanvas, screenToFlowPosition, setNodes, t],
  );

  const updateNodeData = useCallback(
    (id: string, patch: Partial<StudioNodeData>) => {
      const sourceNode = nodesRef.current.find((node) => node.id === id);
      const shouldInvalidateComposers = (
        Object.prototype.hasOwnProperty.call(patch, "content")
        && (sourceNode?.data.kind === "note" || sourceNode?.data.kind === "prompt")
      );
      const downstreamComposerIds = new Set<string>();
      if (shouldInvalidateComposers) {
        const queue = [id];
        const visited = new Set<string>();
        while (queue.length > 0) {
          const sourceId = queue.shift()!;
          if (visited.has(sourceId)) continue;
          visited.add(sourceId);
          for (const edge of edgesRef.current) {
            if (edge.source !== sourceId || visited.has(edge.target)) continue;
            const downstream = nodesRef.current.find((node) => node.id === edge.target);
            if (downstream?.data.kind === "composer") downstreamComposerIds.add(edge.target);
            queue.push(edge.target);
          }
        }
      }
      const nextNodes = nodesRef.current.map((node) => {
        if (node.id === id) return { ...node, data: { ...node.data, ...patch } };
        if (downstreamComposerIds.has(node.id)) {
          return {
            ...node,
            data: {
              ...node.data,
              composeStatus: "idle" as ComposeStatus,
              composeError: undefined,
            },
          };
        }
        return node;
      });
      commitCanvas(nextNodes, edgesRef.current);
    },
    [commitCanvas],
  );

  const composePrompt = useCallback(
    async (composerId: string) => {
      const composerNode = nodesRef.current.find(
        (node) => node.id === composerId && node.data.kind === "composer",
      );
      if (!composerNode || composerNode.data.composeStatus === "running") return false;

      const inputs = collectUpstreamTextInputs(
        nodesRef.current,
        edgesRef.current,
        composerId,
      );
      if (inputs.length === 0) {
        updateNodeData(composerId, {
          composeStatus: "failed",
          composeError: t("composeMissingInputs"),
          composedInputNodeIds: [],
        });
        return false;
      }

      updateNodeData(composerId, {
        composeStatus: "running",
        composeError: undefined,
        composedInputNodeIds: inputs.map((input) => input.nodeId),
      });

      try {
        const result = await canvasApi.composePrompt({
          inputs: inputs.map((input) => ({
            node_id: input.nodeId,
            title: input.nodeTitle,
            content: input.content,
          })),
          target: composerNode.data.composeTarget || "image",
          instruction: composerNode.data.composeInstruction || "",
        });
        if (!mountedRef.current) return false;
        updateNodeData(composerId, {
          content: result.prompt_cn,
          contentEn: result.prompt_en,
          composeStatus: "done",
          composeError: undefined,
          composedInputNodeIds: inputs.map((input) => input.nodeId),
        });
        return true;
      } catch (error) {
        if (!mountedRef.current) return false;
        updateNodeData(composerId, {
          composeStatus: "failed",
          composeError: getSafeApiError(error).message || t("composeFailed"),
        });
        return false;
      }
    },
    [t, updateNodeData],
  );

  const markTaskFailed = useCallback(
    (taskId: string, message: string, generationId?: string) => {
      updateNodeData(taskId, {
        taskStatus: "failed",
        taskError: message,
        ...(generationId ? { generationId } : {}),
      });
    },
    [updateNodeData],
  );

  const completeTask = useCallback(
    (taskId: string, generation: PlaygroundGenerationResponse) => {
      const taskNode = nodesRef.current.find((node) => node.id === taskId);
      if (!taskNode) return false;

      const taskMode = normalizeTaskMode(taskNode.data.taskMode);
      const outputKind = taskMode === "t2i" ? "image" : "video";
      const resolvedOutput = resolveCanvasOutput(generation, outputKind);
      if (!resolvedOutput) {
        markTaskFailed(taskId, t("taskGenerationFailed"), generation.id);
        return false;
      }

      const existingOutput = nodesRef.current.find(
        (node) => node.data.kind === outputKind && node.data.sourceTaskId === taskId,
      );
      const outputNodeId = existingOutput?.id || makeId(`custom:${outputKind}`);
      const outputTitle = t(
        outputKind === "image" ? "generatedImageTitle" : "generatedVideoTitle",
        { title: taskNode.data.title },
      );
      const outputNode: StudioNode = existingOutput
        ? {
            ...existingOutput,
            selected: true,
            data: {
              ...existingOutput.data,
              title: outputTitle,
              mediaUrl: resolvedOutput.mediaUrl,
              mediaReference: resolvedOutput.mediaReference,
              generationId: generation.id,
              sourceTaskId: taskId,
            },
          }
        : {
            id: outputNodeId,
            type: "creative",
            position: {
              x: taskNode.position.x + CREATIVE_NODE_WIDTH + 96,
              y: taskNode.position.y,
            },
            selected: true,
            data: {
              kind: outputKind,
              isCustom: true,
              title: outputTitle,
              mediaUrl: resolvedOutput.mediaUrl,
              mediaReference: resolvedOutput.mediaReference,
              generationId: generation.id,
              sourceTaskId: taskId,
            },
          };

      const nextNodes: StudioNode[] = [
        ...nodesRef.current
          .filter((node) => node.id !== outputNodeId)
          .map<StudioNode>((node) => node.id === taskId
            ? {
                ...node,
                selected: false,
                data: {
                  ...node.data,
                  taskStatus: "done",
                  taskError: undefined,
                  generationId: generation.id,
                },
              }
            : { ...node, selected: false }),
        outputNode,
      ];

      const hasResultEdge = edgesRef.current.some(
        (edge) => edge.source === taskId && edge.target === outputNodeId,
      );
      const nextEdges = hasResultEdge
        ? edgesRef.current
        : addEdge(
            asCanvasCurve({
              id: makeId("custom-edge"),
              source: taskId,
              target: outputNodeId,
            }),
            edgesRef.current,
          );

      commitCanvas(nextNodes, nextEdges);
      window.requestAnimationFrame(() => {
        void fitView({ padding: 0.18, duration: 500, maxZoom: 1 });
      });
      return true;
    },
    [commitCanvas, fitView, markTaskFailed, t],
  );

  const monitorGeneration = useCallback(
    async (taskId: string, generationId: string) => {
      let consecutiveErrors = 0;

      for (let attempt = 0; attempt < TASK_POLL_LIMIT && mountedRef.current; attempt += 1) {
        try {
          const status = await playgroundApi.getGenerationStatus(generationId);
          consecutiveErrors = 0;

          if (status.provider_task_id) {
            updateNodeData(taskId, { providerTaskId: status.provider_task_id });
          }

          if (status.status === "completed") {
            const generation = await playgroundApi.getGeneration(generationId);
            return mountedRef.current ? completeTask(taskId, generation) : false;
          }

          if (status.status === "failed") {
            if (mountedRef.current) {
              markTaskFailed(
                taskId,
                status.error || t("taskGenerationFailed"),
                generationId,
              );
            }
            return false;
          }
        } catch (error) {
          consecutiveErrors += 1;
          if (consecutiveErrors >= 12) {
            if (mountedRef.current) {
              markTaskFailed(
                taskId,
                getSafeApiError(error).message || t("taskPollingFailed"),
                generationId,
              );
            }
            return false;
          }
        }

        await waitForNextPoll();
      }

      if (mountedRef.current) {
        markTaskFailed(taskId, t("taskTimeout"), generationId);
      }
      return false;
    },
    [completeTask, markTaskFailed, t, updateNodeData],
  );

  const executeTask = useCallback(
    async (taskId: string): Promise<boolean> => {
      if (activeTaskIdsRef.current.has(taskId)) return false;

      const taskNode = nodesRef.current.find(
        (node) => node.id === taskId && node.data.kind === "task",
      );
      if (!taskNode) return false;

      const taskMode = normalizeTaskMode(taskNode.data.taskMode);
      const directComposerIds = edgesRef.current
        .filter((edge) => edge.target === taskId)
        .map((edge) => edge.source)
        .filter((nodeId) => nodesRef.current.some(
          (node) => node.id === nodeId && node.data.kind === "composer",
        ));
      for (const composerId of directComposerIds) {
        const composer = nodesRef.current.find((node) => node.id === composerId);
        if (composer?.data.composeStatus !== "done" || !composer.data.content?.trim()) {
          const composed = await composePrompt(composerId);
          if (!composed) {
            markTaskFailed(taskId, t("taskUpstreamComposeFailed"));
            return false;
          }
        }
      }
      const upstreamPrompt = resolveUpstreamPrompt(
        nodesRef.current,
        edgesRef.current,
        taskId,
      );
      if (!upstreamPrompt) {
        markTaskFailed(taskId, t("taskMissingPrompt"));
        return false;
      }

      const upstreamMedia = taskMode === "i2v"
        ? resolveUpstreamMedia(nodesRef.current, edgesRef.current, taskId, "image")
        : null;
      if (taskMode === "i2v" && !upstreamMedia) {
        markTaskFailed(taskId, t("taskMissingImage"));
        return false;
      }

      activeTaskIdsRef.current.add(taskId);
        updateNodeData(taskId, {
          taskStatus: "running",
          taskError: undefined,
          generationId: undefined,
          providerTaskId: undefined,
        sourcePromptNodeId: upstreamPrompt.nodeId,
        sourceMediaNodeId: upstreamMedia?.nodeId,
      });

      try {
        const isImageTask = taskMode === "t2i";
        const generation = await playgroundApi.generate({
          mode: taskMode,
          model_id: IS_CLOUD_DEPLOYMENT
            ? undefined
            : taskNode.data.modelId || DEFAULT_CANVAS_MODELS[taskMode],
          prompt: upstreamPrompt.prompt,
          input_media: upstreamMedia
            ? [resolveCanvasMediaInput(upstreamMedia)]
            : undefined,
          parameters: isImageTask
            ? {
                size: taskNode.data.imageSize || "1280*1280",
                watermark: false,
                prompt_extend: true,
              }
            : {
                duration: taskNode.data.duration || 5,
                resolution: taskNode.data.resolution || "720p",
                aspect_ratio: taskNode.data.aspectRatio || "16:9",
                watermark: false,
                generate_audio: false,
              },
          batch_size: 1,
        });

        if (!mountedRef.current) return false;
        updateNodeData(taskId, {
          generationId: generation.id,
          providerTaskId: generation.provider_task_id || undefined,
        });

        if (generation.status === "completed") {
          return completeTask(taskId, generation);
        }
        if (generation.status === "failed") {
          markTaskFailed(
            taskId,
            generation.error || t("taskGenerationFailed"),
            generation.id,
          );
          return false;
        }
        return await monitorGeneration(taskId, generation.id);
      } catch (error) {
        if (mountedRef.current) {
          markTaskFailed(
            taskId,
            getSafeApiError(error).message || t("taskSubmitFailed"),
          );
        }
        return false;
      } finally {
        activeTaskIdsRef.current.delete(taskId);
      }
    },
    [completeTask, composePrompt, markTaskFailed, monitorGeneration, t, updateNodeData],
  );

  const runTask = useCallback(
    (taskId: string) => {
      void executeTask(taskId);
    },
    [executeTask],
  );

  useEffect(() => {
    if (!isReady) return;

    for (const node of nodesRef.current) {
      if (
        node.data.kind !== "task"
        || node.data.taskStatus !== "running"
        || !node.data.generationId
        || activeTaskIdsRef.current.has(node.id)
      ) {
        continue;
      }

      activeTaskIdsRef.current.add(node.id);
      void monitorGeneration(node.id, node.data.generationId).finally(() => {
        activeTaskIdsRef.current.delete(node.id);
      });
    }
  }, [isReady, monitorGeneration]);

  const runAllTasks = useCallback(() => {
    if (batchRunning) return;

    const plan = buildBatchTaskPlan(nodesRef.current, edgesRef.current);
    const taskIds = plan.layers.flat().concat(plan.cyclicTaskIds);
    if (taskIds.length === 0) return;

    setBatchRunning(true);
    setBatchProgress({ completed: 0, total: taskIds.length });

    void (async () => {
      const failedTaskIds = new Set<string>();
      let completedCount = 0;

      for (const taskId of plan.cyclicTaskIds) {
        failedTaskIds.add(taskId);
        markTaskFailed(taskId, t("taskDependencyCycle"));
        completedCount += 1;
      }
      setBatchProgress({ completed: completedCount, total: taskIds.length });

      for (const layer of plan.layers) {
        const runnable: string[] = [];
        for (const taskId of layer) {
          const dependencies = resolveTaskDependencies(
            nodesRef.current,
            edgesRef.current,
            taskId,
          );
          if (dependencies.some((dependencyId) => failedTaskIds.has(dependencyId))) {
            failedTaskIds.add(taskId);
            markTaskFailed(taskId, t("taskUpstreamFailed"));
            completedCount += 1;
          } else {
            runnable.push(taskId);
          }
        }

        const results = await Promise.all(runnable.map((taskId) => executeTask(taskId)));
        results.forEach((succeeded, index) => {
          if (!succeeded) failedTaskIds.add(runnable[index]);
        });
        completedCount += runnable.length;
        setBatchProgress({ completed: completedCount, total: taskIds.length });
      }
    })().finally(() => {
      if (mountedRef.current) setBatchRunning(false);
    });
  }, [batchRunning, executeTask, markTaskFailed, t]);

  const duplicateNode = useCallback(
    (id: string) => {
      const source = nodes.find((node) => node.id === id && isCustomNode(node));
      if (!source) return;
      const duplicate: StudioNode = {
        id: makeId(`custom:${source.data.kind}`),
        type: "creative",
        position: { x: source.position.x + 36, y: source.position.y + 36 },
        data: { ...source.data, title: `${source.data.title} ${t("copySuffix")}` },
        selected: true,
      };
      const nextNodes = [...nodes.map((node) => ({ ...node, selected: false })), duplicate];
      setNodes(nextNodes);
      persistCanvas(nextNodes, edges);
    },
    [edges, nodes, persistCanvas, setNodes, t],
  );

  const deleteNodesById = useCallback(
    (ids: Set<string>) => {
      const customIds = new Set(
        nodes.filter((node) => ids.has(node.id) && isCustomNode(node)).map((node) => node.id),
      );
      if (customIds.size === 0) return;
      const nextNodes = nodes.filter((node) => !customIds.has(node.id));
      const nextEdges = edges.filter(
        (edge) => !customIds.has(edge.source) && !customIds.has(edge.target),
      );
      setNodes(nextNodes);
      setEdges(nextEdges);
      persistCanvas(nextNodes, nextEdges);
    },
    [edges, nodes, persistCanvas, setEdges, setNodes],
  );

  const deleteNode = useCallback(
    (id: string) => deleteNodesById(new Set([id])),
    [deleteNodesById],
  );

  const deleteEdge = useCallback(
    (id: string) => {
      const nextEdges = edgesRef.current.filter((edge) => edge.id !== id);
      commitCanvas(nodesRef.current, nextEdges);
    },
    [commitCanvas],
  );

  const selectedCustomNodes = nodes.filter((node) => node.selected && isCustomNode(node));
  const selectedCustomEdges = edges.filter((edge) => edge.selected && isCustomEdge(edge));
  const selectedEditableCount = selectedCustomNodes.length + selectedCustomEdges.length;
  const executableTaskCount = nodes.filter((node) => (
    node.data.kind === "task"
    && node.data.taskStatus !== "done"
    && node.data.taskStatus !== "running"
  )).length;

  const duplicateSelected = () => {
    if (selectedCustomNodes.length === 1) duplicateNode(selectedCustomNodes[0].id);
  };

  const deleteSelected = () => {
    const selectedNodeIds = new Set(selectedCustomNodes.map((node) => node.id));
    const nextNodes = nodes.filter((node) => !selectedNodeIds.has(node.id));
    const nextEdges = edges.filter(
      (edge) =>
        !selectedNodeIds.has(edge.source) &&
        !selectedNodeIds.has(edge.target) &&
        !(edge.selected && isCustomEdge(edge)),
    );
    setNodes(nextNodes);
    setEdges(nextEdges);
    persistCanvas(nextNodes, nextEdges);
  };

  const handleConnect = useCallback(
    (connection: Connection) => {
      if (!connection.source || !connection.target || connection.source === connection.target) return;
      const nextEdges = addEdge(
        asCanvasCurve({
          ...connection,
          id: makeId("custom-edge"),
        }),
        edges,
      );
      const nextNodes = nodes.map((node) => (
        node.id === connection.target && node.data.kind === "composer"
          ? {
              ...node,
              data: {
                ...node.data,
                composeStatus: "idle" as ComposeStatus,
                composeError: undefined,
              },
            }
          : node
      ));
      setNodes(nextNodes);
      setEdges(nextEdges);
      persistCanvas(nextNodes, nextEdges);
    },
    [edges, nodes, persistCanvas, setEdges, setNodes],
  );

  const handleMoveEnd = useCallback<OnMoveEnd>(
    (_event, viewport) => {
      viewportRef.current = viewport;
      persistCanvas(nodes, edges, viewport);
    },
    [edges, nodes, persistCanvas],
  );

  const resetLayout = () => {
    viewportRef.current = undefined;
    const nextNodes = nodes.map((node, index) => ({
      ...node,
      position: {
        x: 80 + (index % 3) * (CREATIVE_NODE_WIDTH + 60),
        y: 80 + Math.floor(index / 3) * 320,
      },
      selected: false,
    }));
    setNodes(nextNodes);
    persistCanvas(nextNodes, edges, undefined);
    window.requestAnimationFrame(() => {
      void fitView({ padding: 0.18, duration: 450, maxZoom: 1 });
    });
  };

  const nodeActions = useMemo<CanvasNodeActions>(
    () => ({ updateNodeData, duplicateNode, deleteNode, deleteEdge, runTask, composePrompt }),
    [composePrompt, deleteEdge, deleteNode, duplicateNode, runTask, updateNodeData],
  );

  const paletteItems: Array<{ kind: CreativeItemKind; label: string; icon: typeof Type }> = [
    { kind: "note", label: t("addNote"), icon: MessageSquareText },
    { kind: "prompt", label: t("addPrompt"), icon: WandSparkles },
    { kind: "composer", label: t("addComposer"), icon: Sparkles },
    { kind: "image", label: t("addImage"), icon: ImageIcon },
    { kind: "video", label: t("addVideo"), icon: Video },
    { kind: "task", label: t("addTask"), icon: ListTodo },
  ];

  return (
    <section className="lumenx-free-canvas flex h-full min-h-[640px] flex-col overflow-hidden bg-background">
      <div className="relative z-20 flex min-h-[52px] items-center gap-2 overflow-x-auto border-b border-glass-border bg-surface/60 px-4 py-2 backdrop-blur-xl lg:px-7">
        <span className="mr-1 flex-none font-mono text-[10px] uppercase tracking-[0.16em] text-text-muted">
          {t("nodeShelf")}
        </span>
        {paletteItems.map(({ kind, label, icon: Icon }) => (
          <button
            key={kind}
            type="button"
            onClick={() => addCreativeNode(kind)}
            className="inline-flex h-8 flex-none items-center gap-1.5 rounded-lg border border-glass-border bg-background/45 px-2.5 text-[11px] text-text-secondary transition-colors hover:border-accent/35 hover:bg-accent/10 hover:text-foreground"
          >
            <Icon size={13} />
            {label}
          </button>
        ))}
        <div className="mx-1 h-5 w-px flex-none bg-glass-border" />
        <button
          type="button"
          onClick={duplicateSelected}
          disabled={selectedCustomNodes.length !== 1}
          className="inline-flex h-8 flex-none items-center gap-1.5 rounded-lg px-2.5 text-[11px] text-text-muted transition-colors hover:bg-hover-bg hover:text-foreground disabled:pointer-events-none disabled:opacity-30"
        >
          <Copy size={13} />
          {t("duplicateNode")}
        </button>
        <button
          type="button"
          onClick={deleteSelected}
          disabled={selectedEditableCount === 0}
          className="inline-flex h-8 flex-none items-center gap-1.5 rounded-lg px-2.5 text-[11px] text-text-muted transition-colors hover:bg-status-failed-bg hover:text-status-failed-fg disabled:pointer-events-none disabled:opacity-30"
        >
          <Trash2 size={13} />
          {t("deleteNode")}
        </button>
        <div className="mx-1 h-5 w-px flex-none bg-glass-border" />
        <button
          type="button"
          onClick={runAllTasks}
          disabled={batchRunning || executableTaskCount === 0}
          className="inline-flex h-8 flex-none items-center gap-1.5 rounded-lg border border-accent/35 bg-accent px-3 text-[11px] font-semibold text-on-accent shadow-[0_10px_24px_-16px_var(--color-accent)] transition-all hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-45"
          aria-label={batchRunning ? t("batchRunning") : t("batchRun")}
        >
          {batchRunning ? <LoaderCircle size={13} className="animate-spin" /> : <Layers3 size={13} />}
          {batchRunning
            ? t("batchProgress", batchProgress)
            : t("batchRun")}
        </button>
      </div>

      <div className="relative min-h-0 flex-1">
        <div className="absolute right-4 top-3 z-20 flex items-center gap-2">
          <div className="hidden items-center gap-1.5 rounded-xl border border-glass-border bg-elevated/90 px-3 py-2 text-[10px] text-text-muted shadow-lg backdrop-blur-xl sm:flex">
            <Sparkles size={12} className="text-accent" />
            <span>{t("itemCount", { count: nodes.length })}</span>
          </div>
          <button
            type="button"
            onClick={() => void fitView({ padding: 0.18, duration: 400, maxZoom: 1 })}
            className="inline-flex h-9 items-center gap-1.5 rounded-xl border border-glass-border bg-elevated/90 px-3 text-[11px] text-text-secondary shadow-lg backdrop-blur-xl transition-colors hover:bg-hover-bg hover:text-foreground"
          >
            <Maximize2 size={13} />
            <span className="hidden lg:inline">{t("fitView")}</span>
          </button>
          <button
            type="button"
            onClick={resetLayout}
            className="inline-flex h-9 items-center gap-1.5 rounded-xl border border-glass-border bg-elevated/90 px-3 text-[11px] text-text-secondary shadow-lg backdrop-blur-xl transition-colors hover:bg-hover-bg hover:text-foreground"
          >
            <RotateCcw size={13} />
            <span className="hidden lg:inline">{t("resetLayout")}</span>
          </button>
        </div>

        {nodes.length === 0 ? (
          <div className="pointer-events-none absolute inset-0 z-10 grid place-items-center p-6">
            <div className="max-w-sm rounded-2xl border border-glass-border bg-elevated/90 p-6 text-center shadow-2xl backdrop-blur-xl">
              <div className="mx-auto grid h-12 w-12 place-items-center rounded-2xl border border-accent/25 bg-accent/10 text-accent">
                <LayoutDashboard size={21} />
              </div>
              <h2 className="mt-4 text-sm font-semibold text-foreground">{t("emptyTitle")}</h2>
              <p className="mt-2 text-xs leading-5 text-text-secondary">{t("emptyBody")}</p>
            </div>
          </div>
        ) : null}

        <CanvasNodeActionsContext.Provider value={nodeActions}>
          <ReactFlow<StudioNode, Edge>
            nodes={nodes}
            edges={edges}
            nodeTypes={NODE_TYPES}
            edgeTypes={EDGE_TYPES}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={handleConnect}
            onNodeDragStop={(_event, draggedNode) => {
              const nextNodes = nodes.map((node) =>
                node.id === draggedNode.id ? { ...node, position: draggedNode.position } : node,
              );
              persistCanvas(nextNodes, edges);
            }}
            onMoveEnd={handleMoveEnd}
            onInit={() => setIsReady(true)}
            defaultViewport={initialLayout.viewport || { x: 0, y: 0, zoom: 0.85 }}
            minZoom={0.16}
            maxZoom={1.8}
            panOnScroll
            selectionOnDrag
            panOnDrag={[1, 2]}
            nodesConnectable
            connectOnClick
            deleteKeyCode={null}
            snapToGrid
            snapGrid={[16, 16]}
            defaultEdgeOptions={{
              type: "deletable",
              animated: false,
              style: {
                stroke: "var(--color-accent)",
                strokeWidth: 1.6,
                strokeLinecap: "round",
                opacity: 0.78,
              },
              markerEnd: {
                type: MarkerType.ArrowClosed,
                width: 14,
                height: 14,
                color: "var(--color-accent)",
              },
            }}
            proOptions={{ hideAttribution: false }}
            className="bg-background"
          >
            <Background
              variant={BackgroundVariant.Dots}
              gap={22}
              size={1.2}
              color="var(--free-canvas-dot)"
            />
            <Controls
              position="bottom-left"
              showInteractive={false}
              className="free-canvas-controls !m-4 !overflow-hidden !rounded-xl !border !border-glass-border !bg-elevated/90 !shadow-xl"
            />
            <MiniMap
              position="bottom-right"
              pannable
              zoomable
              nodeStrokeWidth={2}
              nodeColor={(node) => {
                if (node.data.isCustom) return "var(--color-primary)";
                if (node.data.kind === "series") return "var(--color-accent)";
                if (node.data.status === "completed") return "var(--color-status-completed-fg)";
                if (node.data.status === "processing") return "var(--color-status-processing-fg)";
                return "var(--color-text-muted)";
              }}
              maskColor="var(--free-canvas-minimap-mask)"
              className="!m-4 !overflow-hidden !rounded-xl !border !border-glass-border !bg-elevated/90 !shadow-xl"
            />
          </ReactFlow>
        </CanvasNodeActionsContext.Provider>
      </div>
    </section>
  );
}

export default function FreeCanvasPage(props: FreeCanvasPageProps) {
  return (
    <ReactFlowProvider>
      <FreeCanvasSurface {...props} />
    </ReactFlowProvider>
  );
}
