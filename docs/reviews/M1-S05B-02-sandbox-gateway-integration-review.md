# M1-S05B-02 Sandbox Gateway Integration Review

## 1. Review identity

- Slice: `M1-S05B-02`
- Base evidence commit: `7880aaa3e4e7207585735d3298a10c177fccc2e8`
- Review type: incremental critical self-review and adversarial integration audit
- Result: PASS for the slice scope
- Canonical gateway owner: `SandboxGatewayRuntime`
- Canonical permission owner: `ToolPermissionRuntime`
- Canonical managed-workspace owner: `WorkspaceManagerRuntime`

This review covers only the implementation and tests introduced or directly affected by
M1-S05B-02. It does not treat source ledgers, manifests, descriptors, or static imports as
behavioral completion evidence.

## 2. Implemented production boundary

The slice connects the M1-S05B-01 sandbox gateway foundation to the real Zyra execution
surfaces rather than creating a second runtime:

- `zyra_runtime.sandbox_gateway.integration_factory` constructs one runtime bundle and
  installs the CodeWorker, Browser, MCP, event, receipt, and control ports.
- `zyra_runtime.sandbox_gateway.integration_tools` routes CodeWorker shell and managed file
  operations through gateway policy, exact permission binding, lifecycle, backend execution,
  WorkspaceEditPort transactions, receipts, and failure signals.
- `zyra_runtime.sandbox_gateway.integration_browser` routes workspace reads, network fetches,
  uploads, downloads, and live-plan preflight through the same bundle.
- `zyra_runtime.sandbox_gateway.integration_mcp` validates dynamic-tool provenance, bounds and
  redacts results, and prevents MCP payloads from mutating gateway control state.
- `zyra_runtime.sandbox_gateway.integration_remote` binds scheduler dispatch to workspace,
  artifact root, owner epoch, backend generation, policy digest, and a durable receipt.
- `zyra_runtime.sandbox_gateway.integration_control` connects cancel, interrupt, and recovery
  commands without taking ownership from the existing session or scheduler control planes.
- `zyra_runtime.sandbox_gateway.integration_events` projects execution receipts and backend
  failures into Zyra event metadata without persisting grants or raw secrets.
- `zyra_runtime.sandbox_gateway.integration_host` provides the only integration-level host
  process primitive: structured argv, `shell=False`, bounded output, redacted environment,
  process-tree termination, and auditable receipts.
- `@zyra/sandbox-gateway-control` supplies immutable TypeScript validation and serialization
  for integration envelopes, MCP results, remote receipts, and audit rules. It does not own
  lifecycle state or permission decisions.

## 3. Main-path reachability

The new code is dynamically reachable from production paths:

- `CodeWorkerRuntime` installs gateway services into the real QueryEngine ToolExecutor.
- `ToolExecutor.execute` routes owned tools before legacy implementations and fails closed when
  a required gateway is unavailable.
- `BrowserWorkerRuntime._load_url` has no direct filesystem or `urlopen` fallback; it delegates
  to `BrowserGatewayBoundary` or fails with `sandbox_gateway_unavailable`.
- `BrowserWorkerRuntime._run_browser_use_live` performs gateway plan preflight before live
  actions.
- Scheduler backends attach a gateway dispatch receipt to the real dispatch envelope and reject
  owner-epoch or backend-generation tampering.
- `CodeWorkerSidecarBridge` no longer calls raw `subprocess.run`; it uses the controlled host
  process runtime.

The managed production path requires a `WorkspaceEditPort`. In that path, sandbox changes are
committed only through `GatewayPatchPort` and WorkspaceManager transactions.

## 4. Bounded compatibility path

Existing permission-continuation conformance tests construct CodeWorkerRuntime with a plain
workspace directory and no WorkspaceManager port. Removing the old raw shell fallback exposed
that boundary. The replacement is deliberately constrained:

- It is active only when `workspace_edit_port is None` and `bundle.required is false`.
- It handles shell only; file mutations remain gateway-owned and fail closed without a managed
  port.
- It consumes the exact ToolPermissionRuntime grant once.
- It tokenizes to executable plus argv and always uses `shell=False`.
- It enforces output budgets, environment filtering, process-tree cancellation, and receipts.
- Every receipt and ToolResult marks `host_compatibility=true` and `main_path=false`.
- It is excluded from the managed-main-path completion claim and from WorkspaceManager ownership
  evidence.

This path is not a second gateway and is not counted as proof that an unmanaged workspace has
been deeply integrated. It preserves protected permission/session conformance behavior while
making the old process execution materially safer and auditable.

## 5. Permission and replay integrity corrections

Adversarial validation found and corrected the following integration defects:

- Dispatch validation now always compares receipt owner epoch and backend generation with the
  canonical envelope, even when callers omit optional expected values.
- Generated gateway session identifiers are filesystem-safe on Windows.
- Legacy command parsing rejects operators outside quotes but does not mistake a quoted Python
  or Node program argument for shell composition.
- Shell interpreters still reject composition and redirection; language interpreters still
  require exact one-use approval.
- Permission bridge request and replay validation now use the existing
  `GatewayCommandEnvelope.identity_digest` contract instead of looking for a nonexistent
  `command_digest` key in `permission_material()`.
- The host and tool integrations use the real `FAILED_TO_START` termination enum rather than a
  nonexistent `FAILED` member.

No caller-supplied `approved`, bypass, auto, or mutable replay flag is accepted as authority.

## 6. Behavioral evidence

### Python gateway and worker integration

Command:

```text
.\.venv\Scripts\python.exe -m unittest tests.unit.test_sandbox_gateway_integration_policy tests.integration.test_sandbox_gateway_worker_integration tests.integration.test_workspace_worker_gateway tests.unit.test_sandbox_gateway_policy tests.unit.test_sandbox_gateway_state tests.unit.test_sandbox_gateway_artifacts tests.integration.test_sandbox_gateway_runtime
```

Result: `30 tests`, all passed.

Covered effects include exact permission consumption, managed write/edit transactions, Browser
workspace reads, missing-gateway failure, MCP redaction, remote dispatch tamper rejection,
control cancellation, receipts, and failure projection.

### Adjacent permission and Browser regression

Command:

```text
.\.venv\Scripts\python.exe -m unittest tests.unit.test_permission_shell_extensions tests.integration.test_code_worker_permission_continuation_integration tests.integration.test_browser_worker_permission_gate
```

Result: `49 tests`, all passed.

This includes exact approved replay, denial, expiry, claim leases, lost receipts, ambiguous real
side effects, branch isolation, mode custody, and Browser action permission gates.

### TypeScript control supplement

Command:

```text
node --experimental-strip-types --test packages\runtime\sandbox-gateway-control\test\approval.test.ts packages\runtime\sandbox-gateway-control\test\contracts.test.ts packages\runtime\sandbox-gateway-control\test\hashline-control.test.ts packages\runtime\sandbox-gateway-control\test\policy.test.ts packages\runtime\sandbox-gateway-control\test\integration.test.ts
```

Result: `21 tests`, all passed.

### Static gateway audit

Result:

- Scanned production files: `23`
- Scanned production lines: `15,351`
- Blocking findings: `0`
- External runtime dependencies: `0`
- Disabled-gateway probe: passed
- Report digest: `sha256:4ef86b68892d1b9b3cd241d54ce360f1c3a5e96c42c268b97e6b9fc4f276cddf`

### Source custody and path boundary

- Source-custody manifest assertion: passed
- Source-custody digest: `sha256:41177fbdbfa0232fbb736e7e4c983f3a31ec251997b1b8c317ad8e9c14001725`
- Root-source relative/absolute path scan: `0` findings across `21` non-auditor production files
- `vendor-runtimes`, `runtime-sources`, and `source-pool` dependency scan: `0` findings
- `npm link`, editable pip path, and dynamic import scan: `0` findings
- `git diff --check`: passed

The only warnings were pre-existing unclosed stream `ResourceWarning` messages from
`typescript_claude_runtime.py`; they did not fail tests and are outside this slice's process
ownership.

## 7. Disconnect and semantic-effect evidence

- Approving a managed file write without an installed required gateway does not fall back to raw
  mutation; it returns `sandbox_gateway_unavailable` and the file is absent.
- Removing the Browser gateway boundary causes workspace URL loading to fail closed.
- Changing a dispatch envelope's backend generation after attestation invalidates the receipt.
- MCP output containing secret fields is redacted, and MCP output cannot replace gateway control
  state.
- Workspace write and edit each create a real committed WorkspaceManager transaction with a new
  snapshot and owner epoch.
- Approved shell continuation executes once; lost-outcome and replay tests prove duplicate side
  effects remain fenced.

These tests would fail or change behavior if the corresponding Zyra gateway modules were
disconnected. Vendor presence, import smoke, and descriptor output are not used as substitutes.

## 8. Effective line buckets

Cached numstat relative to the slice base reports:

- Production additions: `7,799`
- Production deletions: `66`
- Test additions: `662`
- Test deletions: `41`
- New integration-module physical lines: `7,085`
- New dedicated test-file physical lines: `484`
- Vendor/source-pool additions: `0`
- Generated/data/fixture/mock-only lines counted as production: `0`
- Adapter-only lines counted toward the minimum: `0`

The conservative physical-line count for new production modules is `7,085`, above the slice
minimum of `6,000`. M1-05B cumulative conservative production is `19,119`, above the parent
minimum of `14,000`.

## 9. Source-to-target custody

- OpenHands is the primary implementation source for sandbox lifecycle, action execution,
  backend failure projection, and artifact transfer.
- OpenClaw is supplementary for gateway policy, exact approval binding, control cancellation,
  and remote dispatch receipts.
- oh-my-pi is supplementary for structured argv, Hashline fencing, credential isolation, and
  bounded host/RPC results.
- claude-code-best remains conformance-only for this slice because the existing
  TypeScriptClaudeQueryEngine and ToolPermissionRuntime retain their protected ownership.
- AgentScope, Hermes, and opencode remain reference-only for this slice.

No source repository is imported or executed at runtime. TypeScript remains TypeScript where it
owns validation/serialization behavior; no blanket cross-language rewrite was introduced.

## 10. Residual risks and deferred validation

- The existing permission/session owner does not currently make a second ASK created inside the
  same resumed tool batch recoverable with another public continuation. The gateway integration
  test therefore uses independent exact-approval sessions for write and edit. This behavior was
  observed during this slice but was not introduced by it. Fixing it would modify a protected
  completed owner and requires a separately authorized permission/session review.
- Full-repository and cleanroom suites were not repeated for this ordinary slice. They remain
  mandatory at the M1-05 sibling aggregation or milestone exit layer.
- The host compatibility path is retained only for protected conformance coverage. It must not
  be cited as managed-workspace evidence or enabled when `sandbox_gateway_required=true`.

These residuals do not create a raw process, external repository, vendor runtime, second
permission authority, or second canonical gateway in the M1-S05B-02 main path.

## 11. Final adversarial conclusion

The slice satisfies its integration boundary without moving Claude-derived runtime ownership,
permission ownership, scheduler ownership, or WorkspaceManager ownership. The default managed
path uses one SandboxGatewayRuntime, real permission consumption, real workspace transactions,
real browser/MCP/dispatch boundaries, durable receipts, and fail-closed behavior. No vendor-like
code, source-pool material, generated inventory, or thin black-box adapter is used to meet the
production minimum.
