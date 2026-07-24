import { describe, expect, test } from "bun:test"
import type { TaskApi } from "../src/api/task-api.ts"
import {
  parseDiffFileContent,
  parseDiffManifest,
  parseDiffPage,
  parsePatchTransactionReceipt,
  parseReviewReceipt,
  transactionCanRetry,
  type DiffFileContract,
  type DiffHunkContract,
  type DiffLineContract,
  type DiffManifestContract,
} from "../src/features/diff-review/contracts.ts"
import {
  applyFileHunks,
  decodeDiffBytes,
  invertFileHunks,
  parseUnifiedDiff,
  UnifiedDiffError,
} from "../src/features/diff-review/unified-diff.ts"
import {
  DiffBudgetError,
  DiffBudgetLedger,
  DiffRequestQueue,
} from "../src/features/diff-review/budget.ts"
import { DiffPageStore } from "../src/features/diff-review/fetch-runtime.ts"
import {
  diffVirtualWindow,
  navigateDiffRows,
  projectDiffRows,
  selectionFromRows,
} from "../src/features/diff-review/virtualization.ts"
import { DiffSearchIndex } from "../src/features/diff-review/search.ts"
import {
  diffChanges,
  resolveMergeConflict,
  threeWayMerge,
} from "../src/features/diff-review/merge.ts"
import {
  contentSha256,
  DiffSnapshotStore,
  preflightHashlinePatch,
} from "../src/features/diff-review/hashline.ts"
import { DiffReviewModel } from "../src/features/diff-review/review-model.ts"
import {
  PatchReceiptJournal,
  PatchTransactionRuntime,
} from "../src/features/diff-review/transactions.ts"


const SHA_A = `sha256:${"a".repeat(64)}`
const SHA_B = `sha256:${"b".repeat(64)}`
const SHA_C = `sha256:${"c".repeat(64)}`
const ARTIFACT_REVISION = `sha256:${"d".repeat(64)}`

const PATCH = [
  "diff --git a/src/example.ts b/src/example.ts",
  "index 1111111..2222222 100644",
  "--- a/src/example.ts",
  "+++ b/src/example.ts",
  "@@ -1,3 +1,4 @@ export function value() {",
  " export function value() {",
  "-  return \"old\"",
  "+  return \"reviewed\"",
  " }",
  "+export const enabled = true",
  "",
].join("\n")

function line(
  id: string,
  kind: DiffLineContract["kind"],
  text: string,
  oldLine: number | undefined,
  newLine: number | undefined,
  patchLine: number,
): DiffLineContract {
  return Object.freeze({
    lineId: id,
    kind,
    text,
    oldLine,
    newLine,
    patchLine,
    byteOffset: patchLine * 16,
    byteLength: new TextEncoder().encode(text).byteLength + 1,
    noNewline: false,
  })
}

function hunk(
  fileId = "diff-file-main",
  index = 0,
): DiffHunkContract {
  return Object.freeze({
    hunkId: `diff-hunk-${index}`,
    fileId,
    index,
    header: "@@ -1,3 +1,4 @@",
    section: "",
    oldStart: 1,
    oldCount: 3,
    newStart: 1,
    newCount: 4,
    additions: 2,
    deletions: 1,
    contextLines: 2,
    patchStart: 100,
    patchEnd: 220,
    lines: Object.freeze([
      line(`line-${index}-1`, "context", "alpha", 1, 1, 6),
      line(`line-${index}-2`, "deleted", "beta", 2, undefined, 7),
      line(`line-${index}-3`, "added", "beta reviewed", undefined, 2, 8),
      line(`line-${index}-4`, "context", "gamma", 3, 3, 9),
      line(`line-${index}-5`, "added", "delta", undefined, 4, 10),
    ]),
  })
}

function fileContract(
  patch: Partial<DiffFileContract> = {},
): DiffFileContract {
  return Object.freeze({
    fileId: "diff-file-main",
    path: "src/example.ts",
    kind: "modified",
    binary: false,
    oversized: false,
    truncated: false,
    encoding: "utf-8",
    lineEnding: "lf",
    additions: 2,
    deletions: 1,
    hunkCount: 1,
    pageCount: 1,
    patchOffset: 0,
    patchBytes: 220,
    oldSize: 17,
    newSize: 32,
    oldSha256: SHA_A,
    newSha256: SHA_B,
    currentSha256: SHA_A,
    oldMtimeNs: 1_000_000,
    currentMtimeNs: 1_000_000,
    oldMode: 0o644,
    currentMode: 0o644,
    language: "typescript",
    mimeType: "text/typescript",
    risk: Object.freeze({
      dirty: false,
      nestedRepository: false,
      untrackedPaths: 0,
      modifiedPaths: 0,
      stagedPaths: 0,
      conflictedPaths: 0,
      repositoryRoot: "",
      reasonCodes: Object.freeze([]),
    }),
    ...patch,
  })
}

function manifestContract(
  files: readonly DiffFileContract[] = [fileContract()],
): DiffManifestContract {
  return Object.freeze({
    schema: "zyra.diff-review-manifest.v1",
    diffId: "diff-main",
    generatedAt: "2026-07-24T08:00:00.000Z",
    source: Object.freeze({
      taskId: "task-diff-web",
      artifactId: "artifact-patch",
      artifactRevision: ARTIFACT_REVISION,
      artifactSha256: ARTIFACT_REVISION,
      runId: "run-diff-web",
      workspaceId: "workspace-diff-web",
      ownerEpoch: 2,
      bindingRevision: 5,
      leaseId: "lease-diff-web",
    }),
    files: Object.freeze([...files]),
    totals: Object.freeze({
      files: files.length,
      hunks: files.reduce((total, item) => total + item.hunkCount, 0),
      lines: 5,
      additions: files.reduce((total, item) => total + item.additions, 0),
      deletions: files.reduce((total, item) => total + item.deletions, 0),
      binaryFiles: files.filter((item) => item.binary).length,
      renamedFiles: files.filter((item) => item.kind === "renamed").length,
      oversizedFiles: files.filter((item) => item.oversized).length,
      patchBytes: files.reduce((total, item) => total + item.patchBytes, 0),
    }),
    maximumPageBytes: 8 * 1024 * 1024,
    maximumPageLines: 100_000,
    physicalPathDisclosed: false,
    readOnly: true,
  })
}

function manifestWire(): Record<string, unknown> {
  const manifest = manifestContract()
  const file = manifest.files[0]!
  return {
    schema: manifest.schema,
    diff_id: manifest.diffId,
    generated_at: manifest.generatedAt,
    source: {
      task_id: manifest.source.taskId,
      artifact_id: manifest.source.artifactId,
      artifact_revision: manifest.source.artifactRevision,
      artifact_sha256: manifest.source.artifactSha256,
      run_id: manifest.source.runId,
      workspace_id: manifest.source.workspaceId,
      owner_epoch: manifest.source.ownerEpoch,
      binding_revision: manifest.source.bindingRevision,
      lease_id: manifest.source.leaseId,
    },
    files: [{
      file_id: file.fileId,
      path: file.path,
      kind: file.kind,
      binary: file.binary,
      oversized: file.oversized,
      truncated: file.truncated,
      encoding: file.encoding,
      line_ending: file.lineEnding,
      additions: file.additions,
      deletions: file.deletions,
      hunk_count: file.hunkCount,
      page_count: file.pageCount,
      patch_offset: file.patchOffset,
      patch_bytes: file.patchBytes,
      old_size: file.oldSize,
      new_size: file.newSize,
      old_sha256: file.oldSha256,
      new_sha256: file.newSha256,
      current_sha256: file.currentSha256,
      old_mtime_ns: file.oldMtimeNs,
      current_mtime_ns: file.currentMtimeNs,
      old_mode: file.oldMode,
      current_mode: file.currentMode,
      language: file.language,
      mime_type: file.mimeType,
      risk: {
        dirty: false,
        nested_repository: false,
        untracked_paths: 0,
        modified_paths: 0,
        staged_paths: 0,
        conflicted_paths: 0,
        repository_root: "",
        reason_codes: [],
      },
    }],
    totals: {
      files: 1,
      hunks: 1,
      lines: 5,
      additions: 2,
      deletions: 1,
      binary_files: 0,
      renamed_files: 0,
      oversized_files: 0,
      patch_bytes: 220,
    },
    maximum_page_bytes: manifest.maximumPageBytes,
    maximum_page_lines: manifest.maximumPageLines,
    physical_path_disclosed: false,
    read_only: true,
  }
}

function hunkWire(value: DiffHunkContract): Record<string, unknown> {
  return {
    hunk_id: value.hunkId,
    file_id: value.fileId,
    index: value.index,
    header: value.header,
    section: value.section,
    old_start: value.oldStart,
    old_count: value.oldCount,
    new_start: value.newStart,
    new_count: value.newCount,
    additions: value.additions,
    deletions: value.deletions,
    context_lines: value.contextLines,
    patch_start: value.patchStart,
    patch_end: value.patchEnd,
    lines: value.lines.map((item) => ({
      line_id: item.lineId,
      kind: item.kind,
      text: item.text,
      old_line: item.oldLine,
      new_line: item.newLine,
      patch_line: item.patchLine,
      byte_offset: item.byteOffset,
      byte_length: item.byteLength,
      no_newline: item.noNewline,
    })),
  }
}

async function digestPage(
  manifest: DiffManifestContract,
  value: DiffHunkContract,
): Promise<string> {
  const canonical = JSON.stringify({
    diff_id: manifest.diffId,
    file_id: value.fileId,
    artifact_revision: manifest.source.artifactRevision,
    page_index: 0,
    page_count: 1,
    hunk_start: 0,
    hunk_end: 1,
    hunks: [{
      hunk_id: value.hunkId,
      index: value.index,
      header: value.header,
      old_start: value.oldStart,
      old_count: value.oldCount,
      new_start: value.newStart,
      new_count: value.newCount,
      lines: value.lines.map((item) => ({
        line_id: item.lineId,
        kind: item.kind,
        text: item.text,
        old_line: item.oldLine ?? null,
        new_line: item.newLine ?? null,
        patch_line: item.patchLine,
        no_newline: item.noNewline,
      })),
    }],
  })
  return contentSha256(canonical)
}

async function pageWire(): Promise<Record<string, unknown>> {
  const manifest = manifestContract()
  const selected = hunk()
  return {
    schema: "zyra.diff-review-page.v1",
    diff_id: manifest.diffId,
    file_id: selected.fileId,
    artifact_revision: manifest.source.artifactRevision,
    page_index: 0,
    page_count: 1,
    cursor: "diff-cursor-page-000",
    next_cursor: null,
    complete: true,
    hunk_start: 0,
    hunk_end: 1,
    utf8_bytes: 128,
    content_digest: await digestPage(manifest, selected),
    hunks: [hunkWire(selected)],
    receipt_id: "diff-page-receipt-000",
    generated_at: "2026-07-24T08:00:01.000Z",
    physical_path_disclosed: false,
  }
}

function permissionWire(effect: "allow" | "ask" | "deny") {
  return {
    canonical_owner: "typescript.PermissionCoordinator",
    effect,
    decision_id: `permission-decision-${effect}`,
    request_id: effect === "ask" ? "permission-request-ask" : null,
    permit_id: null,
    reason_code: `permission.${effect}`,
    policy_revision: 3,
    mode_revision: 2,
    request_fingerprint: `permission-fingerprint-${effect}`,
    pending: effect === "ask",
  }
}

function transactionWire(
  input: {
    causationId: string
    phase?: "committed" | "permission_pending"
    transactionId?: string
    snapshotId?: string
  },
) {
  const phase = input.phase ?? "committed"
  return {
    schema: "zyra.patch-review-transaction-receipt.v1",
    receipt_id: `patch-receipt-${phase}-000`,
    task_id: "task-diff-web",
    run_id: "run-diff-web",
    diff_id: "diff-main",
    transaction_id: input.transactionId ?? "workspace-transaction-000",
    phase,
    accepted: true,
    committed: phase === "committed",
    idempotent_replay: false,
    stale: false,
    conflict: false,
    rolled_back: false,
    rollback_failed: false,
    sealed: false,
    human_intervention_count: 0,
    denied_manual_mutation_count: phase === "permission_pending" ? 1 : 0,
    workspace_id: "workspace-diff-web",
    owner_epoch_before: 2,
    owner_epoch_after: phase === "committed" ? 3 : 2,
    binding_revision_before: 5,
    binding_revision_after: phase === "committed" ? 6 : 5,
    snapshot_id: input.snapshotId ?? (phase === "committed" ? "snapshot-before-patch" : ""),
    recovery_input_id: "",
    reason_code: phase === "committed"
      ? "patch.transaction_committed"
      : "permission.ask",
    message: "transaction projection",
    path_results: phase === "committed" ? [{
      file_id: "diff-file-main",
      path: "src/example.ts",
      disposition: "written",
      before_sha256: SHA_A,
      after_sha256: SHA_B,
      bytes_before: 17,
      bytes_after: 32,
      verified: true,
    }] : [],
    artifact_refs: [],
    event_ids: ["event-diff-patch-000"],
    verification_refs: phase === "committed" ? ["verification-diff-000"] : [],
    terminal_refs: phase === "committed" ? ["terminal-diff-000"] : [],
    timeline_refs: ["timeline-diff-000"],
    permission: permissionWire(phase === "committed" ? "allow" : "ask"),
    causation_id: input.causationId,
    created_at: "2026-07-24T08:00:02.000Z",
  }
}


describe("diff and patch review integration", () => {
  test("strict manifest parser binds files, totals, revision, and redaction", () => {
    const manifest = parseDiffManifest(manifestWire())
    expect(manifest.diffId).toBe("diff-main")
    expect(manifest.files[0]?.path).toBe("src/example.ts")
    expect(manifest.source.workspaceId).toBe("workspace-diff-web")
    expect(manifest.physicalPathDisclosed).toBeFalse()
  })

  test("page store verifies digest and immutable receipt identity", async () => {
    const manifest = parseDiffManifest(manifestWire())
    const page = parseDiffPage(await pageWire(), manifest)
    const store = new DiffPageStore(manifest, 4)
    const generation = store.begin(page.fileId)
    const first = await store.admit(page, generation)
    expect(first.cacheHit).toBeFalse()
    expect(first.state.complete).toBeTrue()
    const replay = await store.admit(page, generation)
    expect(replay.cacheHit).toBeTrue()
  })

  test("file content rejects partial or cross-bound snapshots", () => {
    const manifest = manifestContract()
    const wire = {
      schema: "zyra.diff-review-file-content.v1",
      diff_id: manifest.diffId,
      file_id: "diff-file-main",
      artifact_revision: manifest.source.artifactRevision,
      version: "base",
      text: "alpha\nbeta\ngamma\n",
      sha256: SHA_A,
      mtime_ns: 1_000_000,
      mode: 0o644,
      encoding: "utf-8",
      line_ending: "lf",
      complete: true,
      utf8_bytes: 17,
      receipt_id: "diff-content-receipt-000",
      generated_at: "2026-07-24T08:00:01.000Z",
      physical_path_disclosed: false,
    }
    expect(
      parseDiffFileContent(wire, manifest, "diff-file-main", "base").text,
    ).toContain("beta")
    expect(() =>
      parseDiffFileContent(
        { ...wire, complete: false },
        manifest,
        "diff-file-main",
        "base",
      )).toThrow("complete")
  })

  test("unified parser preserves hunk coordinates and applies the review", () => {
    const parsed = parseUnifiedDiff(PATCH)
    expect(parsed.files).toHaveLength(1)
    expect(parsed.files[0]?.hunks[0]?.oldCount).toBe(3)
    expect(parsed.files[0]?.hunks[0]?.newCount).toBe(4)
    const applied = applyFileHunks(
      "export function value() {\n  return \"old\"\n}\n",
      parsed.files[0]!.hunks,
    )
    expect(applied.text).toContain("return \"reviewed\"")
    expect(applied.text).toContain("enabled = true")
  })

  test("inverted hunks restore the exact reviewed base", () => {
    const parsed = parseUnifiedDiff(PATCH)
    const base = "export function value() {\n  return \"old\"\n}\n"
    const applied = applyFileHunks(base, parsed.files[0]!.hunks)
    const inverted = invertFileHunks(parsed.files[0]!.hunks)
    expect(applyFileHunks(applied.text, inverted).text).toBe(base)
  })

  test("binary and NUL inputs fail closed", () => {
    const decoded = decodeDiffBytes(
      new Uint8Array([0x64, 0x69, 0x66, 0x66, 0, 0, 0, 0]),
    )
    expect(decoded.binary).toBeTrue()
    expect(() =>
      parseUnifiedDiff(new Uint8Array([0x64, 0x69, 0x66, 0x66, 0, 0, 0, 0])),
    ).toThrow(UnifiedDiffError)
  })

  test("budget ledger refuses concurrency and over-reservation", () => {
    const ledger = new DiffBudgetLedger({
      maximumBytes: 65_536,
      maximumLines: 1_000,
      maximumFiles: 2,
      maximumHunks: 10,
      maximumPagesPerFile: 3,
      maximumRequests: 4,
      maximumConcurrentRequests: 1,
      requestTimeoutMs: 1_000,
    })
    const first = ledger.reserve({
      fileId: "diff-file-main",
      pageIndex: 0,
      requestedBytes: 32_768,
      requestedLines: 200,
    })
    expect(() =>
      ledger.reserve({
        fileId: "diff-file-main",
        pageIndex: 1,
        requestedBytes: 32_768,
        requestedLines: 200,
      }),
    ).toThrow(DiffBudgetError)
    ledger.commit({
      reservationId: first.reservationId,
      admittedBytes: 16_384,
      admittedLines: 50,
      admittedHunks: 1,
      cacheHit: false,
    })
    expect(ledger.usage.activeRequests).toBe(0)
    expect(ledger.usage.admittedPages).toBe(1)
  })

  test("request queue cancels a file without spending admitted budget", async () => {
    const ledger = new DiffBudgetLedger({ maximumConcurrentRequests: 2 })
    const queue = new DiffRequestQueue(ledger)
    queue.enqueue({
      fileId: "diff-file-main",
      pageIndex: 0,
      priority: 1,
      reason: "test",
    })
    expect(queue.cancelFile("diff-file-main", "superseded")).toBe(1)
    expect(ledger.usage.admittedPages).toBe(0)
  })

  test("virtualized unified and split projections retain stable line identity", () => {
    const file = fileContract()
    const unified = projectDiffRows(file, [hunk()], {
      layout: "unified",
      viewMode: "diff",
    })
    const split = projectDiffRows(file, [hunk()], {
      layout: "split",
      viewMode: "diff",
    })
    expect(unified.lineToRow.get("line-0-3")).toBeTruthy()
    expect(split.lineToRow.get("line-0-3")).toBeTruthy()
    const window = diffVirtualWindow(unified, {
      scrollTop: 0,
      viewportHeight: 24,
      overscanPixels: 0,
    })
    expect(window.rows.length).toBeLessThan(unified.rows.length)
    expect(window.totalHeight).toBeGreaterThan(24)
  })

  test("100k-line review only materializes the viewport window", () => {
    const lines = Array.from({ length: 100_200 }, (_, index) =>
      line(
        `large-line-${index}`,
        index % 2 === 0 ? "context" : "added",
        `line ${index}`,
        index % 2 === 0 ? Math.floor(index / 2) + 1 : undefined,
        index + 1,
        index + 1,
      ))
    const largeHunk: DiffHunkContract = Object.freeze({
      ...hunk("diff-file-large"),
      hunkId: "diff-hunk-large",
      fileId: "diff-file-large",
      oldStart: 1,
      oldCount: 50_100,
      newStart: 1,
      newCount: lines.length,
      additions: 50_100,
      deletions: 0,
      contextLines: 50_100,
      lines: Object.freeze(lines),
    })
    const largeFile = fileContract({
      fileId: "diff-file-large",
      path: "generated/large.log",
      additions: 0,
      deletions: 0,
      oversized: true,
    })
    const projected = projectDiffRows(largeFile, [largeHunk], {
      layout: "unified",
      viewMode: "diff",
      contextCollapseThreshold: 10_000,
    })
    const window = diffVirtualWindow(projected, {
      scrollTop: 1_200_000,
      viewportHeight: 600,
      overscanPixels: 300,
    })
    expect(projected.rows.length).toBe(100_202)
    expect(window.rows.length).toBeLessThan(80)
    expect(window.totalHeight).toBeGreaterThan(2_000_000)
  })

  test("keyboard navigation and range selection are deterministic", () => {
    const projected = projectDiffRows(fileContract(), [hunk()], {
      layout: "unified",
      viewMode: "diff",
    })
    const next = navigateDiffRows(
      projected,
      projected.focusableRowIds[0],
      "next",
      { viewportHeight: 100 },
    )
    expect(next.changed).toBeTrue()
    const selected = selectionFromRows(
      "diff-file-main",
      hunk(),
      "line-0-1",
      "line-0-2",
      "old",
    )
    expect(selected?.anchorLineIds).toEqual(["line-0-1", "line-0-2"])
  })

  test("trigram search finds changed content and reports incomplete files", async () => {
    const index = new DiffSearchIndex()
    index.add({
      file: fileContract(),
      hunks: [hunk()],
      complete: false,
      loadedBytes: 256,
    })
    const result = await index.search({
      query: "reviewed",
      caseSensitive: false,
      wholeWord: false,
      useRegExp: false,
      maximumResults: 100,
    })
    expect(result.matches[0]?.lineId).toBe("line-0-3")
    expect(result.incompleteFiles).toEqual(["diff-file-main"])
  })

  test("three-way merge accepts disjoint changes and reports overlap", () => {
    const base = "alpha\nbeta\ngamma\n"
    const clean = threeWayMerge(
      base,
      "alpha current\nbeta\ngamma\n",
      "alpha\nbeta\ngamma proposed\n",
    )
    expect(clean.state).toBe("clean")
    expect(clean.text).toContain("alpha current")
    expect(clean.text).toContain("gamma proposed")
    const conflict = threeWayMerge(
      base,
      "alpha\ncurrent\ngamma\n",
      "alpha\nproposed\ngamma\n",
    )
    expect(conflict.state).toBe("conflicted")
    const resolved = resolveMergeConflict(
      conflict,
      conflict.conflicts[0]!.conflictId,
      "proposed",
    )
    expect(resolved.state).toBe("clean")
    expect(resolved.text).toContain("proposed")
  })

  test("line diff carries deterministic read/write ranges", () => {
    const changes = diffChanges(
      ["alpha", "beta", "gamma"],
      ["alpha", "reviewed", "gamma", "delta"],
      "proposed",
      100_000,
    )
    expect(changes.length).toBeGreaterThan(0)
    expect(changes[0]?.changeId.startsWith("merge-change:")).toBeTrue()
  })

  test("hashline preflight accepts observed snapshot and rejects stale without merge", async () => {
    const base = "alpha\nbeta\ngamma\n"
    const store = new DiffSnapshotStore({ maximumBytes: 128 * 1024 })
    const snapshot = await store.record({
      fileId: "diff-file-main",
      path: "src/example.ts",
      text: base,
      mtimeNs: 1_000_000,
      mode: 0o644,
      encoding: "utf-8",
      lineEnding: "lf",
      observedLineIds: hunk().lines.map((item) => item.lineId),
    })
    const clean = await preflightHashlinePatch({
      file: fileContract({ oldSha256: snapshot.sha256 }),
      hunks: [hunk()],
      baseSnapshot: snapshot,
      current: {
        text: base,
        sha256: snapshot.sha256,
        mtimeNs: 1_000_000,
        mode: 0o644,
      },
      allowThreeWay: true,
    })
    expect(clean.accepted).toBeTrue()
    expect(clean.precondition?.proposedSha256).toBe(
      await contentSha256("alpha\nbeta reviewed\ngamma\ndelta\n"),
    )
    const stale = await preflightHashlinePatch({
      file: fileContract({ oldSha256: snapshot.sha256 }),
      hunks: [hunk()],
      baseSnapshot: snapshot,
      current: {
        text: base,
        sha256: SHA_C,
        mtimeNs: 1_000_001,
        mode: 0o644,
      },
      allowThreeWay: false,
    })
    expect(stale.state).toBe("stale")
  })

  test("review model filters, selects, comments, and blocks binary apply", () => {
    const binary = fileContract({
      fileId: "diff-file-binary",
      path: "assets/image.png",
      kind: "binary",
      binary: true,
      hunkCount: 0,
      pageCount: 0,
      additions: 0,
      deletions: 0,
    })
    const model = new DiffReviewModel({
      taskId: "task-diff-web",
      diffId: "diff-main",
      files: [fileContract(), binary],
      persistence: {
        getItem: () => null,
        setItem: () => undefined,
        removeItem: () => undefined,
      },
    })
    expect(model.setFilter("example").filteredFiles).toHaveLength(1)
    expect(model.state.selectedFileIds).toEqual(["diff-file-main"])
    expect(() => model.toggleFileSelection("diff-file-binary")).toThrow("Binary")
  })

  test("transaction receipt parser enforces sealed zero-intervention semantics", () => {
    const parsed = parsePatchTransactionReceipt(
      transactionWire({ causationId: "cause-transaction-000" }),
      {
        taskId: "task-diff-web",
        runId: "run-diff-web",
        diffId: "diff-main",
        causationId: "cause-transaction-000",
      },
    )
    expect(parsed.committed).toBeTrue()
    expect(parsed.humanInterventionCount).toBe(0)
    expect(parsed.permission.canonicalOwner).toBe(
      "typescript.PermissionCoordinator",
    )
  })

  test("review receipt parser preserves permission-gated sealed rejection", () => {
    const receipt = parseReviewReceipt({
      schema: "zyra.diff-review-receipt.v1",
      receipt_id: "review-receipt-sealed",
      diff_id: "diff-review-fixture",
      task_id: "task-review-fixture",
      action: "comment",
      accepted: false,
      causation_id: "review-cause-sealed",
      event_ids: ["event-review-sealed"],
      comment: null,
      selection: null,
      revision: 0,
      permission: {
        ...permissionWire("deny"),
        reason_code: "sealed.manual_review_mutation_denied",
      },
      human_intervention_count: 0,
      denied_manual_mutation_count: 1,
      created_at: "2026-07-24T00:00:00Z",
    })
    expect(receipt.accepted).toBeFalse()
    expect(receipt.revision).toBe(0)
    expect(receipt.permission.canonicalOwner).toBe(
      "typescript.PermissionCoordinator",
    )
    expect(receipt.permission.effect).toBe("deny")
    expect(receipt.deniedManualMutationCount).toBe(1)
  })

  test("receipt journal rejects identity reuse and recognizes permission retry", () => {
    const pending = parsePatchTransactionReceipt(
      transactionWire({
        causationId: "cause-pending-000",
        phase: "permission_pending",
      }),
      {
        taskId: "task-diff-web",
        runId: "run-diff-web",
        diffId: "diff-main",
        causationId: "cause-pending-000",
      },
    )
    expect(transactionCanRetry(pending)).toBeTrue()
    const journal = new PatchReceiptJournal()
    expect(journal.admit(pending, "idempotency-pending-000").replay).toBeFalse()
    expect(journal.admit(pending, "idempotency-pending-000").replay).toBeTrue()
  })

  test("transaction runtime submits exact preconditions and records committed receipt", async () => {
    const manifest = manifestContract()
    let submitted: Record<string, unknown> | undefined
    const fake = {
      diffReviewApply: async (
        _taskId: string,
        _artifactId: string,
        body: unknown,
      ) => {
        submitted = body as Record<string, unknown>
        return transactionWire({
          causationId: String(submitted.causationId),
        })
      },
    } as unknown as TaskApi
    const runtime = new PatchTransactionRuntime({
      api: fake,
      taskId: manifest.source.taskId,
      runId: manifest.source.runId,
      diffId: manifest.diffId,
    })
    const base = "alpha\nbeta\ngamma\n"
    const baseSha = await contentSha256(base)
    const proposedSha = await contentSha256(
      "alpha\nbeta reviewed\ngamma\ndelta\n",
    )
    const receipt = await runtime.apply({
      manifest: Object.freeze({
        ...manifest,
        files: Object.freeze([
          fileContract({
            oldSha256: baseSha,
            currentSha256: baseSha,
            newSha256: proposedSha,
          }),
        ]),
      }),
      selectedFileIds: ["diff-file-main"],
      preflightReceipts: [Object.freeze({
        receiptId: "hashline-preflight-000",
        fileId: "diff-file-main",
        path: "src/example.ts",
        state: "clean",
        accepted: true,
        baseSha256: baseSha,
        currentSha256: baseSha,
        proposedSha256: proposedSha,
        baseMtimeNs: 1_000_000,
        currentMtimeNs: 1_000_000,
        baseMode: 0o644,
        currentMode: 0o644,
        precondition: Object.freeze({
          fileId: "diff-file-main",
          path: "src/example.ts",
          kind: "modified",
          baseSha256: baseSha,
          currentSha256: baseSha,
          proposedSha256: proposedSha,
          baseMtimeNs: 1_000_000,
          currentMtimeNs: 1_000_000,
          baseMode: 0o644,
          currentMode: 0o644,
          encoding: "utf-8",
          lineEnding: "lf",
          binary: false,
        }),
        previews: Object.freeze([]),
        reasonCode: "hashline.preflight_clean",
        message: "clean",
        createdAt: Date.now(),
      })],
      reviewRevision: 0,
      identity: {
        actorId: "reviewer",
        sessionId: "session-diff-web",
        sessionRevision: 0,
        workerRequestId: "worker-request-diff-web",
        sealed: false,
      },
      causationId: "cause-runtime-transaction",
      idempotencyKey: "idempotency-runtime-transaction",
    })
    expect(submitted?.schema).toBe("zyra.patch-review-apply.v1")
    expect(
      (submitted?.preconditions as readonly Record<string, unknown>[])[0]
        ?.currentSha256,
    ).toBe(baseSha)
    expect(receipt.phase).toBe("committed")
    expect(runtime.state.phase).toBe("committed")
  })
})
