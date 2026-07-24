import { spawnSync } from "node:child_process"
import { resolve } from "node:path"

const ROOT = resolve(import.meta.dirname, "..")
const BASELINE =
  process.argv
    .find((value) => value.startsWith("--baseline="))
    ?.slice("--baseline=".length) ||
  "1833319acdb8a09fac3438b12356da0e9c78d6bb"
const TARGET =
  process.argv
    .find((value) => value.startsWith("--target="))
    ?.slice("--target=".length) ||
  "9ca93e3"
const MINIMUM = Number(
  process.argv
    .find((value) => value.startsWith("--minimum="))
    ?.slice("--minimum=".length) || 7_500,
)

function sourceDecision(path) {
  if (
    path.endsWith("/security.ts") ||
    path.endsWith("/viewers.ts") ||
    path.endsWith("/view/artifact-workbench.tsx")
  ) {
    return {
      source_role: "supplementary_implementation",
      source_repo: "OpenHands",
      migration_mode:
        "cropped_migration/same_language_component_integration",
    }
  }
  if (
    path.startsWith("apps/web/src/features/artifacts/cache.ts") ||
    path.startsWith("apps/web/src/features/artifacts/catalog.ts") ||
    path.startsWith("apps/web/src/features/artifacts/content.ts") ||
    path.startsWith("apps/web/src/features/artifacts/runtime.ts") ||
    path.startsWith("apps/web/src/features/artifacts/search-index.ts")
  ) {
    return {
      source_role: "primary_implementation",
      source_repo: "opencode",
      migration_mode:
        "cropped_migration/same_language_component_integration",
    }
  }
  if (
    path.startsWith("apps/web/src/features/artifacts/") ||
    path.startsWith("apps/web/src/api/") ||
    path.startsWith("apps/web/src/components/") ||
    path.startsWith("apps/web/src/features/timeline/") ||
    path.startsWith("apps/web/src/features/topology/")
  ) {
    return {
      source_role: "zyra_owner_integration",
      source_repo: "zyra",
      migration_mode: "same_language_component_integration",
    }
  }
  if (
    path.startsWith("apps/api/") ||
    path.startsWith("packages/runtime/")
  ) {
    return {
      source_role: "supplementary_implementation",
      source_repo: "OpenHands",
      migration_mode: "cropped_migration/canonical_owner_integration",
    }
  }
  if (path.startsWith("packages/core/")) {
    return {
      source_role: "zyra_owner_integration",
      source_repo: "zyra",
      migration_mode: "typed_protocol_integration",
    }
  }
  if (path.startsWith("tests/") || path.includes("/test/")) {
    return {
      source_role: "behavior_evidence",
      source_repo: "zyra",
      migration_mode: "test_or_conformance",
    }
  }
  return {
    source_role: "evidence_tooling",
    source_repo: "zyra",
    migration_mode: "audit_or_documentation",
  }
}

const child = spawnSync(
  resolve(ROOT, "node_modules", ".bin", "bun.exe"),
  [
    resolve(ROOT, "scripts", "audit_m2_s02b_01_effective_lines.mjs"),
    `--baseline=${BASELINE}`,
    `--target=${TARGET}`,
    `--minimum=${MINIMUM}`,
  ],
  {
    cwd: ROOT,
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
  },
)

if (!child.stdout.trim()) {
  process.stderr.write(child.stderr)
  process.exit(child.status ?? 1)
}

const result = JSON.parse(child.stdout)
result.audit_scope = "M2-S03A-01"
result.baseline_commit = BASELINE
result.implementation_commit = TARGET
result.interval = `${BASELINE}..${TARGET}`
result.methodology =
  "Exact Git added-line sets using the frozen TypeScript compiler AST and scanner classifier. Imports, interfaces, type aliases, export-only declarations, declaration-only signatures, top-level artifact contract constants, JSX presentation, CSS/static presentation, comments, blanks, Python custody/API integration, tests, fixtures, probes, docs, audit tooling, data, generated material, adapters and vendor/source-pool content receive zero TypeScript/React production credit. Each row is rebound to the M2-S03A-01 preimplementation source role and migration mode."
result.files = result.files.map((file) => ({
  ...file,
  ...sourceDecision(file.path),
}))

if (process.argv.includes("--summary")) {
  const totals = result.totals
  process.stdout.write(
    [
      `scope=${result.audit_scope}`,
      `interval=${result.interval}`,
      `raw_additions=${totals.raw_additions}`,
      `raw_deletions=${totals.raw_deletions}`,
      `production_runtime=${totals.production_runtime}`,
      `ui_behavior=${totals.ui_behavior}`,
      `ui_presentation=${totals.ui_presentation}`,
      `type_declaration=${totals.type_declaration}`,
      `schema_dto_data=${totals.schema_dto_data}`,
      `adapter_only=${totals.adapter_only}`,
      `generated=${totals.generated}`,
      `test_mock_fixture=${totals.test_mock_fixture}`,
      `docs_comments_blank=${totals.docs_comments_blank}`,
      `vendor_like_source_pool=${totals.vendor_like_source_pool}`,
      `effective_production=${totals.effective_production}`,
      `minimum=${MINIMUM}`,
      `line_count_ok=${result.line_count_ok}`,
    ].join("\n") + "\n",
  )
} else {
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`)
}

if (!result.line_count_ok) process.exitCode = 1
