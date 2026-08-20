export interface WorkflowNodeLike {
  id: string;
  data: {
    kind?: unknown;
    title?: unknown;
    content?: unknown;
    mediaUrl?: unknown;
    mediaReference?: unknown;
    sourceTaskId?: unknown;
    taskStatus?: unknown;
  };
}

export interface WorkflowEdgeLike {
  source: string;
  target: string;
}

export interface ResolvedWorkflowPrompt {
  nodeId: string;
  nodeTitle: string;
  prompt: string;
}

export interface UpstreamTextInput {
  nodeId: string;
  nodeTitle: string;
  content: string;
}

export interface ResolvedWorkflowMedia {
  nodeId: string;
  nodeTitle: string;
  mediaUrl: string;
  mediaReference?: string;
}

export interface BatchTaskPlan {
  layers: string[][];
  cyclicTaskIds: string[];
}

function buildIncomingIndex(edges: WorkflowEdgeLike[]): Map<string, string[]> {
  const incomingByTarget = new Map<string, string[]>();
  for (const edge of edges) {
    const sources = incomingByTarget.get(edge.target) || [];
    sources.push(edge.source);
    incomingByTarget.set(edge.target, sources);
  }
  return incomingByTarget;
}

export function resolveUpstreamPrompt(
  nodes: WorkflowNodeLike[],
  edges: WorkflowEdgeLike[],
  taskNodeId: string,
): ResolvedWorkflowPrompt | null {
  const nodesById = new Map(nodes.map((node) => [node.id, node]));
  const incomingByTarget = buildIncomingIndex(edges);

  const queue = [...(incomingByTarget.get(taskNodeId) || [])];
  const visited = new Set<string>([taskNodeId]);
  let fallback: ResolvedWorkflowPrompt | null = null;

  while (queue.length > 0) {
    const nodeId = queue.shift()!;
    if (visited.has(nodeId)) continue;
    visited.add(nodeId);

    const node = nodesById.get(nodeId);
    if (!node) continue;

    const content = typeof node.data.content === "string" ? node.data.content.trim() : "";
    const title = typeof node.data.title === "string" ? node.data.title : "";
    const kind = node.data.kind;
    const resolved = content
      ? { nodeId, nodeTitle: title, prompt: content }
      : null;

    if (kind === "composer" && resolved) return resolved;
    if (kind === "prompt" && resolved && !fallback) fallback = resolved;
    if (kind === "note" && resolved && !fallback) fallback = resolved;

    queue.push(...(incomingByTarget.get(nodeId) || []));
  }

  return fallback;
}

export function collectUpstreamTextInputs(
  nodes: WorkflowNodeLike[],
  edges: WorkflowEdgeLike[],
  composerNodeId: string,
): UpstreamTextInput[] {
  const nodesById = new Map(nodes.map((node) => [node.id, node]));
  const incomingByTarget = buildIncomingIndex(edges);
  const queue = [...(incomingByTarget.get(composerNodeId) || [])];
  const visited = new Set<string>([composerNodeId]);
  const inputs: UpstreamTextInput[] = [];

  while (queue.length > 0) {
    const nodeId = queue.shift()!;
    if (visited.has(nodeId)) continue;
    visited.add(nodeId);

    const node = nodesById.get(nodeId);
    if (!node) continue;

    const kind = node.data.kind;
    const content = typeof node.data.content === "string" ? node.data.content.trim() : "";
    if ((kind === "note" || kind === "prompt" || kind === "composer") && content) {
      inputs.push({
        nodeId,
        nodeTitle: typeof node.data.title === "string" ? node.data.title : "",
        content,
      });
    }

    queue.push(...(incomingByTarget.get(nodeId) || []));
  }

  return inputs;
}

export function resolveUpstreamMedia(
  nodes: WorkflowNodeLike[],
  edges: WorkflowEdgeLike[],
  taskNodeId: string,
  mediaKind: "image" | "video",
): ResolvedWorkflowMedia | null {
  const nodesById = new Map(nodes.map((node) => [node.id, node]));
  const incomingByTarget = buildIncomingIndex(edges);
  const queue = [...(incomingByTarget.get(taskNodeId) || [])];
  const visited = new Set<string>([taskNodeId]);

  while (queue.length > 0) {
    const nodeId = queue.shift()!;
    if (visited.has(nodeId)) continue;
    visited.add(nodeId);

    const node = nodesById.get(nodeId);
    if (!node) continue;

    const kind = node.data.kind;
    const mediaUrl = typeof node.data.mediaUrl === "string"
      ? node.data.mediaUrl.trim()
      : "";
    if (kind === mediaKind && mediaUrl) {
      return {
        nodeId,
        nodeTitle: typeof node.data.title === "string" ? node.data.title : "",
        mediaUrl,
        mediaReference: typeof node.data.mediaReference === "string"
          ? node.data.mediaReference
          : undefined,
      };
    }

    if (kind === "task") {
      const generatedOutput = nodes.find((candidate) => (
        candidate.data.kind === mediaKind
        && candidate.data.sourceTaskId === nodeId
        && typeof candidate.data.mediaUrl === "string"
        && candidate.data.mediaUrl.trim().length > 0
      ));
      if (generatedOutput) {
        return {
          nodeId: generatedOutput.id,
          nodeTitle: typeof generatedOutput.data.title === "string"
            ? generatedOutput.data.title
            : "",
          mediaUrl: generatedOutput.data.mediaUrl as string,
          mediaReference: typeof generatedOutput.data.mediaReference === "string"
            ? generatedOutput.data.mediaReference
            : undefined,
        };
      }
    }

    queue.push(...(incomingByTarget.get(nodeId) || []));
  }

  return null;
}

export function resolveTaskDependencies(
  nodes: WorkflowNodeLike[],
  edges: WorkflowEdgeLike[],
  taskNodeId: string,
): string[] {
  const nodesById = new Map(nodes.map((node) => [node.id, node]));
  const incomingByTarget = buildIncomingIndex(edges);
  const queue = [...(incomingByTarget.get(taskNodeId) || [])];
  const visited = new Set<string>([taskNodeId]);
  const dependencies = new Set<string>();

  while (queue.length > 0) {
    const nodeId = queue.shift()!;
    if (visited.has(nodeId)) continue;
    visited.add(nodeId);

    const node = nodesById.get(nodeId);
    if (!node) continue;

    if (node.data.kind === "task") dependencies.add(nodeId);
    if (
      (node.data.kind === "image" || node.data.kind === "video")
      && typeof node.data.sourceTaskId === "string"
    ) {
      dependencies.add(node.data.sourceTaskId);
    }

    queue.push(...(incomingByTarget.get(nodeId) || []));
  }

  return Array.from(dependencies);
}

export function buildBatchTaskPlan(
  nodes: WorkflowNodeLike[],
  edges: WorkflowEdgeLike[],
): BatchTaskPlan {
  const taskIds = nodes
    .filter((node) => (
      node.data.kind === "task"
      && node.data.taskStatus !== "done"
      && node.data.taskStatus !== "running"
    ))
    .map((node) => node.id);
  const taskIdSet = new Set(taskIds);
  const remaining = new Set(taskIds);
  const completed = new Set<string>();
  const layers: string[][] = [];

  while (remaining.size > 0) {
    const ready = taskIds.filter((taskId) => (
      remaining.has(taskId)
      && resolveTaskDependencies(nodes, edges, taskId)
        .filter((dependencyId) => taskIdSet.has(dependencyId))
        .every((dependencyId) => completed.has(dependencyId))
    ));

    if (ready.length === 0) break;
    layers.push(ready);
    ready.forEach((taskId) => {
      remaining.delete(taskId);
      completed.add(taskId);
    });
  }

  return { layers, cyclicTaskIds: Array.from(remaining) };
}
