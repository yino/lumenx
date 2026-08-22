import { create } from 'zustand';
import { readClientStorage, writeClientStorage } from '@/lib/clientCacheScope';
import { resolveModelForMode } from './playgroundModels';

// ---------------------------------------------------------------------------
// Featured (best-of-batch) persistence — client-side localStorage only.
// Map of generationId -> the one outputId marked "featured" within that batch.
// ---------------------------------------------------------------------------

const FEATURED_LS_KEY = 'lumenx:playground:featured';

function loadFeatured(): Record<string, string> {
  if (typeof window === 'undefined') return {};
  try {
    return JSON.parse(readClientStorage(FEATURED_LS_KEY) || '{}') as Record<string, string>;
  } catch {
    return {};
  }
}

function saveFeatured(map: Record<string, string>): void {
  if (typeof window === 'undefined') return;
  try {
    writeClientStorage(FEATURED_LS_KEY, JSON.stringify(map));
  } catch {
    /* ignore quota / serialization errors */
  }
}

// ---------------------------------------------------------------------------
// Generation queue — client-side concurrency gate. Default concurrency is
// persisted to localStorage; queued ids use a simple module counter.
// ---------------------------------------------------------------------------

const CONCURRENCY_LS_KEY = 'lumenx:playground:concurrency';
const DEFAULT_CONCURRENCY = 3;

function loadConcurrency(): number {
  if (typeof window === 'undefined') return DEFAULT_CONCURRENCY;
  const raw = Number(readClientStorage(CONCURRENCY_LS_KEY));
  return Number.isFinite(raw) && raw >= 1 && raw <= 8 ? raw : DEFAULT_CONCURRENCY;
}

function saveConcurrency(n: number): void {
  if (typeof window === 'undefined') return;
  try {
    writeClientStorage(CONCURRENCY_LS_KEY, String(n));
  } catch {
    /* ignore */
  }
}

let queueSeq = 0;

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type PlaygroundMode = 't2i' | 'i2i' | 't2v' | 'i2v' | 'r2v' | 'v2v';

type PlaygroundMediaKind = 'image' | 'video';

const IMAGE_PATH_PATTERN = /\.(avif|bmp|gif|heic|heif|jpe?g|png|svg|tiff?|webp)$/i;
const VIDEO_PATH_PATTERN = /\.(avi|m4v|mkv|mov|mp4|mpeg|mpg|webm)$/i;

function mediaKindFromPath(path: string): PlaygroundMediaKind | null {
  const cleanPath = path.split(/[?#]/, 1)[0];
  if (/^data:image\//i.test(path) || IMAGE_PATH_PATTERN.test(cleanPath)) return 'image';
  if (/^data:video\//i.test(path) || VIDEO_PATH_PATTERN.test(cleanPath)) return 'video';
  return null;
}

function inferredInputKind(mode: PlaygroundMode): PlaygroundMediaKind | null {
  if (mode === 't2v') return null;
  return mode === 'v2v' ? 'video' : 'image';
}

function maxInputMediaForMode(mode: PlaygroundMode): number {
  if (mode === 't2v') return 0;
  return mode === 't2i' || mode === 'i2v' || mode === 'r2v' ? 9 : 1;
}

export function filterInputMediaForMode(
  inputMedia: string[],
  sourceMode: PlaygroundMode,
  targetMode: PlaygroundMode,
): string[] {
  const targetKind = inferredInputKind(targetMode);
  if (!targetKind) return [];
  const sourceKind = inferredInputKind(sourceMode);
  return inputMedia
    .filter((path) => (mediaKindFromPath(path) ?? sourceKind) === targetKind)
    .slice(0, maxInputMediaForMode(targetMode));
}

export interface PlaygroundOutput {
  id: string;
  media_reference: string;
  media_id?: string;
  media_url?: string;
  media_type: 'image' | 'video';
  thumbnail_path?: string;
  saved_to_library: boolean;
}

export interface PlaygroundGeneration {
  id: string;
  mode: PlaygroundMode;
  model_id?: string;
  actual_model_name?: string;
  actual_model_id?: string;
  prompt: string;
  negative_prompt?: string;
  input_media: string[];
  parameters: Record<string, any>;
  batch_size: number;
  outputs: PlaygroundOutput[];
  status: 'pending' | 'processing' | 'completed' | 'failed';
  raw_status?: string;
  status_zh?: string;
  cancellation_requested?: boolean;
  support_review?: boolean;
  support_review_reason?: string | null;
  error?: string;
  provider_name?: string | null;
  provider_task_id?: string | null;
  provider_request_id?: string | null;
  created_at: string;
  quoted_microtickets?: string;
  quoted_tickets?: string;
  tokens_per_ticket?: string;
}

export interface PlaygroundTemplate {
  id: string;
  name: string;
  category: string;
  prompt: string;
  negative_prompt?: string;
  default_mode?: PlaygroundMode;
  default_model_id?: string;
  default_parameters: Record<string, any>;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface QueuedRequest {
  id: string;
  mode: PlaygroundMode;
  modelId: string;
  prompt: string;
  negativePrompt?: string;
  inputMedia: string[];
  parameters: Record<string, any>;
  batchSize: number;
  status: 'pending' | 'dispatching';
  enqueuedAt: number;
}

// ---------------------------------------------------------------------------
// State & Actions
// ---------------------------------------------------------------------------

interface PlaygroundState {
  // Current input
  mode: PlaygroundMode;
  modelId: string;
  prompt: string;
  negativePrompt: string;
  inputMedia: string[];
  parameters: Record<string, any>;
  batchSize: number;

  // Model preferences (mode -> last used modelId)
  modelPreferences: Partial<Record<PlaygroundMode, string>>;

  // History
  history: PlaygroundGeneration[];
  /** Generation ids created in the current browser session only. */
  sessionGenerationIds: string[];

  // Templates
  templates: PlaygroundTemplate[];

  // UI
  isGenerating: boolean;
  activeGenerationIds: string[];
  showAdvancedParams: boolean;
  showTemplateModal: boolean;
  showHistoryDrawer: boolean;

  // Template favorites (local, not persisted to backend)
  favoriteTemplateIds: string[];
  toggleTemplateFavorite: (id: string) => void;
  isTemplateFavorited: (id: string) => boolean;

  // Featured output per generation (best-of-batch); one per batch, localStorage-persisted
  featuredByGen: Record<string, string>;
  toggleFeatured: (genId: string, outputId: string) => void;
  isFeatured: (genId: string, outputId: string) => boolean;

  // Generation queue (client-side concurrency gate)
  queue: QueuedRequest[];
  maxConcurrent: number;
  enqueueRequest: (req: Omit<QueuedRequest, 'id' | 'status' | 'enqueuedAt'>) => void;
  markDispatching: (id: string) => void;
  removeFromQueue: (id: string) => void;
  setMaxConcurrent: (n: number) => void;

  // Actions — input setters
  setMode: (mode: PlaygroundMode) => void;
  setModelId: (modelId: string) => void;
  setPrompt: (prompt: string) => void;
  setNegativePrompt: (neg: string) => void;
  setInputMedia: (media: string[]) => void;
  /** Push a generated result back into the compose panel as reference input,
   *  switching to the appropriate mode. Image → i2i (default) or i2v when an
   *  explicit targetMode is given; video → v2v. Respects per-mode model
   *  preference (same behavior as setMode). */
  useResultAsReference: (
    mediaPath: string,
    mediaType: 'image' | 'video',
    targetMode?: PlaygroundMode,
  ) => void;
  setParameters: (params: Record<string, any>) => void;
  setBatchSize: (size: number) => void;
  setShowAdvancedParams: (show: boolean) => void;
  setShowTemplateModal: (show: boolean) => void;
  setShowHistoryDrawer: (show: boolean) => void;

  // Actions — generation lifecycle
  startGeneration: (gen: PlaygroundGeneration) => void;
  updateGeneration: (gen: PlaygroundGeneration) => void;
  removeGeneration: (id: string) => void;

  // Actions — history
  setHistory: (history: PlaygroundGeneration[]) => void;
  appendToHistory: (gen: PlaygroundGeneration) => void;

  // Actions — templates
  setTemplates: (templates: PlaygroundTemplate[]) => void;
  addTemplate: (template: PlaygroundTemplate) => void;
  updateTemplate: (template: PlaygroundTemplate) => void;
  removeTemplate: (id: string) => void;
  applyTemplate: (template: PlaygroundTemplate) => void;

  // Actions — reset
  resetInput: () => void;
  resetForScope: () => void;
}

// ---------------------------------------------------------------------------
// Defaults
// ---------------------------------------------------------------------------

const DEFAULT_MODE: PlaygroundMode = 't2i';
const DEFAULT_MODEL_ID = resolveModelForMode(DEFAULT_MODE);
const DEFAULT_PROMPT = '';
const DEFAULT_BATCH_SIZE = 1;

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

export const usePlaygroundStore = create<PlaygroundState>((set, get) => ({
  // -- Current input --------------------------------------------------------
  mode: DEFAULT_MODE,
  modelId: DEFAULT_MODEL_ID,
  prompt: DEFAULT_PROMPT,
  negativePrompt: '',
  inputMedia: [],
  parameters: {},
  batchSize: DEFAULT_BATCH_SIZE,

  // -- Model preferences ----------------------------------------------------
  modelPreferences: {},

  // -- History ---------------------------------------------------------------
  history: [],
  sessionGenerationIds: [],

  // -- Templates -------------------------------------------------------------
  templates: [],

  // -- UI --------------------------------------------------------------------
  isGenerating: false,
  activeGenerationIds: [],
  showAdvancedParams: false,
  showTemplateModal: false,
  showHistoryDrawer: false,

  // -- Template favorites ----------------------------------------------------
  favoriteTemplateIds: [],
  toggleTemplateFavorite: (id) => {
    const { favoriteTemplateIds } = get();
    if (favoriteTemplateIds.includes(id)) {
      set({ favoriteTemplateIds: favoriteTemplateIds.filter((fid) => fid !== id) });
    } else {
      set({ favoriteTemplateIds: [...favoriteTemplateIds, id] });
    }
  },
  isTemplateFavorited: (id) => get().favoriteTemplateIds.includes(id),

  // -- Featured output (best-of-batch, one per generation) -------------------
  featuredByGen: loadFeatured(),
  toggleFeatured: (genId, outputId) => {
    const next = { ...get().featuredByGen };
    if (next[genId] === outputId) delete next[genId];
    else next[genId] = outputId;
    saveFeatured(next);
    set({ featuredByGen: next });
  },
  isFeatured: (genId, outputId) => get().featuredByGen[genId] === outputId,

  // -- Generation queue (client-side concurrency gate) -----------------------
  queue: [],
  maxConcurrent: loadConcurrency(),
  enqueueRequest: (req) =>
    set((s) => ({
      queue: [
        ...s.queue,
        { ...req, id: `q${++queueSeq}`, status: 'pending' as const, enqueuedAt: Date.now() },
      ],
      isGenerating: true,
    })),
  markDispatching: (id) =>
    set((s) => ({
      queue: s.queue.map((q) => (q.id === id ? { ...q, status: 'dispatching' as const } : q)),
    })),
  removeFromQueue: (id) =>
    set((s) => {
      const queue = s.queue.filter((q) => q.id !== id);
      return { queue, isGenerating: queue.length > 0 || s.activeGenerationIds.length > 0 };
    }),
  setMaxConcurrent: (n) => {
    const clamped = Math.max(1, Math.min(8, Math.round(n)));
    saveConcurrency(clamped);
    set({ maxConcurrent: clamped });
  },

  // =========================================================================
  // Actions
  // =========================================================================

  // -- Input setters ---------------------------------------------------------

  setMode: (mode) => {
    const { mode: currentMode, modelId, inputMedia, modelPreferences } = get();
    const nextModelId = resolveModelForMode(
      mode,
      mode === currentMode ? modelId : modelPreferences[mode],
    );
    if (mode === currentMode) {
      if (modelId !== nextModelId) set({ modelId: nextModelId });
      return;
    }
    set({
      mode,
      modelId: nextModelId,
      inputMedia: filterInputMediaForMode(inputMedia, currentMode, mode),
      parameters: {},
    });
  },

  setModelId: (modelId) => {
    const { mode, modelPreferences } = get();
    set({
      modelId,
      modelPreferences: { ...modelPreferences, [mode]: modelId },
    });
  },

  setPrompt: (prompt) => set({ prompt }),

  setNegativePrompt: (negativePrompt) => set({ negativePrompt }),

  setInputMedia: (inputMedia) => set({ inputMedia }),

  useResultAsReference: (mediaPath, mediaType, targetMode) => {
    const { modelPreferences } = get();
    const mode: PlaygroundMode =
      targetMode ?? (mediaType === 'video' ? 'v2v' : 'i2i');
    set({
      mode,
      modelId: resolveModelForMode(mode, modelPreferences[mode]),
      inputMedia: [mediaPath],
      parameters: {},
    });
  },

  setParameters: (parameters) => set({ parameters }),

  setBatchSize: (batchSize) => set({ batchSize }),

  setShowAdvancedParams: (showAdvancedParams) => set({ showAdvancedParams }),

  setShowTemplateModal: (showTemplateModal) =>
    set(showTemplateModal ? { showTemplateModal, showHistoryDrawer: false } : { showTemplateModal }),

  setShowHistoryDrawer: (showHistoryDrawer) =>
    set(showHistoryDrawer ? { showHistoryDrawer, showTemplateModal: false } : { showHistoryDrawer }),

  // -- Generation lifecycle --------------------------------------------------

  startGeneration: (gen) => {
    const { activeGenerationIds, history, sessionGenerationIds, queue } = get();
    const isTerminal = gen.status === 'completed' || gen.status === 'failed';
    set({
      activeGenerationIds: isTerminal
        ? activeGenerationIds
        : [...activeGenerationIds.filter((id) => id !== gen.id), gen.id],
      history: [gen, ...history.filter((item) => item.id !== gen.id)],
      sessionGenerationIds: sessionGenerationIds.includes(gen.id)
        ? sessionGenerationIds
        : [...sessionGenerationIds, gen.id],
      isGenerating: !isTerminal || queue.length > 0,
    });
  },

  updateGeneration: (gen) => {
    const { history, activeGenerationIds, sessionGenerationIds, queue } = get();
    const updatedHistory = history.some((h) => h.id === gen.id)
      ? history.map((h) => (h.id === gen.id ? gen : h))
      : [gen, ...history];
    const isTerminal = gen.status === 'completed' || gen.status === 'failed';
    const updatedActive = isTerminal
      ? activeGenerationIds.filter((id) => id !== gen.id)
      : activeGenerationIds;

    set({
      history: updatedHistory,
      activeGenerationIds: updatedActive,
      sessionGenerationIds: sessionGenerationIds.includes(gen.id)
        ? sessionGenerationIds
        : [...sessionGenerationIds, gen.id],
      isGenerating: updatedActive.length > 0 || queue.length > 0,
    });
  },

  removeGeneration: (id) => {
    const { history, activeGenerationIds, sessionGenerationIds, queue } = get();
    const updatedActive = activeGenerationIds.filter((gid) => gid !== id);
    set({
      history: history.filter((h) => h.id !== id),
      activeGenerationIds: updatedActive,
      sessionGenerationIds: sessionGenerationIds.filter((gid) => gid !== id),
      isGenerating: updatedActive.length > 0 || queue.length > 0,
    });
  },

  // -- History ---------------------------------------------------------------

  setHistory: (history) => {
    const current = get();
    // A history refresh can race with a just-submitted task. Keep local
    // session records until the server includes them, so the current page
    // never flickers back to an empty result state.
    const sessionRecords = current.history.filter((generation) =>
      current.sessionGenerationIds.includes(generation.id),
    );
    const remoteIds = new Set(history.map((generation) => generation.id));
    const localOnly = sessionRecords.filter((generation) => !remoteIds.has(generation.id));
    set({ history: [...localOnly, ...history] });
  },

  appendToHistory: (gen) =>
    set((s) => ({
      history: [gen, ...s.history.filter((item) => item.id !== gen.id)],
      sessionGenerationIds: s.sessionGenerationIds.includes(gen.id)
        ? s.sessionGenerationIds
        : [...s.sessionGenerationIds, gen.id],
    })),

  // -- Templates -------------------------------------------------------------

  setTemplates: (templates) => set({ templates }),

  addTemplate: (template) =>
    set((s) => ({ templates: [...s.templates, template] })),

  updateTemplate: (template) =>
    set((s) => ({
      templates: s.templates.map((t) => (t.id === template.id ? template : t)),
    })),

  removeTemplate: (id) =>
    set((s) => ({ templates: s.templates.filter((t) => t.id !== id) })),

  applyTemplate: (template) => {
    const current = get();
    const nextMode = template.default_mode ?? current.mode;
    const preferredModel = template.default_model_id ?? (
      nextMode === current.mode ? current.modelId : current.modelPreferences[nextMode]
    );
    const patch: Partial<PlaygroundState> = {
      prompt: template.prompt,
      parameters: template.default_parameters ?? {},
      modelId: resolveModelForMode(nextMode, preferredModel),
    };
    if (template.negative_prompt != null) {
      patch.negativePrompt = template.negative_prompt;
    }
    if (template.default_mode != null) {
      patch.mode = template.default_mode;
      if (template.default_mode !== current.mode) {
        patch.inputMedia = filterInputMediaForMode(
          current.inputMedia,
          current.mode,
          nextMode,
        );
      }
    }
    set(patch);
  },

  // -- Reset -----------------------------------------------------------------

  resetInput: () =>
    set({
      prompt: DEFAULT_PROMPT,
      negativePrompt: '',
      inputMedia: [],
      parameters: {},
      batchSize: DEFAULT_BATCH_SIZE,
    }),
  resetForScope: () =>
    set({
      mode: DEFAULT_MODE,
      modelId: DEFAULT_MODEL_ID,
      prompt: DEFAULT_PROMPT,
      negativePrompt: '',
      inputMedia: [],
      parameters: {},
      batchSize: DEFAULT_BATCH_SIZE,
      modelPreferences: {},
      history: [],
      sessionGenerationIds: [],
      templates: [],
      isGenerating: false,
      activeGenerationIds: [],
      showAdvancedParams: false,
      showTemplateModal: false,
      showHistoryDrawer: false,
      favoriteTemplateIds: [],
      featuredByGen: loadFeatured(),
      queue: [],
      maxConcurrent: loadConcurrency(),
    }),
}));
