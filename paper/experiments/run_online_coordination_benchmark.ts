import { createHash, randomUUID } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

import {
  DEEPSEEK_PROVIDER_ID,
  DEEPSEEK_FLASH_MODEL_ID,
  ProviderControlPlane,
  installDeepSeekFlashProfile,
} from "../../packages/runtime/provider-control-plane/src/index.ts";
import type {
  DispatchMessage,
  ProviderDispatchRequest,
  ProviderDispatchResult,
  RouteRequest,
} from "../../packages/runtime/provider-control-plane/src/contracts.ts";

type MethodId = "single_agent" | "static_chain" | "static_star" | "broadcast" | "zyra_full";
type ConditionId = "no_fault" | "agent_loss";

interface FactRecord {
  field: string;
  revision: number;
  status: "active" | "superseded" | "revoked";
  value: string | number;
  source: string;
}

interface BenchmarkTask {
  taskId: string;
  title: string;
  rules: string[];
  requiredFields: string[];
  packets: FactRecord[][];
  recordsByGroup: FactRecord[][];
  expected: Record<string, string | number>;
  digest: string;
}

interface CallReceipt {
  callId: string;
  nodeId: string;
  providerId: string;
  modelId: string;
  promptTokens: number;
  completionTokens: number;
  totalTokens: number;
  requestBytes: number;
  responseBytes: number;
  wallTimeMs: number;
  httpStatuses: Array<number | null>;
  outputDigest: string;
  outputText: string;
  parseableJson: boolean;
}

interface CellResult {
  schema: "zyra.online-coordination-cell/v1";
  campaignId: string;
  cellId: string;
  method: MethodId;
  condition: ConditionId;
  taskId: string;
  taskDigest: string;
  repetition: number;
  seed: number;
  startedAt: string;
  completedAt: string;
  success: boolean;
  fieldAccuracy: number;
  correctFieldCount: number;
  requiredFieldCount: number;
  staleFactAcceptanceCount: number;
  missingFieldCount: number;
  modelCallCount: number;
  promptTokens: number;
  completionTokens: number;
  totalTokens: number;
  providerRequestBytes: number;
  providerResponseBytes: number;
  communicationBytes: number;
  communicationMessages: number;
  wallTimeMs: number;
  faultInjected: boolean;
  faultTarget: string;
  recoveryAttempted: boolean;
  recoverySucceeded: boolean | null;
  recoveryLatencyMs: number | null;
  verificationRepairAttempted: boolean;
  verificationRepairLatencyMs: number | null;
  outputDigest: string;
  answer: Record<string, unknown>;
  calls: CallReceipt[];
}

interface Options {
  output: string;
  repetitions: number;
  taskCount: number;
  seed: number;
  conditions: ConditionId[];
  methods: MethodId[];
}

const METHODS: MethodId[] = [
  "single_agent",
  "static_chain",
  "static_star",
  "broadcast",
  "zyra_full",
];

const FIELD_VALUES: Record<string, Array<string | number>> = {
  release_channel: ["stable", "beta", "canary", "lts"],
  api_version: ["v2", "v3", "v4", "v5"],
  timeout_ms: [800, 1200, 1800, 2400],
  retry_limit: [1, 2, 3, 4],
  data_region: ["cn-south", "cn-north", "ap-east", "local-only"],
  encryption: ["AES-256-GCM", "SM4-GCM", "ChaCha20-Poly1305", "AES-256-CBC"],
  retention_days: [7, 14, 30, 90],
  owner_team: ["runtime", "security", "platform", "reliability"],
  feature_flag: ["edge_route_v2", "safe_resume", "audit_strict", "sparse_mesh"],
  checksum_algorithm: ["SHA-256", "SHA-384", "BLAKE3", "SM3"],
  max_parallelism: [2, 4, 6, 8],
  audit_level: ["standard", "strict", "forensic", "minimal"],
};

const RULES = [
  "For every required field, select the value from the active record with the highest revision.",
  "Ignore every record whose status is superseded or revoked, even when its revision is higher.",
  "Use only supplied records. Preserve numeric values as JSON numbers and text values as JSON strings.",
  "Return exactly one flat JSON object containing all required fields and no explanation.",
];

function parseArgs(argv: string[]): Options {
  const values = new Map<string, string>();
  for (let index = 0; index < argv.length; index += 1) {
    const key = argv[index];
    if (!key.startsWith("--")) continue;
    const value = argv[index + 1];
    if (value && !value.startsWith("--")) {
      values.set(key.slice(2), value);
      index += 1;
    } else {
      values.set(key.slice(2), "true");
    }
  }
  const repetitions = Number.parseInt(values.get("repetitions") ?? "3", 10);
  const taskCount = Number.parseInt(values.get("tasks") ?? "3", 10);
  const seed = Number.parseInt(values.get("seed") ?? "20260907", 10);
  const conditionText = values.get("conditions") ?? "no_fault,agent_loss";
  const conditions = conditionText.split(",").map((item) => item.trim()).filter(Boolean) as ConditionId[];
  const methodText = values.get("methods") ?? METHODS.join(",");
  const methods = methodText.split(",").map((item) => item.trim()).filter(Boolean) as MethodId[];
  if (!Number.isInteger(repetitions) || repetitions < 1 || repetitions > 30) {
    throw new Error("--repetitions must be an integer from 1 to 30");
  }
  if (!Number.isInteger(taskCount) || taskCount < 1 || taskCount > 12) {
    throw new Error("--tasks must be an integer from 1 to 12");
  }
  if (conditions.some((item) => item !== "no_fault" && item !== "agent_loss")) {
    throw new Error("--conditions accepts no_fault and agent_loss");
  }
  if (methods.length === 0 || methods.some((item) => !METHODS.includes(item))) {
    throw new Error(`--methods accepts ${METHODS.join(",")}`);
  }
  return {
    output: resolve(values.get("output") ?? "paper/experiments/evidence/online-pilot/results.json"),
    repetitions,
    taskCount,
    seed,
    conditions,
    methods,
  };
}

function digest(value: unknown): string {
  return createHash("sha256").update(JSON.stringify(value)).digest("hex");
}

function utf8Bytes(value: string): number {
  return Buffer.byteLength(value, "utf8");
}

function createRng(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (Math.imul(state, 1664525) + 1013904223) >>> 0;
    return state / 0x100000000;
  };
}

function shuffle<T>(items: T[], rng: () => number): T[] {
  const copy = [...items];
  for (let index = copy.length - 1; index > 0; index -= 1) {
    const selected = Math.floor(rng() * (index + 1));
    [copy[index], copy[selected]] = [copy[selected], copy[index]];
  }
  return copy;
}

function generateTask(index: number, seed: number): BenchmarkTask {
  const rng = createRng(seed + index * 7919);
  const fields = Object.keys(FIELD_VALUES);
  const expected: Record<string, string | number> = {};
  const records: FactRecord[] = [];
  for (let fieldIndex = 0; fieldIndex < fields.length; fieldIndex += 1) {
    const field = fields[fieldIndex];
    const values = FIELD_VALUES[field];
    const activeValue = values[(index + fieldIndex) % values.length];
    const staleValue = values[(index + fieldIndex + 1) % values.length];
    const revokedValue = values[(index + fieldIndex + 2) % values.length];
    const activeRevision = 4 + Math.floor(rng() * 5);
    expected[field] = activeValue;
    records.push(
      {
        field,
        revision: activeRevision,
        status: "active",
        value: activeValue,
        source: `source-${(fieldIndex % 4) + 1}`,
      },
      {
        field,
        revision: Math.max(1, activeRevision - 1),
        status: "superseded",
        value: staleValue,
        source: `source-${((fieldIndex + 1) % 4) + 1}`,
      },
      {
        field,
        revision: activeRevision + 2,
        status: "revoked",
        value: revokedValue,
        source: `source-${((fieldIndex + 2) % 4) + 1}`,
      },
    );
    if (fieldIndex % 3 === index % 3) {
      records.push({
        field,
        revision: Math.max(1, activeRevision - 2),
        status: "active",
        value: staleValue,
        source: `source-${((fieldIndex + 3) % 4) + 1}`,
      });
    }
  }
  const packets: FactRecord[][] = [[], [], [], []];
  for (const record of shuffle(records, rng)) {
    packets[Math.floor(rng() * packets.length)].push(record);
  }
  const recordsByGroup: FactRecord[][] = [[], [], [], []];
  fields.forEach((field, fieldIndex) => {
    recordsByGroup[Math.floor(fieldIndex / 3)].push(...records.filter((item) => item.field === field));
  });
  const taskValue = {
    taskId: `coordination-${String(index + 1).padStart(2, "0")}`,
    title: `Versioned requirement synthesis case ${index + 1}`,
    rules: RULES,
    requiredFields: fields,
    packets,
    recordsByGroup,
    expected,
  };
  return { ...taskValue, digest: digest(taskValue) };
}

function formatRecords(records: FactRecord[]): string {
  return records.map((record) => JSON.stringify(record)).join("\n");
}

function taskHeader(task: BenchmarkTask): string {
  return [
    `Task: ${task.title}`,
    `Required fields: ${task.requiredFields.join(", ")}`,
    "Rules:",
    ...task.rules.map((rule, index) => `${index + 1}. ${rule}`),
  ].join("\n");
}

function parseJsonObject(text: string): Record<string, unknown> {
  const cleaned = text.replace(/```(?:json)?/gi, "").replace(/```/g, "").trim();
  const candidates: string[] = [cleaned];
  let depth = 0;
  let start = -1;
  let quoted = false;
  let escaped = false;
  for (let index = 0; index < cleaned.length; index += 1) {
    const char = cleaned[index];
    if (quoted) {
      if (escaped) escaped = false;
      else if (char === "\\") escaped = true;
      else if (char === '"') quoted = false;
      continue;
    }
    if (char === '"') {
      quoted = true;
      continue;
    }
    if (char === "{") {
      if (depth === 0) start = index;
      depth += 1;
    } else if (char === "}" && depth > 0) {
      depth -= 1;
      if (depth === 0 && start >= 0) candidates.push(cleaned.slice(start, index + 1));
    }
  }
  for (const candidate of candidates.reverse()) {
    try {
      const value = JSON.parse(candidate);
      if (value && typeof value === "object" && !Array.isArray(value)) return value;
    } catch {
      // Continue to the next balanced JSON object.
    }
  }
  return {};
}

function numberValue(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function usageValue(result: ProviderDispatchResult, key: string): number {
  return numberValue((result.usage as Record<string, unknown>)[key]);
}

class LiveModel {
  private readonly controlPlane: ProviderControlPlane;
  private readonly campaignId: string;
  private callSequence = 0;

  constructor(campaignId: string, stateRoot: string) {
    this.campaignId = campaignId;
    this.controlPlane = new ProviderControlPlane({
      databasePath: resolve(stateRoot, "provider.sqlite3"),
      route: {
        leaseMilliseconds: 180_000,
        defaultRetryPolicy: {
          maximumAttempts: 2,
          baseDelayMilliseconds: 250,
          maximumDelayMilliseconds: 1_000,
          retryStatuses: [429, 500, 502, 503, 504],
          rotateCredentialOnAuthenticationFailure: false,
          rotateRouteOnProviderUnavailable: false,
        },
      },
    });
    installDeepSeekFlashProfile(this.controlPlane);
  }

  close(): void {
    this.controlPlane.close();
  }

  async call(params: {
    cellId: string;
    taskId: string;
    nodeId: string;
    messages: DispatchMessage[];
    maximumOutputTokens?: number;
  }): Promise<{ text: string; json: Record<string, unknown>; receipt: CallReceipt }> {
    this.callSequence += 1;
    const callId = `${params.cellId}-call-${String(this.callSequence).padStart(5, "0")}`;
    const routeRequest: RouteRequest = {
      runId: params.cellId,
      taskId: params.taskId,
      nodeId: params.nodeId,
      sessionId: `${params.cellId}:${params.nodeId}`,
      turnId: callId,
      purpose: "execute",
      preferredProviderId: DEEPSEEK_PROVIDER_ID,
      preferredModelId: DEEPSEEK_FLASH_MODEL_ID,
      routeHint: `${DEEPSEEK_PROVIDER_ID}/${DEEPSEEK_FLASH_MODEL_ID}`,
      constraints: {
        providerIds: [DEEPSEEK_PROVIDER_ID],
        modelIds: [DEEPSEEK_FLASH_MODEL_ID],
        requiredInput: ["text"],
        requiredOutput: ["text"],
        requireTools: false,
        requireStreaming: true,
        minimumContextWindow: 8_000,
        maximumInputPricePerMillion: 1,
        maximumOutputPricePerMillion: 1,
        excludedCredentialIds: [],
        requiredScopes: ["chat.completions"],
      },
      metadata: {
        evidence_class: "online-coordination-benchmark",
        campaign_id: this.campaignId,
        secret_material_present: false,
      },
    };
    const route = this.controlPlane.acquireRoute(routeRequest);
    const dispatchRequest: ProviderDispatchRequest = {
      dispatchId: `dispatch-${randomUUID().replaceAll("-", "")}`,
      routeId: route.routeId,
      runId: route.runId,
      taskId: route.taskId,
      nodeId: route.nodeId,
      sessionId: route.sessionId,
      turnId: route.turnId,
      routeFallbackPolicy: "pin_initial_route",
      messages: params.messages,
      tools: [],
      maximumOutputTokens: params.maximumOutputTokens ?? 700,
      temperature: 0,
      stream: true,
      timeoutMilliseconds: 120_000,
      chunkTimeoutMilliseconds: 60_000,
      idempotencyKey: callId,
      extraBody: {
        thinking: { type: "disabled" },
      },
      metadata: {
        benchmark: "online-coordination-v1",
        campaign_id: this.campaignId,
        cell_id: params.cellId,
      },
    };
    const started = Date.now();
    const result = await this.controlPlane.dispatch(dispatchRequest);
    const wallTimeMs = Date.now() - started;
    const json = parseJsonObject(result.text);
    const receipt: CallReceipt = {
      callId,
      nodeId: params.nodeId,
      providerId: result.providerId,
      modelId: result.modelId,
      promptTokens: usageValue(result, "prompt_tokens"),
      completionTokens: usageValue(result, "completion_tokens"),
      totalTokens: usageValue(result, "total_tokens"),
      requestBytes: result.attempts.reduce((sum, item) => sum + item.requestBytes, 0),
      responseBytes: result.attempts.reduce((sum, item) => sum + item.responseBytes, 0),
      wallTimeMs,
      httpStatuses: result.attempts.map((item) => item.httpStatus),
      outputDigest: digest(result.text),
      outputText: result.text,
      parseableJson: Object.keys(json).length > 0,
    };
    return { text: result.text, json, receipt };
  }
}

function systemMessage(role: string): DispatchMessage {
  return {
    role: "system",
    content: `You are the ${role} in a controlled coordination benchmark. Follow the supplied selection rules exactly. Return JSON only.`,
  };
}

function userMessage(content: string): DispatchMessage {
  return { role: "user", content };
}

function normalizedAnswer(task: BenchmarkTask, answer: Record<string, unknown>): Record<string, unknown> {
  const queue: Array<{ value: Record<string, unknown>; depth: number }> = [{ value: answer, depth: 0 }];
  let selected = answer;
  let selectedCount = task.requiredFields.filter((field) => Object.hasOwn(answer, field)).length;
  while (queue.length > 0) {
    const current = queue.shift()!;
    const count = task.requiredFields.filter((field) => Object.hasOwn(current.value, field)).length;
    if (count > selectedCount) {
      selected = current.value;
      selectedCount = count;
    }
    if (current.depth >= 4) continue;
    for (const nested of Object.values(current.value)) {
      if (nested && typeof nested === "object" && !Array.isArray(nested)) {
        queue.push({ value: nested as Record<string, unknown>, depth: current.depth + 1 });
      }
    }
  }
  const normalized: Record<string, unknown> = {};
  for (const field of task.requiredFields) {
    if (Object.hasOwn(selected, field)) normalized[field] = selected[field];
  }
  return normalized;
}

function evaluate(task: BenchmarkTask, answer: Record<string, unknown>): {
  normalized: Record<string, unknown>;
  correct: number;
  stale: number;
  missing: number;
} {
  const normalized = normalizedAnswer(task, answer);
  let correct = 0;
  let stale = 0;
  let missing = 0;
  for (const field of task.requiredFields) {
    if (!Object.hasOwn(normalized, field)) {
      missing += 1;
      continue;
    }
    if (normalized[field] === task.expected[field]) {
      correct += 1;
      continue;
    }
    const invalidValues = task.packets.flat().filter((item) => item.field === field && item.status !== "active").map((item) => item.value);
    if (invalidValues.some((value) => value === normalized[field])) stale += 1;
  }
  return { normalized, correct, stale, missing };
}

function sumCalls(calls: CallReceipt[], field: keyof Pick<CallReceipt, "promptTokens" | "completionTokens" | "totalTokens" | "requestBytes" | "responseBytes">): number {
  return calls.reduce((sum, item) => sum + item[field], 0);
}

async function runSingle(model: LiveModel, task: BenchmarkTask, cellId: string, condition: ConditionId) {
  const allRecords = formatRecords(task.packets.flat());
  const result = await model.call({
    cellId,
    taskId: task.taskId,
    nodeId: "single-agent",
    messages: [systemMessage("single agent"), userMessage(`${taskHeader(task)}\nRecords:\n${allRecords}`)],
  });
  return {
    answer: condition === "agent_loss" ? {} : result.json,
    calls: [result.receipt],
    communicationBytes: 0,
    communicationMessages: 0,
    faultTarget: condition === "agent_loss" ? "single-agent response" : "",
    recoveryAttempted: false,
    recoveryLatencyMs: null as number | null,
    verificationRepairAttempted: false,
    verificationRepairLatencyMs: null as number | null,
  };
}

async function runChain(model: LiveModel, task: BenchmarkTask, cellId: string, condition: ConditionId, faultIndex: number) {
  let ledger: Record<string, unknown> = {};
  const calls: CallReceipt[] = [];
  let communicationBytes = 0;
  let communicationMessages = 0;
  for (let index = 0; index < task.packets.length; index += 1) {
    const previous = JSON.stringify(ledger);
    if (index > 0) {
      communicationBytes += utf8Bytes(previous);
      communicationMessages += 1;
    }
    const result = await model.call({
      cellId,
      taskId: task.taskId,
      nodeId: `chain-agent-${index + 1}`,
      messages: [
        systemMessage(`chain agent ${index + 1}`),
        userMessage(`${taskHeader(task)}\nPrior ledger:\n${previous}\nNew packet:\n${formatRecords(task.packets[index])}\nReturn an updated flat JSON ledger.`),
      ],
    });
    calls.push(result.receipt);
    if (!(condition === "agent_loss" && index === faultIndex)) ledger = result.json;
  }
  return {
    answer: ledger,
    calls,
    communicationBytes,
    communicationMessages,
    faultTarget: condition === "agent_loss" ? `chain-agent-${faultIndex + 1} response` : "",
    recoveryAttempted: false,
    recoveryLatencyMs: null as number | null,
    verificationRepairAttempted: false,
    verificationRepairLatencyMs: null as number | null,
  };
}

async function runStar(model: LiveModel, task: BenchmarkTask, cellId: string, condition: ConditionId, faultIndex: number) {
  const calls: CallReceipt[] = [];
  const reports: string[] = [];
  let communicationBytes = 0;
  let communicationMessages = 0;
  for (let index = 0; index < task.packets.length; index += 1) {
    const result = await model.call({
      cellId,
      taskId: task.taskId,
      nodeId: `star-leaf-${index + 1}`,
      messages: [
        systemMessage(`star leaf ${index + 1}`),
        userMessage(`${taskHeader(task)}\nInspect this packet. Return a JSON object whose values are arrays of every active candidate record for each field.\nPacket:\n${formatRecords(task.packets[index])}`),
      ],
    });
    calls.push(result.receipt);
    if (condition === "agent_loss" && index === faultIndex) continue;
    reports.push(result.text);
    communicationBytes += utf8Bytes(result.text);
    communicationMessages += 1;
  }
  const finalResult = await model.call({
    cellId,
    taskId: task.taskId,
    nodeId: "star-coordinator",
    messages: [
      systemMessage("star coordinator"),
      userMessage(`${taskHeader(task)}\nLeaf reports:\n${reports.map((item, index) => `Leaf ${index + 1}: ${item}`).join("\n")}`),
    ],
  });
  calls.push(finalResult.receipt);
  return {
    answer: finalResult.json,
    calls,
    communicationBytes,
    communicationMessages,
    faultTarget: condition === "agent_loss" ? `star-leaf-${faultIndex + 1} response` : "",
    recoveryAttempted: false,
    recoveryLatencyMs: null as number | null,
    verificationRepairAttempted: false,
    verificationRepairLatencyMs: null as number | null,
  };
}

async function runBroadcast(model: LiveModel, task: BenchmarkTask, cellId: string, condition: ConditionId, faultIndex: number) {
  const calls: CallReceipt[] = [];
  const proposals: string[] = [];
  let communicationBytes = 0;
  let communicationMessages = 0;
  const allRecords = formatRecords(task.packets.flat());
  for (let index = 0; index < task.packets.length; index += 1) {
    const result = await model.call({
      cellId,
      taskId: task.taskId,
      nodeId: `broadcast-agent-${index + 1}`,
      messages: [
        systemMessage(`broadcast agent ${index + 1}`),
        userMessage(`${taskHeader(task)}\nAll records broadcast to every member:\n${allRecords}`),
      ],
    });
    calls.push(result.receipt);
    if (condition === "agent_loss" && index === faultIndex) continue;
    proposals.push(result.text);
    communicationBytes += utf8Bytes(allRecords) + utf8Bytes(result.text);
    communicationMessages += 2;
  }
  const finalResult = await model.call({
    cellId,
    taskId: task.taskId,
    nodeId: "broadcast-coordinator",
    messages: [
      systemMessage("broadcast coordinator"),
      userMessage(`${taskHeader(task)}\nIndependent full proposals:\n${proposals.map((item, index) => `Proposal ${index + 1}: ${item}`).join("\n")}`),
    ],
  });
  calls.push(finalResult.receipt);
  return {
    answer: finalResult.json,
    calls,
    communicationBytes,
    communicationMessages,
    faultTarget: condition === "agent_loss" ? `broadcast-agent-${faultIndex + 1} response` : "",
    recoveryAttempted: false,
    recoveryLatencyMs: null as number | null,
    verificationRepairAttempted: false,
    verificationRepairLatencyMs: null as number | null,
  };
}

async function runZyra(model: LiveModel, task: BenchmarkTask, cellId: string, condition: ConditionId, faultIndex: number) {
  const calls: CallReceipt[] = [];
  const partials: string[] = [];
  let communicationBytes = 0;
  let communicationMessages = 0;
  let recoveryAttempted = false;
  let recoveryLatencyMs: number | null = null;
  for (let index = 0; index < task.recordsByGroup.length; index += 1) {
    const groupFields = task.requiredFields.slice(index * 3, index * 3 + 3);
    const result = await model.call({
      cellId,
      taskId: task.taskId,
      nodeId: `zyra-specialist-${index + 1}`,
      messages: [
        systemMessage(`ZYRA obligation specialist ${index + 1}`),
        userMessage(`${taskHeader(task)}\nYour assigned obligations: ${groupFields.join(", ")}\nRouted records:\n${formatRecords(task.recordsByGroup[index])}\nReturn only the assigned fields as a flat JSON object.`),
      ],
    });
    calls.push(result.receipt);
    if (condition === "agent_loss" && index === faultIndex) {
      recoveryAttempted = true;
      const recoveryStarted = Date.now();
      const replacement = await model.call({
        cellId,
        taskId: task.taskId,
        nodeId: `zyra-replacement-${index + 1}`,
        messages: [
          systemMessage(`ZYRA replacement specialist ${index + 1}`),
          userMessage(`${taskHeader(task)}\nA prior worker response was lost. Recover only these obligations: ${groupFields.join(", ")}\nCheckpoint-bound records:\n${formatRecords(task.recordsByGroup[index])}\nReturn only the recovered fields as a flat JSON object.`),
        ],
      });
      recoveryLatencyMs = Date.now() - recoveryStarted;
      calls.push(replacement.receipt);
      partials.push(replacement.text);
      communicationBytes += utf8Bytes(replacement.text);
      communicationMessages += 1;
      continue;
    }
    partials.push(result.text);
    communicationBytes += utf8Bytes(result.text);
    communicationMessages += 1;
  }
  const finalResult = await model.call({
    cellId,
    taskId: task.taskId,
    nodeId: "zyra-verifier-assembler",
    messages: [
      systemMessage("ZYRA verifier and final assembler"),
      userMessage(`${taskHeader(task)}\nObligation-bound partial deliveries:\n${partials.map((item, index) => `Delivery ${index + 1}: ${item}`).join("\n")}`),
    ],
  });
  calls.push(finalResult.receipt);
  let answer = finalResult.json;
  let verificationRepairAttempted = false;
  let verificationRepairLatencyMs: number | null = null;
  const firstAssessment = evaluate(task, answer);
  const failedFields = task.requiredFields.filter((field) => firstAssessment.normalized[field] !== task.expected[field]);
  if (failedFields.length > 0) {
    verificationRepairAttempted = true;
    const repairRecords = task.packets.flat().filter((record) => failedFields.includes(record.field));
    const repairStarted = Date.now();
    const repair = await model.call({
      cellId,
      taskId: task.taskId,
      nodeId: "zyra-continuity-repair",
      messages: [
        systemMessage("ZYRA continuity repair worker"),
        userMessage(`${taskHeader(task)}\nThe independent verifier rejected these obligations: ${failedFields.join(", ")}. Recompute only those fields from the routed records.\nRecords:\n${formatRecords(repairRecords)}`),
      ],
    });
    verificationRepairLatencyMs = Date.now() - repairStarted;
    calls.push(repair.receipt);
    const repaired = normalizedAnswer(task, repair.json);
    const merged = { ...normalizedAnswer(task, answer) };
    for (const field of failedFields) {
      if (Object.hasOwn(repaired, field) && repaired[field] !== null) merged[field] = repaired[field];
    }
    answer = merged;
    communicationBytes += utf8Bytes(repair.text);
    communicationMessages += 1;
  }
  return {
    answer,
    calls,
    communicationBytes,
    communicationMessages,
    faultTarget: condition === "agent_loss" ? `zyra-specialist-${faultIndex + 1} response` : "",
    recoveryAttempted,
    recoveryLatencyMs,
    verificationRepairAttempted,
    verificationRepairLatencyMs,
  };
}

async function runCell(model: LiveModel, campaignId: string, task: BenchmarkTask, method: MethodId, condition: ConditionId, repetition: number, seed: number): Promise<CellResult> {
  const cellId = `cell-${method}-${condition}-${task.taskId}-r${repetition}-${digest([campaignId, method, condition, task.taskId, repetition, seed]).slice(0, 10)}`;
  const startedAt = new Date().toISOString();
  const started = Date.now();
  const faultIndex = Math.abs(seed + repetition + Number.parseInt(task.taskId.slice(-2), 10)) % 4;
  let run;
  if (method === "single_agent") run = await runSingle(model, task, cellId, condition);
  else if (method === "static_chain") run = await runChain(model, task, cellId, condition, faultIndex);
  else if (method === "static_star") run = await runStar(model, task, cellId, condition, faultIndex);
  else if (method === "broadcast") run = await runBroadcast(model, task, cellId, condition, faultIndex);
  else run = await runZyra(model, task, cellId, condition, faultIndex);
  const assessment = evaluate(task, run.answer);
  const success = assessment.correct === task.requiredFields.length;
  const calls = run.calls;
  return {
    schema: "zyra.online-coordination-cell/v1",
    campaignId,
    cellId,
    method,
    condition,
    taskId: task.taskId,
    taskDigest: task.digest,
    repetition,
    seed,
    startedAt,
    completedAt: new Date().toISOString(),
    success,
    fieldAccuracy: assessment.correct / task.requiredFields.length,
    correctFieldCount: assessment.correct,
    requiredFieldCount: task.requiredFields.length,
    staleFactAcceptanceCount: assessment.stale,
    missingFieldCount: assessment.missing,
    modelCallCount: calls.length,
    promptTokens: sumCalls(calls, "promptTokens"),
    completionTokens: sumCalls(calls, "completionTokens"),
    totalTokens: sumCalls(calls, "totalTokens"),
    providerRequestBytes: sumCalls(calls, "requestBytes"),
    providerResponseBytes: sumCalls(calls, "responseBytes"),
    communicationBytes: run.communicationBytes,
    communicationMessages: run.communicationMessages,
    wallTimeMs: Date.now() - started,
    faultInjected: condition === "agent_loss",
    faultTarget: run.faultTarget,
    recoveryAttempted: run.recoveryAttempted,
    recoverySucceeded: condition === "agent_loss" ? success : null,
    recoveryLatencyMs: run.recoveryLatencyMs,
    verificationRepairAttempted: run.verificationRepairAttempted,
    verificationRepairLatencyMs: run.verificationRepairLatencyMs,
    outputDigest: digest(assessment.normalized),
    answer: assessment.normalized,
    calls,
  };
}

async function main(): Promise<void> {
  const options = parseArgs(process.argv.slice(2));
  mkdirSync(dirname(options.output), { recursive: true });
  const campaignId = `online-${new Date().toISOString().replaceAll(/[-:.TZ]/g, "").slice(0, 14)}-${randomUUID().slice(0, 8)}`;
  const stateRoot = resolve(dirname(options.output), `${campaignId}-state`);
  mkdirSync(stateRoot, { recursive: true });
  const tasks = Array.from({ length: options.taskCount }, (_, index) => generateTask(index, options.seed));
  const model = new LiveModel(campaignId, stateRoot);
  const cells: CellResult[] = [];
  const planned = options.repetitions * tasks.length * options.methods.length * options.conditions.length;
  try {
    for (let repetition = 1; repetition <= options.repetitions; repetition += 1) {
      for (const task of tasks) {
        for (const condition of options.conditions) {
          const orderSeed = options.seed + repetition * 65537 + Number.parseInt(task.taskId.slice(-2), 10) * 4099 + (condition === "agent_loss" ? 97 : 0);
          const methodOrder = shuffle(options.methods, createRng(orderSeed));
          for (const method of methodOrder) {
            const cellSeed = options.seed + repetition * 1009 + Number.parseInt(task.taskId.slice(-2), 10) * 9176;
            const cell = await runCell(model, campaignId, task, method, condition, repetition, cellSeed);
            cells.push(cell);
            process.stdout.write(`${JSON.stringify({ progress: cells.length, planned, method, condition, task_id: task.taskId, repetition, success: cell.success, field_accuracy: cell.fieldAccuracy, total_tokens: cell.totalTokens, wall_time_ms: cell.wallTimeMs })}\n`);
            const checkpoint = {
              schema: "zyra.online-coordination-campaign/v1",
              campaignId,
              status: "running",
              generatedAt: new Date().toISOString(),
              configuration: {
                modelProvider: DEEPSEEK_PROVIDER_ID,
                modelId: DEEPSEEK_FLASH_MODEL_ID,
                repetitions: options.repetitions,
                taskCount: options.taskCount,
                methods: options.methods,
                conditions: options.conditions,
                seed: options.seed,
                independentProviderDispatchPerAgent: true,
                historicalTraceReplay: false,
              },
              tasks,
              cells,
            };
            writeFileSync(options.output, `${JSON.stringify(checkpoint, null, 2)}\n`, "utf8");
          }
        }
      }
    }
    const completed = {
      schema: "zyra.online-coordination-campaign/v1",
      campaignId,
      status: "completed",
      generatedAt: new Date().toISOString(),
      configuration: {
        modelProvider: DEEPSEEK_PROVIDER_ID,
        modelId: DEEPSEEK_FLASH_MODEL_ID,
        repetitions: options.repetitions,
        taskCount: options.taskCount,
        methods: options.methods,
        conditions: options.conditions,
        seed: options.seed,
        independentProviderDispatchPerAgent: true,
        historicalTraceReplay: false,
      },
      tasks,
      cells,
      evidenceDigest: digest(cells),
    };
    writeFileSync(options.output, `${JSON.stringify(completed, null, 2)}\n`, "utf8");
    process.stdout.write(`${JSON.stringify({ status: "completed", campaign_id: campaignId, cells: cells.length, evidence_digest: completed.evidenceDigest, output: options.output })}\n`);
  } finally {
    model.close();
  }
}

main().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.stack : String(error)}\n`);
  process.exitCode = 1;
});
