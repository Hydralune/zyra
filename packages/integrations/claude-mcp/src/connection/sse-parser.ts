import type { JsonObject } from "../contracts.ts";
import {
  canonicalJson,
  cloneJson,
  deterministicMcpId,
  positiveInteger,
  requiredText,
} from "../core/canonical.ts";
import { McpRuntimeError } from "../core/failure.ts";
import type { McpSseEvent, McpSseParserSnapshot } from "./contracts.ts";

export interface McpSseParserOptions {
  maximumBufferBytes?: number;
  maximumEventBytes?: number;
  maximumDataLines?: number;
  allowByteOrderMark?: boolean;
  now?: () => Date;
}

const decoder = new TextDecoder("utf-8", { fatal: true, ignoreBOM: false });

export class McpSseParser {
  private bufferValue = "";
  private eventNameValue = "";
  private dataLinesValue: string[] = [];
  private eventIdValue: string | null = null;
  private retryMsValue: number | null = null;
  private commentsValue: string[] = [];
  private totalBytesValue = 0;
  private emittedEventsValue = 0;
  private lastEventIdValue: string | null = null;
  private firstChunk = true;
  private readonly maximumBufferBytes: number;
  private readonly maximumEventBytes: number;
  private readonly maximumDataLines: number;
  private readonly allowByteOrderMark: boolean;
  private readonly now: () => Date;

  constructor(options: McpSseParserOptions = {}, snapshot?: McpSseParserSnapshot | null) {
    this.maximumBufferBytes = options.maximumBufferBytes ?? 4 * 1024 * 1024;
    this.maximumEventBytes = options.maximumEventBytes ?? 8 * 1024 * 1024;
    this.maximumDataLines = options.maximumDataLines ?? 100_000;
    this.allowByteOrderMark = options.allowByteOrderMark ?? true;
    this.now = options.now ?? (() => new Date());
    if (!Number.isSafeInteger(this.maximumBufferBytes) || this.maximumBufferBytes < 1_024) {
      throw sseError("invalid_buffer_limit", "SSE maximum buffer must be at least 1024 bytes");
    }
    if (!Number.isSafeInteger(this.maximumEventBytes) || this.maximumEventBytes < 1_024) {
      throw sseError("invalid_event_limit", "SSE maximum event must be at least 1024 bytes");
    }
    if (!Number.isSafeInteger(this.maximumDataLines) || this.maximumDataLines < 1) {
      throw sseError("invalid_line_limit", "SSE maximum data lines must be positive");
    }
    if (snapshot) this.restore(snapshot);
  }

  get lastEventId(): string | null {
    return this.lastEventIdValue;
  }

  get retryMs(): number | null {
    return this.retryMsValue;
  }

  get totalBytes(): number {
    return this.totalBytesValue;
  }

  get emittedEvents(): number {
    return this.emittedEventsValue;
  }

  push(chunk: Uint8Array | string, final = false): McpSseEvent[] {
    let text: string;
    let bytes: number;
    try {
      if (typeof chunk === "string") {
        text = chunk;
        bytes = Buffer.byteLength(chunk, "utf8");
      } else {
        text = decoder.decode(chunk, { stream: !final });
        bytes = chunk.byteLength;
      }
    } catch (error) {
      throw new McpRuntimeError({
        failureId: "mcp-sse-invalid-utf8",
        category: "protocol",
        code: "invalid_sse_utf8",
        message: "MCP SSE stream contains invalid UTF-8",
        retryable: false,
        disposition: "terminal",
      }, { cause: error });
    }
    if (this.firstChunk) {
      this.firstChunk = false;
      if (text.startsWith("\uFEFF")) {
        if (!this.allowByteOrderMark) throw sseError("unexpected_bom", "SSE stream starts with a byte-order mark");
        text = text.slice(1);
      }
    }
    this.totalBytesValue += bytes;
    this.bufferValue += text;
    if (Buffer.byteLength(this.bufferValue, "utf8") > this.maximumBufferBytes) {
      throw sseError("sse_buffer_overflow", `SSE line buffer exceeds ${this.maximumBufferBytes} bytes`);
    }
    const output: McpSseEvent[] = [];
    let newlineIndex = findNewline(this.bufferValue);
    while (newlineIndex >= 0) {
      const line = this.bufferValue.slice(0, newlineIndex);
      const consumed = newlineIndex + (this.bufferValue[newlineIndex] === "\r" && this.bufferValue[newlineIndex + 1] === "\n" ? 2 : 1);
      this.bufferValue = this.bufferValue.slice(consumed);
      const event = this.consumeLine(line);
      if (event) output.push(event);
      newlineIndex = findNewline(this.bufferValue);
    }
    if (final) {
      if (this.bufferValue.length) {
        const event = this.consumeLine(this.bufferValue);
        this.bufferValue = "";
        if (event) output.push(event);
      }
      const finalEvent = this.dispatch();
      if (finalEvent) output.push(finalEvent);
    }
    return output;
  }

  finish(): McpSseEvent[] {
    return this.push("", true);
  }

  reset(options: { preserveLastEventId?: boolean; preserveRetry?: boolean } = {}): void {
    this.bufferValue = "";
    this.eventNameValue = "";
    this.dataLinesValue = [];
    this.eventIdValue = options.preserveLastEventId ? this.lastEventIdValue : null;
    if (!options.preserveRetry) this.retryMsValue = null;
    this.commentsValue = [];
    this.firstChunk = true;
  }

  snapshot(): McpSseParserSnapshot {
    return {
      version: "zyra.mcp-sse-parser/v1",
      buffer: this.bufferValue,
      eventName: this.eventNameValue,
      dataLines: [...this.dataLinesValue],
      eventId: this.eventIdValue,
      retryMs: this.retryMsValue,
      comments: [...this.commentsValue],
      totalBytes: this.totalBytesValue,
      emittedEvents: this.emittedEventsValue,
      lastEventId: this.lastEventIdValue,
    };
  }

  restore(snapshot: McpSseParserSnapshot): void {
    if (snapshot.version !== "zyra.mcp-sse-parser/v1") throw sseError("unsupported_sse_snapshot", "unsupported SSE parser snapshot version");
    const bufferBytes = Buffer.byteLength(snapshot.buffer, "utf8");
    if (bufferBytes > this.maximumBufferBytes) throw sseError("sse_snapshot_buffer_overflow", "SSE parser snapshot buffer is too large");
    if (snapshot.dataLines.length > this.maximumDataLines) throw sseError("sse_snapshot_line_overflow", "SSE parser snapshot has too many data lines");
    this.bufferValue = snapshot.buffer;
    this.eventNameValue = snapshot.eventName;
    this.dataLinesValue = [...snapshot.dataLines];
    this.eventIdValue = snapshot.eventId;
    this.retryMsValue = snapshot.retryMs;
    this.commentsValue = [...snapshot.comments];
    this.totalBytesValue = snapshot.totalBytes;
    this.emittedEventsValue = snapshot.emittedEvents;
    this.lastEventIdValue = snapshot.lastEventId;
    this.firstChunk = false;
    this.validateCurrentEventSize();
  }

  private consumeLine(lineValue: string): McpSseEvent | null {
    const line = lineValue.endsWith("\r") ? lineValue.slice(0, -1) : lineValue;
    if (line === "") return this.dispatch();
    if (line.startsWith(":")) {
      this.commentsValue.push(line.slice(1).replace(/^ /, ""));
      this.validateCurrentEventSize();
      return null;
    }
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") {
      if (value.includes("\0")) throw sseError("invalid_sse_event_name", "SSE event name contains NUL");
      this.eventNameValue = value;
    } else if (field === "data") {
      if (this.dataLinesValue.length >= this.maximumDataLines) throw sseError("sse_data_line_overflow", "SSE event has too many data lines");
      this.dataLinesValue.push(value);
    } else if (field === "id") {
      if (!value.includes("\0")) this.eventIdValue = value;
    } else if (field === "retry") {
      if (/^\d+$/.test(value)) this.retryMsValue = Math.min(Number(value), 3_600_000);
    }
    this.validateCurrentEventSize();
    return null;
  }

  private dispatch(): McpSseEvent | null {
    if (!this.dataLinesValue.length) {
      this.eventNameValue = "";
      this.commentsValue = [];
      return null;
    }
    const data = this.dataLinesValue.join("\n");
    const eventName = this.eventNameValue || "message";
    const eventId = this.eventIdValue;
    const rawBytes = Buffer.byteLength(data, "utf8")
      + Buffer.byteLength(eventName, "utf8")
      + Buffer.byteLength(eventId ?? "", "utf8")
      + this.commentsValue.reduce((total, comment) => total + Buffer.byteLength(comment, "utf8"), 0);
    if (rawBytes > this.maximumEventBytes) throw sseError("sse_event_overflow", `SSE event exceeds ${this.maximumEventBytes} bytes`);
    const event: McpSseEvent = {
      eventId,
      event: eventName,
      data,
      retryMs: this.retryMsValue,
      comments: [...this.commentsValue],
      receivedAt: this.now().toISOString(),
      rawBytes,
    };
    if (eventId !== null) this.lastEventIdValue = eventId;
    this.emittedEventsValue += 1;
    this.eventNameValue = "";
    this.dataLinesValue = [];
    this.commentsValue = [];
    return event;
  }

  private validateCurrentEventSize(): void {
    const bytes = Buffer.byteLength(this.eventNameValue, "utf8")
      + Buffer.byteLength(this.eventIdValue ?? "", "utf8")
      + this.dataLinesValue.reduce((total, line) => total + Buffer.byteLength(line, "utf8") + 1, 0)
      + this.commentsValue.reduce((total, line) => total + Buffer.byteLength(line, "utf8") + 1, 0);
    if (bytes > this.maximumEventBytes) throw sseError("sse_event_overflow", `SSE event exceeds ${this.maximumEventBytes} bytes`);
  }
}

function findNewline(value: string): number {
  const lineFeed = value.indexOf("\n");
  const carriageReturn = value.indexOf("\r");
  if (lineFeed < 0) return carriageReturn;
  if (carriageReturn < 0) return lineFeed;
  return Math.min(lineFeed, carriageReturn);
}

function sseError(code: string, message: string): McpRuntimeError {
  return new McpRuntimeError({
    failureId: deterministicMcpId("mcp-sse", { code, message }),
    category: "protocol",
    code,
    message,
    retryable: false,
    disposition: "terminal",
  });
}

export function serializeSseEvent(event: McpSseEvent): string {
  const lines: string[] = [];
  for (const comment of event.comments) lines.push(`:${comment ? ` ${comment}` : ""}`);
  if (event.eventId !== null) lines.push(`id: ${event.eventId}`);
  if (event.event && event.event !== "message") lines.push(`event: ${event.event}`);
  if (event.retryMs !== null) lines.push(`retry: ${event.retryMs}`);
  for (const dataLine of event.data.split("\n")) lines.push(`data: ${dataLine}`);
  return `${lines.join("\n")}\n\n`;
}

export function parseSseJson(event: McpSseEvent): JsonObject {
  let value: unknown;
  try {
    value = JSON.parse(event.data);
  } catch (error) {
    throw new McpRuntimeError({
      failureId: deterministicMcpId("mcp-sse-json", { event_id: event.eventId, data: event.data }),
      category: "protocol",
      code: "invalid_sse_json",
      message: `SSE event ${event.eventId ?? "<none>"} is not valid JSON`,
      retryable: false,
      disposition: "terminal",
      details: { event: canonicalJson(event) as JsonObject },
    }, { cause: error });
  }
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw sseError("invalid_sse_json_shape", "SSE JSON event must contain an object");
  }
  return cloneJson(value as JsonObject);
}
