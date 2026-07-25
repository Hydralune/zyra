import { execFileSync, spawnSync } from "node:child_process"
import { resolve } from "node:path"
import { writeFileSync } from "node:fs"

const ROOT = resolve(import.meta.dirname, "..")
const BASELINE =
  process.argv.find((value) => value.startsWith("--baseline="))
    ?.slice("--baseline=".length)
  || "b3387c626fc587936c24777ce108eac6f962283e"
const TARGET =
  process.argv.find((value) => value.startsWith("--target="))
    ?.slice("--target=".length)
  || "WORKTREE"
const MINIMUM = Number(
  process.argv.find((value) => value.startsWith("--minimum="))
    ?.slice("--minimum=".length)
  || 6_500
)
const OUTPUT =
  process.argv.find((value) => value.startsWith("--output="))
    ?.slice("--output=".length)

function git(...args) {
  return execFileSync("git", args, { cwd: ROOT, encoding: "utf8" })
}

function implementationText(path) {
  return TARGET === "WORKTREE"
    ? execFileSync(
      "powershell",
      [
        "-NoProfile",
        "-Command",
        `Get-Content -LiteralPath '${path.replaceAll("'", "''")}' -Raw`,
      ],
      { cwd: ROOT, encoding: "utf8" },
    )
    : git("show", `${TARGET}:${path}`)
}

function addedLines(path) {
  const arguments_ = TARGET === "WORKTREE"
    ? ["diff", "--unified=0", BASELINE, "--", path]
    : ["diff", "--unified=0", BASELINE, TARGET, "--", path]
  const patch = git(...arguments_)
  const output = new Set()
  let lineNumber = 0
  for (const line of patch.split(/\r?\n/)) {
    const header = /^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@/.exec(line)
    if (header) {
      lineNumber = Number(header[1])
      continue
    }
    if (!lineNumber || line.startsWith("---") || line.startsWith("+++")) continue
    if (line.startsWith("+")) {
      output.add(lineNumber)
      lineNumber += 1
    } else if (!line.startsWith("-")) {
      lineNumber += 1
    }
  }
  return output
}

function classifyPython(entry) {
  const script = [
    "import ast,json,subprocess,pathlib",
    `target=${JSON.stringify(TARGET)}`,
    `path=${JSON.stringify(entry.path)}`,
    "text=pathlib.Path(path).read_text(encoding='utf-8') if target=='WORKTREE' else subprocess.check_output(['git','show',f'{target}:{path}'],text=True,encoding='utf-8')",
    "tree=ast.parse(text)",
    "types=set(); schema=set(); docs=set()",
    "def mark(target,node): target.update(range(node.lineno,getattr(node,'end_lineno',node.lineno)+1))",
    "for node in ast.walk(tree):",
    "  if isinstance(node,(ast.Import,ast.ImportFrom)): mark(types,node)",
    "  if isinstance(node,(ast.Module,ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef)) and node.body:",
    "    first=node.body[0]",
    "    if isinstance(first,ast.Expr) and isinstance(first.value,ast.Constant) and isinstance(first.value.value,str): mark(docs,first)",
    "for parent in ast.walk(tree):",
    "  if isinstance(parent,ast.ClassDef):",
    "    for node in parent.body:",
    "      if isinstance(node,ast.AnnAssign): mark(schema,node)",
    "for node in tree.body:",
    "  targets=[]",
    "  if isinstance(node,ast.Assign): targets=[target.id for target in node.targets if isinstance(target,ast.Name)]",
    "  elif isinstance(node,ast.AnnAssign) and isinstance(node.target,ast.Name): targets=[node.target.id]",
    "  if targets and all(name.isupper() for name in targets): mark(schema,node)",
    "print(json.dumps({'type':sorted(types),'schema':sorted(schema),'docs':sorted(docs)}))",
  ].join("\n")
  const ranges = JSON.parse(
    execFileSync("python", ["-c", script], { cwd: ROOT, encoding: "utf8" }),
  )
  const types = new Set(ranges.type)
  const schema = new Set(ranges.schema)
  const docs = new Set(ranges.docs)
  const lines = implementationText(entry.path).split(/\r?\n/)
  const result = {
    ...entry,
    production_runtime: 0,
    ui_behavior: 0,
    ui_presentation: 0,
    type_declaration: 0,
    schema_dto_data: 0,
    adapter_only: 0,
    generated: 0,
    test_mock_fixture: 0,
    docs_comments_blank: 0,
    vendor_like_source_pool: 0,
    effective_production: 0,
  }
  for (const line of addedLines(entry.path)) {
    const value = lines[line - 1]?.trim() ?? ""
    if (types.has(line)) result.type_declaration += 1
    else if (schema.has(line)) result.schema_dto_data += 1
    else if (docs.has(line) || value === "" || value.startsWith("#")) {
      result.docs_comments_blank += 1
    } else {
      result.production_runtime += 1
    }
  }
  result.effective_production = result.production_runtime
  return result
}

function file(result, path) {
  return result.files.find((entry) => entry.path === path)
}

function move(result, path, from, to, requested = "all") {
  const selected = file(result, path)
  if (!selected) return
  const count = requested === "all"
    ? Number(selected[from] || 0)
    : Math.min(Number(requested), Number(selected[from] || 0))
  selected[from] -= count
  selected[to] += count
  selected.effective_production =
    selected.production_runtime + selected.ui_behavior
}

function excludeExecutable(result, path, bucket) {
  move(result, path, "production_runtime", bucket)
  move(result, path, "ui_behavior", bucket)
}

const child = spawnSync(
  resolve(ROOT, "node_modules", ".bin", "bun.exe"),
  [
    resolve(ROOT, "scripts", "audit_m2_s02b_01_effective_lines.mjs"),
    `--baseline=${BASELINE}`,
    `--target=${TARGET}`,
    "--minimum=0",
  ],
  {
    cwd: ROOT,
    encoding: "utf8",
    maxBuffer: 128 * 1024 * 1024,
  },
)
if (child.status !== 0 || !child.stdout.trim()) {
  process.stderr.write(child.stderr)
  process.exit(child.status ?? 1)
}
const result = JSON.parse(child.stdout)
result.files = result.files.map((entry) =>
  entry.path.endsWith(".py")
  && !entry.path.startsWith("tests/")
    ? classifyPython(entry)
    : entry
)

// API transport, typed wire declarations and package exports are composition,
// not experiment/evidence domain behavior.
for (const path of [
  "apps/api/zyra_api/experiment_api.py",
  "apps/api/zyra_api/main.py",
  "apps/web/src/api/experiment-api.ts",
  "apps/web/src/api/index.ts",
  "packages/core/typed-api-client/src/constants.ts",
  "packages/core/typed-api-client/src/normalizers.ts",
  "packages/core/typed-api-client/src/protocol.ts",
  "packages/evaluation/zyra_evaluation/__init__.py",
  "packages/evaluation/zyra_evaluation/experiment_runtime/__init__.py",
]) {
  excludeExecutable(result, path, "adapter_only")
}

// Formal-run, ledger and audit tools are evidence tooling and receive no
// production credit even though they are executable and productized.
for (const path of [
  "scripts/run_m2_s05_03_experiments.py",
  "scripts/sync_m2_experiment_evidence_source_ledger.py",
  "scripts/audit_m2_s05_03_effective_lines.mjs",
]) {
  excludeExecutable(result, path, "adapter_only")
}

function sourceDecision(entry) {
  const path = entry.path
  const language = path.endsWith(".py")
    ? "python"
    : path.endsWith(".tsx")
      ? "typescript/tsx"
      : "typescript"
  if (path.startsWith("tests/") || path.startsWith("apps/web/test/")) {
    return {
      source_role: "behavior_evidence",
      source_repo: "zyra",
      source_language: language,
      target_language: language,
      migration_mode: "test_or_conformance",
    }
  }
  if (path.startsWith("scripts/")) {
    return {
      source_role: "evidence_tooling",
      source_repo: "zyra",
      source_language: language,
      target_language: language,
      migration_mode: "scenario_and_evidence_integration_only",
    }
  }
  if (
    path.startsWith(
      "packages/evaluation/zyra_evaluation/experiment_runtime/",
    )
    || path.startsWith("apps/web/src/features/experiments/")
  ) {
    return {
      source_role: "primary_implementation",
      source_repo: "zyra",
      source_language: language,
      target_language: language,
      migration_mode: "scenario_and_evidence_integration_only",
    }
  }
  return {
    source_role: "existing_owner_integration",
    source_repo: "zyra",
    source_language: language,
    target_language: language,
    migration_mode: "composition_or_protocol_adapter",
  }
}

const numericKeys = [
  "raw_additions",
  "raw_deletions",
  "production_runtime",
  "ui_behavior",
  "ui_presentation",
  "type_declaration",
  "schema_dto_data",
  "adapter_only",
  "generated",
  "test_mock_fixture",
  "docs_comments_blank",
  "vendor_like_source_pool",
  "effective_production",
]
result.audit_scope = "M2-S05-03"
result.baseline_commit = BASELINE
result.implementation_commit = TARGET
result.interval = `${BASELINE}..${TARGET}`
result.methodology =
  "Exact Git added-line sets between frozen commits. Python AST excludes imports, dataclass/schema fields, top-level constants, docstrings, comments and blanks. TypeScript compiler AST separates declarations, JSX/static presentation and executable UI behavior. Tests, docs, generated/data, typed wire/API composition, package exports, formal-run/ledger/audit scripts and vendor/source-pool content receive zero production credit. Effective production is the durable experiment/workload/metric/statistics/requirement/report/bundle/portfolio runtime plus non-JSX experiment workbench behavior."
result.files = result.files.map((entry) => ({
  ...entry,
  ...sourceDecision(entry),
}))
result.totals = Object.fromEntries(
  numericKeys.map((key) => [
    key,
    result.files.reduce(
      (total, entry) => total + Number(entry[key] || 0),
      0,
    ),
  ]),
)
result.minimum_effective_production = MINIMUM
result.line_count_ok = result.totals.effective_production >= MINIMUM
result.large_file_triggers = result.files
  .filter((entry) =>
    entry.raw_additions > 500
    || entry.effective_production
      > result.totals.effective_production * 0.2
    || entry.type_declaration > entry.raw_additions * 0.3
    || entry.schema_dto_data > entry.raw_additions * 0.3
    || entry.adapter_only > entry.raw_additions * 0.3
    || entry.test_mock_fixture > entry.raw_additions * 0.3
    || entry.ui_presentation > entry.raw_additions * 0.3
  )
  .map((entry) => entry.path)

const serialized = `${JSON.stringify(result, null, 2)}\n`
if (OUTPUT) writeFileSync(resolve(ROOT, OUTPUT), serialized)
if (process.argv.includes("--summary")) {
  process.stdout.write(
    [
      `scope=${result.audit_scope}`,
      `interval=${result.interval}`,
      ...numericKeys.map((key) => `${key}=${result.totals[key]}`),
      `minimum=${MINIMUM}`,
      `line_count_ok=${result.line_count_ok}`,
      `large_file_triggers=${result.large_file_triggers.join(",")}`,
    ].join("\n") + "\n",
  )
} else {
  process.stdout.write(serialized)
}
if (!result.line_count_ok) process.exitCode = 1
