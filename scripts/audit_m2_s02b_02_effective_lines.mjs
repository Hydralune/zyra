import { spawnSync } from "node:child_process"
import { resolve } from "node:path"

const ROOT = resolve(import.meta.dirname, "..")
const BASELINE =
  process.argv
    .find((value) => value.startsWith("--baseline="))
    ?.slice("--baseline=".length) ||
  "46be71eb4ced2da38f5b05fce24907da7ffc8f6c"
const TARGET =
  process.argv
    .find((value) => value.startsWith("--target="))
    ?.slice("--target=".length) ||
  "WORKTREE"
const MINIMUM = Number(
  process.argv
    .find((value) => value.startsWith("--minimum="))
    ?.slice("--minimum=".length) || 6_000,
)

function sourceDecision(path) {
  if (
    path.endsWith("/control/ledger.ts") ||
    path.endsWith("/control/runtime.ts") ||
    path.endsWith("/view/recovery-control-panel.tsx")
  ) {
    return {
      source_role: "supplementary_implementation",
      source_repo: "OpenHands",
      migration_mode: "cropped_migration",
    }
  }
  if (
    path.endsWith("/control/receipts.ts") ||
    path.endsWith("/control/sealed.ts") ||
    path.endsWith("/scale/overlays.ts")
  ) {
    return {
      source_role: "supplementary_implementation",
      source_repo: "browser-use",
      migration_mode: "semantic_port/production",
    }
  }
  if (
    path.startsWith("apps/web/src/features/timeline/control/") ||
    path.startsWith("apps/web/src/features/timeline/scale/") ||
    path.startsWith("apps/web/src/features/timeline/view/")
  ) {
    return {
      source_role: "primary_implementation",
      source_repo: "opencode",
      migration_mode:
        "cropped_migration/same_language_component_integration",
    }
  }
  if (
    path.startsWith("apps/api/") ||
    path.startsWith("packages/commands/")
  ) {
    return {
      source_role: "zyra_owner_integration",
      source_repo: "zyra",
      migration_mode: "canonical_owner_integration",
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
result.audit_scope = "M2-S02B-02"
result.baseline_commit = BASELINE
result.implementation_commit = TARGET
result.interval = `${BASELINE}..${TARGET}`
result.methodology =
  "Exact Git added-line sets using the frozen M2-S02B-01 TypeScript compiler AST and scanner classifier. Imports, interfaces, type aliases, export-only declarations, declaration-only signatures, contracts schema constants, JSX presentation, CSS/static presentation, comments, blanks, Python owner integration, tests, fixtures, probes, docs, audit tooling, data, generated material, adapters and vendor/source-pool content receive zero production credit. Each row is rebound to the M2-S02B-02 preimplementation source role and migration mode."
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
