import type { JsonObject, JsonValue } from "../contracts.ts";
import { canonicalJson, cloneJson, deterministicMcpId, sha256 } from "../core/canonical.ts";
import { McpRuntimeError } from "../core/failure.ts";

export interface McpPage<T> {
  items: T[];
  nextCursor: string | null;
  metadata: JsonObject;
}

export interface McpPaginationResult<T> {
  items: T[];
  pages: number;
  cursors: string[];
  itemDigests: string[];
  duplicates: number;
  truncated: boolean;
  digest: string;
  metadata: JsonObject;
}

export class McpPaginationRuntime {
  private readonly maximumPages: number;
  private readonly maximumItems: number;
  private readonly rejectDuplicateCursors: boolean;

  constructor(options: { maximumPages?: number; maximumItems?: number; rejectDuplicateCursors?: boolean } = {}) {
    this.maximumPages = options.maximumPages ?? 1_000;
    this.maximumItems = options.maximumItems ?? 100_000;
    this.rejectDuplicateCursors = options.rejectDuplicateCursors ?? true;
  }

  async collect<T extends JsonValue>(
    operation: string,
    fetchPage: (cursor: string | null, page: number) => Promise<McpPage<T>>,
    options: {
      itemKey?: (item: T) => string;
      signal?: AbortSignal;
      metadata?: JsonObject;
      truncateAtLimit?: boolean;
    } = {},
  ): Promise<McpPaginationResult<T>> {
    const items: T[] = [];
    const cursors: string[] = [];
    const cursorSet = new Set<string>();
    const itemDigests: string[] = [];
    const itemKeys = new Set<string>();
    let cursor: string | null = null;
    let page = 0;
    let duplicates = 0;
    let truncated = false;
    while (page < this.maximumPages) {
      if (options.signal?.aborted) throw paginationError(operation, "pagination_cancelled", `${operation} pagination was cancelled`);
      page += 1;
      const result = await fetchPage(cursor, page);
      if (!Array.isArray(result.items)) throw paginationError(operation, "invalid_page_items", `${operation} page ${page} items are invalid`);
      for (const itemValue of result.items) {
        const item = canonicalJson(itemValue) as T;
        const key = options.itemKey ? options.itemKey(item) : sha256(item);
        if (itemKeys.has(key)) {
          duplicates += 1;
          continue;
        }
        itemKeys.add(key);
        items.push(item);
        itemDigests.push(sha256(item));
        if (items.length >= this.maximumItems) {
          if (options.truncateAtLimit) { truncated = true; break; }
          throw paginationError(operation, "pagination_item_limit", `${operation} exceeds ${this.maximumItems} items`);
        }
      }
      if (truncated || !result.nextCursor) break;
      if (cursorSet.has(result.nextCursor)) {
        if (this.rejectDuplicateCursors) throw paginationError(operation, "pagination_cursor_cycle", `${operation} repeated cursor ${result.nextCursor}`);
        break;
      }
      cursorSet.add(result.nextCursor);
      cursors.push(result.nextCursor);
      cursor = result.nextCursor;
    }
    if (page >= this.maximumPages && cursor !== null && !truncated) throw paginationError(operation, "pagination_page_limit", `${operation} exceeds ${this.maximumPages} pages`);
    const base = {
      items,
      pages: page,
      cursors,
      itemDigests,
      duplicates,
      truncated,
      metadata: cloneJson(options.metadata ?? {}),
    };
    return { ...base, digest: sha256(base) };
  }
}

function paginationError(operation: string, code: string, message: string): McpRuntimeError {
  return new McpRuntimeError({
    failureId: deterministicMcpId("mcp-pagination", { operation, code, message }),
    category: "protocol",
    code,
    message,
    operation,
    retryable: false,
    disposition: "terminal",
  });
}
