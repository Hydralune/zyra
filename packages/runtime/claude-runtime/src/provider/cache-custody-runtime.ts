export type CacheTtl = "5m" | "1h";

export interface CacheControlMarker {
  type: "ephemeral";
  ttl?: CacheTtl;
}

export interface ProviderCachePolicyInput {
  sessionId: string;
  providerId: string;
  model: string;
  querySource: string;
  oneHourEnabled: boolean;
  oneHourModels?: readonly string[];
  oneHourSources?: readonly string[];
}

export interface ProviderCacheSample {
  sessionId: string;
  model: string;
  promptHash: string;
  cacheReadInputTokens: number;
  cacheCreationInputTokens?: number;
  observedAtMs: number;
  deletionObserved?: boolean;
  compactionObserved?: boolean;
}

export interface ProviderCacheBreak {
  broken: boolean;
  reason:
    | "none"
    | "excluded-model"
    | "initial-sample"
    | "model-change"
    | "prompt-change"
    | "observation-gap"
    | "cache-read-collapse";
  previousReadTokens: number;
  currentReadTokens: number;
  readRatio: number;
  suppressed: boolean;
}

export interface ToolMismatchEvidence {
  missingResultIds: string[];
  orphanResultIds: string[];
  duplicateUseIds: string[];
  duplicateResultIds: string[];
  toolUseIndexes: Record<string, number[]>;
  toolResultIndexes: Record<string, number[]>;
  preNormalizationCount: number;
  postNormalizationCount: number;
  valid: boolean;
}

export interface AnthropicClientDescriptor {
  family: "anthropic";
  transport: "anthropic" | "bedrock" | "vertex";
  endpoint: string;
  model: string;
  auth: "api-key" | "oauth" | "aws" | "gcp";
  credentialFingerprint: string;
  headers: Record<string, string>;
}

export interface ProviderRequestCustodyEffect {
  applied: boolean;
  ttl: CacheTtl;
  breakpointCount: number;
  mismatch: ToolMismatchEvidence;
  client: AnthropicClientDescriptor | null;
  cacheBreak: ProviderCacheBreak | null;
  diffPath: string | null;
}

type MutableRecord = Record<string, unknown>;

const recordOf = (value: unknown): MutableRecord | null =>
  value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as MutableRecord
    : null;

const stringOf = (value: unknown, fallback = ""): string =>
  typeof value === "string" ? value : fallback;

const numberOf = (value: unknown, fallback = 0): number =>
  typeof value === "number" && Number.isFinite(value) ? value : fallback;

const booleanOf = (value: unknown, fallback = false): boolean =>
  typeof value === "boolean" ? value : fallback;

const wildcardMatches = (value: string, pattern: string): boolean => {
  if (pattern === "*") return true;
  if (!pattern.includes("*")) return value === pattern;
  const escaped = pattern.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replaceAll("\\*", ".*");
  return new RegExp(`^${escaped}$`).test(value);
};

const stableFingerprint = (value: string): string => {
  let hash = 0x811c9dc5;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return (hash >>> 0).toString(16).padStart(8, "0");
};

const cloneValue = <T>(value: T): T => {
  if (Array.isArray(value)) return value.map((item) => cloneValue(item)) as T;
  const record = recordOf(value);
  if (!record) return value;
  return Object.fromEntries(
    Object.entries(record).map(([key, item]) => [key, cloneValue(item)]),
  ) as T;
};

const contentBlocks = (message: unknown): MutableRecord[] => {
  const record = recordOf(message);
  if (!record) return [];
  const content = record.content;
  if (Array.isArray(content)) return content.map(recordOf).filter((item): item is MutableRecord => item !== null);
  const block = recordOf(content);
  return block ? [block] : [];
};

const toolUseId = (block: MutableRecord): string => {
  const type = stringOf(block.type);
  if (type === "tool_use") return stringOf(block.id);
  if (type === "tool_result") return stringOf(block.tool_use_id ?? block.toolUseId);
  return "";
};

export class ProviderCacheCustodyRuntime {
  private readonly oneHourSessions = new Set<string>();
  private readonly samples = new Map<string, ProviderCacheSample>();

  should1hCacheTTL(input: ProviderCachePolicyInput): boolean {
    if (this.oneHourSessions.has(input.sessionId)) return true;
    if (!input.oneHourEnabled) return false;
    if (input.providerId !== "bedrock") return false;
    const models = input.oneHourModels ?? ["claude-*"];
    const sources = input.oneHourSources ?? ["*"];
    const modelEligible = models.some((pattern) => wildcardMatches(input.model, pattern));
    const sourceEligible = sources.some((pattern) => wildcardMatches(input.querySource, pattern));
    if (modelEligible && sourceEligible) this.oneHourSessions.add(input.sessionId);
    return modelEligible && sourceEligible;
  }

  addCacheBreakpoints<T>(
    messages: readonly T[],
    options: {
      ttl: CacheTtl;
      skipCacheWrite?: boolean;
      pinnedToolResultIds?: readonly string[];
    },
  ): T[] {
    const cloned = cloneValue(messages) as unknown[];
    const pinned = new Set(options.pinnedToolResultIds ?? []);
    let selected: MutableRecord | null = null;
    for (const message of cloned) {
      for (const block of contentBlocks(message)) {
        delete block.cacheControl;
        delete block.cache_control;
        if (stringOf(block.type) === "tool_result" && pinned.has(toolUseId(block))) {
          block.cacheReference = true;
        }
      }
    }
    if (!options.skipCacheWrite) {
      for (let messageIndex = cloned.length - 1; messageIndex >= 0 && selected === null; messageIndex -= 1) {
        const message = recordOf(cloned[messageIndex]);
        if (!message || stringOf(message.role) !== "user") continue;
        const blocks = contentBlocks(message);
        for (let blockIndex = blocks.length - 1; blockIndex >= 0; blockIndex -= 1) {
          const block = blocks[blockIndex]!;
          if (stringOf(block.type) === "cache_reference") continue;
          selected = block;
          break;
        }
      }
    }
    if (selected) selected.cacheControl = { type: "ephemeral", ttl: options.ttl } satisfies CacheControlMarker;
    return cloned as T[];
  }

  getCacheBreakDiffPath(root: string, sessionId: string, observedAtMs: number): string {
    const safeSession = sessionId.replace(/[^a-zA-Z0-9._-]/g, "_").slice(0, 96) || "unknown";
    const safeRoot = root.replaceAll("\\", "/").replace(/\/$/, "");
    return `${safeRoot}/${safeSession}/${Math.max(0, Math.trunc(observedAtMs))}-cache-break.json`;
  }

  isExcludedModel(model: string, excluded: readonly string[] = []): boolean {
    return excluded.some((pattern) => wildcardMatches(model, pattern));
  }

  checkResponseForCacheBreak(
    current: ProviderCacheSample,
    options: {
      excludedModels?: readonly string[];
      maxGapMs?: number;
      minimumPreviousReadTokens?: number;
      collapseRatio?: number;
    } = {},
  ): ProviderCacheBreak {
    const previous = this.samples.get(current.sessionId);
    this.samples.set(current.sessionId, { ...current });
    const base = {
      previousReadTokens: previous?.cacheReadInputTokens ?? 0,
      currentReadTokens: current.cacheReadInputTokens,
      readRatio: previous && previous.cacheReadInputTokens > 0
        ? current.cacheReadInputTokens / previous.cacheReadInputTokens
        : 1,
      suppressed: Boolean(current.deletionObserved || current.compactionObserved),
    };
    if (this.isExcludedModel(current.model, options.excludedModels)) {
      return { ...base, broken: false, reason: "excluded-model" };
    }
    if (!previous) return { ...base, broken: false, reason: "initial-sample" };
    if (previous.model !== current.model) return { ...base, broken: true, reason: "model-change" };
    if (previous.promptHash !== current.promptHash && !base.suppressed) {
      return { ...base, broken: true, reason: "prompt-change" };
    }
    if (current.observedAtMs - previous.observedAtMs > (options.maxGapMs ?? 15 * 60_000)) {
      return { ...base, broken: true, reason: "observation-gap" };
    }
    const minimum = options.minimumPreviousReadTokens ?? 128;
    const ratio = options.collapseRatio ?? 0.25;
    if (!base.suppressed && previous.cacheReadInputTokens >= minimum && base.readRatio < ratio) {
      return { ...base, broken: true, reason: "cache-read-collapse" };
    }
    return { ...base, broken: false, reason: "none" };
  }

  logToolUseToolResultMismatch(
    preNormalization: readonly unknown[],
    postNormalization: readonly unknown[],
  ): ToolMismatchEvidence {
    const uses = new Map<string, number[]>();
    const results = new Map<string, number[]>();
    postNormalization.forEach((message, messageIndex) => {
      for (const block of contentBlocks(message)) {
        const type = stringOf(block.type);
        const id = toolUseId(block);
        if (!id) continue;
        const target = type === "tool_use" ? uses : type === "tool_result" ? results : null;
        if (!target) continue;
        const indexes = target.get(id) ?? [];
        indexes.push(messageIndex);
        target.set(id, indexes);
      }
    });
    const missingResultIds = [...uses.keys()].filter((id) => !results.has(id)).sort();
    const orphanResultIds = [...results.keys()].filter((id) => !uses.has(id)).sort();
    const duplicateUseIds = [...uses].filter(([, indexes]) => indexes.length > 1).map(([id]) => id).sort();
    const duplicateResultIds = [...results].filter(([, indexes]) => indexes.length > 1).map(([id]) => id).sort();
    return {
      missingResultIds,
      orphanResultIds,
      duplicateUseIds,
      duplicateResultIds,
      toolUseIndexes: Object.fromEntries(uses),
      toolResultIndexes: Object.fromEntries(results),
      preNormalizationCount: preNormalization.length,
      postNormalizationCount: postNormalization.length,
      valid: missingResultIds.length + orphanResultIds.length + duplicateUseIds.length + duplicateResultIds.length === 0,
    };
  }

  getAssistantMessageFromError(error: unknown): string {
    const record = recordOf(error);
    const status = numberOf(record?.status ?? record?.statusCode, 0);
    const code = stringOf(record?.code).toLowerCase();
    const message = stringOf(record?.message, String(error ?? "unknown provider error"));
    const normalized = `${code} ${message}`.toLowerCase();
    if (status === 401 || status === 403 || /auth|api.?key|credential|permission/.test(normalized)) {
      return "Provider authentication failed. Check the selected credential and provider permissions.";
    }
    if (status === 402 || /billing|credit|payment/.test(normalized)) {
      return "Provider billing is unavailable. Check credits, billing status, and account limits.";
    }
    if (status === 429 || /rate.?limit|too many requests|overloaded/.test(normalized)) {
      return "Provider rate limit reached. The runtime will apply bounded backoff before retrying.";
    }
    if (/timeout|timed out|etimedout|econnaborted|deadline|abort/.test(normalized)) {
      return "Provider request timed out. The runtime can retry with the preserved session state.";
    }
    if (/media|image|pdf|document|unsupported.*format/.test(normalized)) {
      return "Provider rejected an attachment. Check media type, size, and model capabilities.";
    }
    if (/organization|workspace|tenant/.test(normalized)) {
      return "Provider organization access failed. Check the configured organization or workspace.";
    }
    if (/refusal|safety|policy/.test(normalized)) {
      return "Provider declined the request under its safety policy. Revise the request before retrying.";
    }
    return `Provider request failed: ${message.slice(0, 320)}`;
  }

  getAnthropicClient(input: {
    providerId: string;
    endpoint?: string;
    model: string;
    credential: string;
    oauth?: boolean;
  }): AnthropicClientDescriptor | null {
    const provider = input.providerId.toLowerCase();
    if (!["anthropic", "bedrock", "vertex"].includes(provider)) return null;
    const transport = provider as AnthropicClientDescriptor["transport"];
    const endpoint = input.endpoint || (
      transport === "anthropic"
        ? "https://api.anthropic.com"
        : transport === "bedrock"
          ? "https://bedrock-runtime.amazonaws.com"
          : "https://aiplatform.googleapis.com"
    );
    const auth: AnthropicClientDescriptor["auth"] = transport === "bedrock"
      ? "aws"
      : transport === "vertex"
        ? "gcp"
        : input.oauth
          ? "oauth"
          : "api-key";
    const headers: Record<string, string> = transport === "anthropic"
      ? input.oauth
        ? { authorization: "Bearer [redacted]", "anthropic-version": "2023-06-01" }
        : { "x-api-key": "[redacted]", "anthropic-version": "2023-06-01" }
      : {};
    return {
      family: "anthropic",
      transport,
      endpoint,
      model: input.model,
      auth,
      credentialFingerprint: stableFingerprint(input.credential),
      headers,
    };
  }

  applyProviderRequestCustody(request: unknown): ProviderRequestCustodyEffect {
    const record = recordOf(request);
    const metadata = recordOf(record?.metadata);
    const providerId = stringOf(record?.providerId ?? record?.provider ?? metadata?.providerId, "compatible");
    const model = stringOf(record?.model ?? metadata?.model, "unknown");
    const sessionId = stringOf(record?.sessionId ?? metadata?.sessionId, "default");
    const querySource = stringOf(record?.querySource ?? metadata?.querySource, "runtime");
    const ttl: CacheTtl = this.should1hCacheTTL({
      sessionId,
      providerId,
      model,
      querySource,
      oneHourEnabled: booleanOf(record?.oneHourCacheEnabled ?? metadata?.oneHourCacheEnabled),
      oneHourModels: Array.isArray(record?.oneHourCacheModels) ? record.oneHourCacheModels.map(String) : undefined,
      oneHourSources: Array.isArray(record?.oneHourCacheSources) ? record.oneHourCacheSources.map(String) : undefined,
    }) ? "1h" : "5m";
    const messages = Array.isArray(record?.messages) ? record.messages : [];
    const normalized = this.addCacheBreakpoints(messages, {
      ttl,
      skipCacheWrite: booleanOf(record?.skipCacheWrite ?? metadata?.skipCacheWrite),
      pinnedToolResultIds: Array.isArray(record?.pinnedToolResultIds)
        ? record.pinnedToolResultIds.map(String)
        : [],
    });
    if (record && Array.isArray(record.messages)) record.messages = normalized;
    const mismatch = this.logToolUseToolResultMismatch(messages, normalized);
    const credential = stringOf(record?.credential ?? metadata?.credential);
    const client = credential
      ? this.getAnthropicClient({
        providerId,
        endpoint: stringOf(record?.endpoint ?? metadata?.endpoint) || undefined,
        model,
        credential,
        oauth: booleanOf(record?.oauth ?? metadata?.oauth),
      })
      : null;
    const previousSample = recordOf(record?.previousCacheSample);
    const currentSample = recordOf(record?.cacheSample);
    const cacheBreak = currentSample
      ? this.checkResponseForCacheBreak({
        sessionId,
        model,
        promptHash: stringOf(currentSample.promptHash),
        cacheReadInputTokens: numberOf(currentSample.cacheReadInputTokens),
        cacheCreationInputTokens: numberOf(currentSample.cacheCreationInputTokens),
        observedAtMs: numberOf(currentSample.observedAtMs, Date.now()),
        deletionObserved: booleanOf(currentSample.deletionObserved),
        compactionObserved: booleanOf(currentSample.compactionObserved),
      })
      : previousSample
        ? this.checkResponseForCacheBreak({
          sessionId,
          model,
          promptHash: stringOf(previousSample.promptHash),
          cacheReadInputTokens: numberOf(previousSample.cacheReadInputTokens),
          observedAtMs: numberOf(previousSample.observedAtMs, Date.now()),
        })
        : null;
    const diffRoot = stringOf(record?.cacheBreakDiffRoot ?? metadata?.cacheBreakDiffRoot);
    const diffPath = diffRoot
      ? this.getCacheBreakDiffPath(diffRoot, sessionId, Date.now())
      : null;
    const effect: ProviderRequestCustodyEffect = {
      applied: Boolean(record),
      ttl,
      breakpointCount: normalized.flatMap(contentBlocks).filter((block) => block.cacheControl !== undefined).length,
      mismatch,
      client,
      cacheBreak,
      diffPath,
    };
    if (record) record.sourceCustody = effect;
    return effect;
  }

  snapshot(): { oneHourSessions: string[]; samples: ProviderCacheSample[] } {
    return {
      oneHourSessions: [...this.oneHourSessions].sort(),
      samples: [...this.samples.values()].map((sample) => ({ ...sample })),
    };
  }

  restore(snapshot: { oneHourSessions: readonly string[]; samples: readonly ProviderCacheSample[] }): void {
    this.oneHourSessions.clear();
    for (const sessionId of snapshot.oneHourSessions) this.oneHourSessions.add(sessionId);
    this.samples.clear();
    for (const sample of snapshot.samples) this.samples.set(sample.sessionId, { ...sample });
  }
}

export const providerCacheCustodyRuntime = new ProviderCacheCustodyRuntime();
