import { mkdtemp, mkdir, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join, resolve } from "node:path"
import { workspaceReferenceCandidates } from "../../apps/cli/src/product/files/index.ts"
import { completionState } from "../../apps/cli/src/tui/overlay/completion.ts"

const DIRECTORY_COUNT = 200
const FILES_PER_DIRECTORY = 100
const ENTRY_LIMIT = 20_000
const MAXIMUM_INDEX_MS = 30_000
const QUERY_COUNT = 1_000
const MAXIMUM_QUERY_P95_MS = 50
const MAXIMUM_RSS_DELTA_BYTES = 256 * 1024 * 1024

const root = await mkdtemp(join(tmpdir(), "zyra-file-reference-gate-"))
const canonicalRoot = resolve(root)
const baselineRss = process.memoryUsage.rss()

try {
  const directories = Array.from({ length: DIRECTORY_COUNT }, (_, directoryIndex) => (
    join(canonicalRoot, `模块 ${String(directoryIndex).padStart(3, "0")}`)
  ))
  await Promise.all(directories.map((directory) => mkdir(directory)))
  const targets = directories.flatMap((directory) => (
    Array.from({ length: FILES_PER_DIRECTORY }, (_, fileIndex) => (
      join(directory, `文件-${String(fileIndex).padStart(3, "0")}.ts`)
    ))
  ))
  for (let offset = 0; offset < targets.length; offset += 1_000) {
    await Promise.all(targets.slice(offset, offset + 1_000).map((target) => Bun.write(target, "")))
  }

  const indexStarted = performance.now()
  const candidates = await workspaceReferenceCandidates(canonicalRoot, ENTRY_LIMIT)
  const indexMs = performance.now() - indexStarted
  if (candidates.length !== ENTRY_LIMIT) throw new Error(`expected ${ENTRY_LIMIT} bounded entries, received ${candidates.length}`)
  if (!candidates.some((candidate) => candidate.includes("模块 000/文件-000.ts"))) {
    throw new Error("Unicode/space reference is missing from the real index")
  }

  const queryDurations: number[] = []
  for (let iteration = 0; iteration < QUERY_COUNT; iteration += 1) {
    const query = iteration % 2 === 0 ? "@模块" : "@文件"
    const draft = { text: query, cursor: query.length, display: query, pasteRefs: [] }
    const started = performance.now()
    const completion = completionState(draft, candidates)
    queryDurations.push(performance.now() - started)
    if (!completion?.matches.length) throw new Error(`query ${query} returned no match`)
  }
  queryDurations.sort((left, right) => left - right)
  const queryP95Ms = queryDurations[Math.ceil(queryDurations.length * 0.95) - 1] ?? Number.POSITIVE_INFINITY
  const rssDeltaBytes = Math.max(0, process.memoryUsage.rss() - baselineRss)
  if (indexMs > MAXIMUM_INDEX_MS) throw new Error(`index exceeded ${MAXIMUM_INDEX_MS}ms: ${indexMs.toFixed(3)}ms`)
  if (queryP95Ms > MAXIMUM_QUERY_P95_MS) throw new Error(`query P95 exceeded ${MAXIMUM_QUERY_P95_MS}ms: ${queryP95Ms.toFixed(3)}ms`)
  if (rssDeltaBytes > MAXIMUM_RSS_DELTA_BYTES) throw new Error(`RSS delta exceeded ${MAXIMUM_RSS_DELTA_BYTES} bytes: ${rssDeltaBytes}`)

  process.stdout.write(`${JSON.stringify({
    schema: "zyra.product-tui-file-reference-gate/v1",
    entry_count: candidates.length,
    query_count: QUERY_COUNT,
    index_ms: Number(indexMs.toFixed(3)),
    query_p95_ms: Number(queryP95Ms.toFixed(3)),
    rss_delta_bytes: rssDeltaBytes,
    all_passed: true,
  })}\n`)
} finally {
  await rm(canonicalRoot, { recursive: true, force: true })
}
