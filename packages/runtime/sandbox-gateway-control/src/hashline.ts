import {
  GatewayProtocolError,
  type HashlineApplyResult,
  type HashlineEdit,
  type HashlineLine,
  type HashlineRange,
  type HashlineSnapshot,
  type JsonValue,
} from "./contracts.ts";
import {
  contentDigest,
  digestJson,
  stableId,
} from "./canonical.ts";

export interface HashlineOptions {
  readonly maximumLines?: number;
  readonly maximumBytes?: number;
  readonly hashPrefixLength?: number;
}

interface NormalizedHashlineOptions {
  readonly maximumLines: number;
  readonly maximumBytes: number;
  readonly hashPrefixLength: number;
}

export class HashlinePatcher {
  readonly options: NormalizedHashlineOptions;

  constructor(options: HashlineOptions = {}) {
    this.options = Object.freeze({
      maximumLines: options.maximumLines ?? 1_000_000,
      maximumBytes: options.maximumBytes ?? 128 * 1024 * 1024,
      hashPrefixLength: options.hashPrefixLength ?? 16,
    });
  }

  snapshot(content: string): HashlineSnapshot {
    const bytes = Buffer.byteLength(content, "utf8");
    if (bytes > this.options.maximumBytes) {
      throw new GatewayProtocolError(
        "hashline_content_limit",
        "Hashline content exceeds byte budget",
      );
    }
    const newline: "\n" | "\r\n" = content.includes("\r\n")
      ? "\r\n"
      : "\n";
    const trailingNewline = content.endsWith("\n");
    const rawLines = content.length === 0
      ? []
      : content.replace(/\r\n/gu, "\n").split("\n");
    if (trailingNewline) {
      rawLines.pop();
    }
    if (rawLines.length > this.options.maximumLines) {
      throw new GatewayProtocolError(
        "hashline_line_limit",
        "Hashline content exceeds line budget",
      );
    }
    const lines = Object.freeze(
      rawLines.map((text, index) =>
        Object.freeze({
          lineNumber: index + 1,
          hash: this.lineHash(text),
          text,
        }),
      ),
    );
    const contentHash = contentDigest(content);
    const snapshotId = stableId("hashline-snapshot", {
      contentDigest: contentHash,
      newline,
      trailingNewline,
      lines: lines.map((line) => ({
        lineNumber: line.lineNumber,
        hash: line.hash,
      })),
    });
    return Object.freeze({
      snapshotId,
      contentDigest: contentHash,
      lineCount: lines.length,
      newline,
      trailingNewline,
      lines,
    });
  }

  range(
    snapshot: HashlineSnapshot,
    startLine: number,
    endLine: number,
  ): HashlineRange {
    if (
      !Number.isSafeInteger(startLine) ||
      !Number.isSafeInteger(endLine) ||
      startLine < 1 ||
      endLine < startLine ||
      endLine > snapshot.lineCount
    ) {
      throw new GatewayProtocolError(
        "hashline_range_invalid",
        "Hashline range is outside the snapshot",
      );
    }
    return Object.freeze({
      startLine,
      endLine,
      expectedStartHash: snapshot.lines[startLine - 1]?.hash ?? "",
      expectedEndHash: snapshot.lines[endLine - 1]?.hash ?? "",
    });
  }

  createEdit(
    snapshot: HashlineSnapshot,
    range: HashlineRange,
    replacement: string,
  ): HashlineEdit {
    this.assertRange(snapshot, range);
    return Object.freeze({
      editId: stableId("hashline-edit", {
        snapshotId: snapshot.snapshotId,
        range: range as unknown as JsonValue,
        replacementDigest: contentDigest(replacement),
      }),
      snapshotId: snapshot.snapshotId,
      range,
      replacement,
      expectedContentDigest: snapshot.contentDigest,
    });
  }

  apply(content: string, edit: HashlineEdit): HashlineApplyResult {
    const snapshot = this.snapshot(content);
    if (
      snapshot.snapshotId !== edit.snapshotId ||
      snapshot.contentDigest !== edit.expectedContentDigest
    ) {
      throw new GatewayProtocolError(
        "hashline_snapshot_mismatch",
        "Content changed after Hashline edit was prepared",
      );
    }
    this.assertRange(snapshot, edit.range);
    const normalized = content.replace(/\r\n/gu, "\n");
    const rawLines = normalized.length === 0 ? [] : normalized.split("\n");
    if (snapshot.trailingNewline) {
      rawLines.pop();
    }
    const replacement = edit.replacement.replace(/\r\n/gu, "\n");
    const replacementLines = replacement.length === 0
      ? []
      : replacement.split("\n");
    if (replacement.endsWith("\n")) {
      replacementLines.pop();
    }
    rawLines.splice(
      edit.range.startLine - 1,
      edit.range.endLine - edit.range.startLine + 1,
      ...replacementLines,
    );
    let nextContent = rawLines.join(snapshot.newline);
    if (snapshot.trailingNewline && rawLines.length > 0) {
      nextContent += snapshot.newline;
    }
    const nextSnapshot = this.snapshot(nextContent);
    return Object.freeze({
      editId: edit.editId,
      previousSnapshotId: snapshot.snapshotId,
      nextSnapshot,
      content: nextContent,
      changed: nextContent !== content,
    });
  }

  prepareMany(
    content: string,
    edits: readonly HashlineEdit[],
  ): readonly HashlineEdit[] {
    const snapshot = this.snapshot(content);
    const ranges = edits.map((edit) => {
      if (
        edit.snapshotId !== snapshot.snapshotId ||
        edit.expectedContentDigest !== snapshot.contentDigest
      ) {
        throw new GatewayProtocolError(
          "hashline_snapshot_mismatch",
          "Every edit must bind to the same current snapshot",
        );
      }
      this.assertRange(snapshot, edit.range);
      return edit.range;
    });
    const sorted = [...ranges].sort(
      (left, right) => left.startLine - right.startLine,
    );
    for (let index = 1; index < sorted.length; index += 1) {
      const previous = sorted[index - 1] as HashlineRange;
      const current = sorted[index] as HashlineRange;
      if (current.startLine <= previous.endLine) {
        throw new GatewayProtocolError(
          "hashline_overlap",
          "Hashline edits cannot overlap",
        );
      }
    }
    return Object.freeze([...edits]);
  }

  applyMany(
    content: string,
    edits: readonly HashlineEdit[],
  ): HashlineApplyResult {
    const prepared = this.prepareMany(content, edits);
    if (prepared.length === 0) {
      const snapshot = this.snapshot(content);
      return Object.freeze({
        editId: stableId("hashline-edit-set", []),
        previousSnapshotId: snapshot.snapshotId,
        nextSnapshot: snapshot,
        content,
        changed: false,
      });
    }
    const snapshot = this.snapshot(content);
    const normalized = content.replace(/\r\n/gu, "\n");
    const lines = normalized.length === 0 ? [] : normalized.split("\n");
    if (snapshot.trailingNewline) {
      lines.pop();
    }
    const descending = [...prepared].sort(
      (left, right) => right.range.startLine - left.range.startLine,
    );
    for (const edit of descending) {
      const replacement = edit.replacement.replace(/\r\n/gu, "\n");
      const replacements = replacement.length === 0
        ? []
        : replacement.split("\n");
      if (replacement.endsWith("\n")) {
        replacements.pop();
      }
      lines.splice(
        edit.range.startLine - 1,
        edit.range.endLine - edit.range.startLine + 1,
        ...replacements,
      );
    }
    let next = lines.join(snapshot.newline);
    if (snapshot.trailingNewline && lines.length > 0) {
      next += snapshot.newline;
    }
    return Object.freeze({
      editId: stableId(
        "hashline-edit-set",
        prepared.map((edit) => edit.editId),
      ),
      previousSnapshotId: snapshot.snapshotId,
      nextSnapshot: this.snapshot(next),
      content: next,
      changed: next !== content,
    });
  }

  locate(
    snapshot: HashlineSnapshot,
    hashPrefix: string,
  ): readonly HashlineLine[] {
    const normalized = hashPrefix.trim().toLocaleLowerCase("en-US");
    if (normalized.length < 6) {
      throw new GatewayProtocolError(
        "hashline_prefix_short",
        "Hashline prefix must contain at least six hexadecimal characters",
      );
    }
    return Object.freeze(
      snapshot.lines.filter((line) =>
        line.hash.toLocaleLowerCase("en-US").startsWith(normalized),
      ),
    );
  }

  descriptor(): JsonValue {
    return {
      patcher: "HashlinePatcher",
      ownerSlice: "M1-S05B-01",
      sourceMechanism: "oh-my-pi/packages/hashline",
      maximumLines: this.options.maximumLines,
      maximumBytes: this.options.maximumBytes,
      hashPrefixLength: this.options.hashPrefixLength,
      snapshotBinding: true,
      preflightAll: true,
      directWorkspaceWrite: false,
      descriptorDigest: digestJson({
        maximumLines: this.options.maximumLines,
        maximumBytes: this.options.maximumBytes,
        hashPrefixLength: this.options.hashPrefixLength,
      }),
    };
  }

  private lineHash(text: string): string {
    return contentDigest(text).split(":").at(-1)?.slice(
      0,
      this.options.hashPrefixLength,
    ) as string;
  }

  private assertRange(
    snapshot: HashlineSnapshot,
    range: HashlineRange,
  ): void {
    if (
      range.startLine < 1 ||
      range.endLine < range.startLine ||
      range.endLine > snapshot.lineCount
    ) {
      throw new GatewayProtocolError(
        "hashline_range_invalid",
        "Hashline range is outside the snapshot",
      );
    }
    const start = snapshot.lines[range.startLine - 1];
    const end = snapshot.lines[range.endLine - 1];
    if (
      start?.hash !== range.expectedStartHash ||
      end?.hash !== range.expectedEndHash
    ) {
      throw new GatewayProtocolError(
        "hashline_mismatch",
        "Hashline boundary changed after edit preparation",
      );
    }
  }
}
