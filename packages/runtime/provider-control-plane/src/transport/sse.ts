import { ProviderControlPlaneError } from "../errors.ts";

export interface RawSseEvent {
  readonly event: string | null;
  readonly data: string;
  readonly id: string | null;
  readonly retry: number | null;
}

export async function* readSse(
  response: Response,
  options: { readonly chunkTimeoutMilliseconds: number; readonly signal?: AbortSignal },
): AsyncGenerator<RawSseEvent> {
  if (response.body === null) throw protocolError("SSE response has no body");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const part = await readWithTimeout(reader, options.chunkTimeoutMilliseconds, options.signal);
      if (part.done) break;
      buffer += decoder.decode(part.value, { stream: true }).replaceAll("\r\n", "\n").replaceAll("\r", "\n");
      let boundary = buffer.indexOf("\n\n");
      while (boundary >= 0) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const event = parseSseBlock(block);
        if (event !== null) yield event;
        boundary = buffer.indexOf("\n\n");
      }
    }
    buffer += decoder.decode();
    if (buffer.trim()) {
      const event = parseSseBlock(buffer);
      if (event !== null) yield event;
    }
  } finally {
    reader.releaseLock();
  }
}

export function parseSseBlock(block: string): RawSseEvent | null {
  const data: string[] = [];
  let event: string | null = null;
  let id: string | null = null;
  let retry: number | null = null;
  for (const line of block.split("\n")) {
    if (!line || line.startsWith(":")) continue;
    const separator = line.indexOf(":");
    const field = separator < 0 ? line : line.slice(0, separator);
    let value = separator < 0 ? "" : line.slice(separator + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "data") data.push(value);
    else if (field === "event") event = value;
    else if (field === "id" && !value.includes("\0")) id = value;
    else if (field === "retry" && /^\d+$/.test(value)) retry = Number(value);
  }
  if (data.length === 0 && event === null && id === null && retry === null) return null;
  return { event, data: data.join("\n"), id, retry };
}

async function readWithTimeout(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  milliseconds: number,
  signal?: AbortSignal,
): Promise<ReadableStreamReadResult<Uint8Array>> {
  if (milliseconds <= 0) return reader.read();
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new ProviderControlPlaneError({
      layer: "transport",
      kind: "stream_timeout",
      message: `provider SSE chunk timed out after ${milliseconds} ms`,
      retryable: true,
      recoveryIntent: "change_provider_route",
    })), milliseconds);
  });
  const aborted = signal === undefined
    ? new Promise<never>(() => undefined)
    : new Promise<never>((_, reject) => signal.addEventListener("abort", () => reject(signal.reason ?? new DOMException("aborted", "AbortError")), { once: true }));
  try {
    return await Promise.race([reader.read(), timeout, aborted]);
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}

function protocolError(message: string): ProviderControlPlaneError {
  return new ProviderControlPlaneError({
    layer: "protocol",
    kind: "response_protocol_error",
    message,
    retryable: false,
    recoveryIntent: "surface_to_operator",
  });
}
