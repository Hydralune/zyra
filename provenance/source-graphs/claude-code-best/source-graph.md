# Claude Code 初始 Source Graph

状态：第一版草图，基于 2026-07-07 的只读探索。尚未完成逐行验证。

## 总体运行链路

```text
CLI / bridge / SDK entry
  -> startup bootstrap
  -> QueryEngine.submitMessage()
  -> processUserInput()
  -> system prompt + memory + context + attachments
  -> query()
     -> snip / microcompact / context collapse / autocompact
     -> services/api/queryModel()
        -> client factory
        -> withRetry()
        -> raw SSE stream state machine
     -> assistant message with tool_use
     -> runTools()
        -> serial/concurrent partition
        -> runToolUse()
           -> schema validation
           -> PreToolUse hooks
           -> canUseTool / permission runtime
           -> tool.call()
           -> PostToolUse hooks
           -> result persistence / budget
           -> contextModifier / new messages
     -> tool_result messages
     -> loop until end_turn / abort / budget / error
  -> session/cost/state persistence
```

## L0：启动与全局状态

| Node | Source paths | Role | Zyra reading |
| --- | --- | --- | --- |
| CLI fast path | `src/entrypoints/*`, `src/main.tsx`, `src/setup.ts`, `src/bootstrap/state.ts` | 选择运行路径，延迟导入，建立会话级全局状态 | 不应整迁 CLI；应吸收 fast path、session state leaf、startup profiling 的设计 |
| Bootstrap state | `src/bootstrap/state.ts` | 会话 ID、成本、API 状态、缓存锁存、invoked skills、计划/telemetry 状态 | Zyra 应拆成 `CodeWorkerSessionStore`、runtime latch state、event/cost state |
| Setup | `src/setup.ts`, `src/context.ts` | 信任后环境准备、cwd、hooks、worktree、后台预取 | Zyra 需要 workspace/session bootstrap port |

## L1：Query/session 链路

| Node | Source paths | Entry | State / output |
| --- | --- | --- | --- |
| QueryEngine | `src/QueryEngine.ts` | `QueryEngine.submitMessage()` | mutable messages, usage, permission denials, read file cache, system prompt context, MCP clients, commands |
| Legacy ask wrapper | `src/QueryEngine.ts` | `ask()` | 构造 QueryEngine 并代理 `submitMessage()` |
| User input processor | `src/utils/processUserInput/processUserInput.ts` | `processUserInput()` | slash command result, attachments, tool allowlist, shouldQuery |
| Context sources | `src/context.ts`, `src/utils/queryContext.ts`, `src/constants/prompts.ts`, `src/utils/systemPrompt.ts`, `src/utils/attachments.ts` | `fetchSystemPromptParts()`, `getSystemContext()`, `getUserContext()`, `getAttachments()` | system prompt parts, memory prompt, git/env context, per-turn attachments |
| Query loop | `src/query.ts` | `query()` | per-turn loop, tool use/result pairing, compact state, token budget, retry/recovery |

Initial judgment:

- QueryEngine 是会话级 owner，`query()` 是每个用户回合的执行循环。
- Zyra 不应该只迁移 `query()`；必须同时定义 session state、context port、tool pool port 和 event output。
- M1-02A/02B 最小纵切应包含：session seed + context snapshot + tool pool + query loop shell + event mapping。

Batch 01 verified detail:

- `QueryEngine.submitMessage()` 在进入 `query()` 前已经完成 cwd 设置、permission denial 包装、system/user/system context 前缀构建、memory mechanics prompt 注入、structured output hook 注册、orphaned permission 处理、输入归一化、pre-submit hook 执行、transcript 预写入、command-scoped allowed tools 写入、skills/plugins cache-only 加载和 system-init 输出。
- `ProcessUserInputContext` 被构建两次：第一次允许 slash command 通过 `setMessages` 改写 `mutableMessages`；第二次在输入处理后冻结为 query loop 使用的上下文。
- `processSlashCommand()` 是命令、Skill、compact、forked sub-agent、plugin telemetry 和 command permission 的交汇点，不应被拆成一个仅前端控制命令。
- `context.ts` 的 `getUserContext()` 会读取 CLAUDE.md / memory 并缓存给 auto-mode classifier；`getSystemContext()` 会生成 git 快照和 cache breaker。它们都参与模型前缀，不是普通附加信息。
- 详细记录见 `batch-01-query-session-context.md`。

## L2：Tool registry 与 execution 链路

| Node | Source paths | Entry | State / output |
| --- | --- | --- | --- |
| Tool contract | `src/Tool.ts` | `Tool`, `ToolUseContext`, `buildTool()` | schema, permission hooks, UI rendering, behavior flags, contextModifier |
| Base registry | `src/tools.ts` | `getAllBaseTools()`, `getTools()`, `assembleToolPool()` | built-in tools, feature gates, deny filtering, MCP merge, stable ordering |
| Tool orchestration | `src/services/tools/toolOrchestration.ts` | `runTools()` | serial/concurrent partition, completion marking |
| Tool execution | `src/services/tools/toolExecution.ts` | `runToolUse()`, `streamedCheckPermissionsAndCallTool()`, `checkPermissionsAndCallTool()` | validation, permission, hooks, call, result block mapping, persistent large results |
| Streaming executor | `src/services/tools/StreamingToolExecutor.ts` | `StreamingToolExecutor` | early/streaming tool execution while assistant message is still arriving |

Initial judgment:

- `ToolUseContext` is the critical state seam for Zyra, not the individual tool modules alone.
- `buildTool()` defaults are mixed: concurrency/read/write classification is conservative, while `checkPermissions` defaults to allow and relies on the general permission layer. Zyra should preserve this split instead of treating tool-local permission as the only guard.
- Tool result budget and artifact persistence must be first-class Zyra behavior, not hidden in a sidecar.

Batch 02 verified detail:

- `query.ts` is a loop-level state machine. It applies tool result budget, snip, microcompact, context-collapse projection and autocompact before the API call, then manages streaming fallback, reactive compact, max-output-token recovery, stop hooks, token-budget continuation, tool execution, queued-command attachments, memory/skill attachments, MCP tool refresh and max-turn termination.
- `ToolUseContext` is the central runtime context for app state, MCP clients, active tools, agent identity, abort, file/read state, hook prompts, in-progress tool ids, content replacement and query tracking.
- `tools.ts` filters blanket-denied tools before the model sees them and assembles built-in + MCP tools with stable ordering for prompt-cache stability.
- `runTools()` partitions tool calls by `isConcurrencySafe(input)`: concurrent-safe batches run together and apply context modifiers after the batch; non-safe tools run serially and apply context modifiers immediately.
- `StreamingToolExecutor` can start tools while assistant streaming is still in progress, discards in-flight results on streaming fallback and emits synthetic tool_result messages for interrupts or sibling Bash errors.
- `toolExecution.ts` single-tool pipeline is schema validation -> tool validation -> speculative Bash classifier -> PreToolUse hooks -> permission decision -> tool.call -> result mapping/persistence -> PostToolUse hooks -> failure hooks/MCP auth state updates.
- 详细记录见 `batch-02-query-tool-loop.md`。

## L3：Permission 链路

| Node | Source paths | Role | Zyra reading |
| --- | --- | --- | --- |
| Permission context | `src/Tool.ts`, `src/utils/permissions/PermissionRule.ts`, `src/utils/permissions/PermissionMode.ts` | rule sources and mode | Zyra `ToolPermissionRuntime` needs explicit source + mode fields |
| Rule loading/parsing | `src/utils/permissions/permissionsLoader.ts`, `permissionRuleParser.ts`, `shadowedRuleDetection.ts` | load and diagnose rules | Zyra needs settings/session/API rule sources |
| Core evaluator | `src/utils/permissions/permissions.ts`, `filesystem.ts` | ordered allow/deny/ask and filesystem path checks | must become deterministic runtime guard |
| Auto/classifier | `src/utils/permissions/yoloClassifier.ts`, `classifierDecision.ts`, `bashClassifier.ts` | optional AI-assisted allow/deny | Zyra can use classifier as advisory, not sole decision owner |
| Denial fuse | `src/utils/permissions/denialTracking.ts` | consecutive/total denial limits | should affect loop continuation and event log |
| Hook/user approval | `src/hooks/useCanUseTool.ts`, `src/bridge/bridgePermissionCallbacks.ts`, `src/services/tools/toolExecution.ts` | ask path and hook path | M2 UI/API must connect to same permission queue |

Initial judgment:

- Permission cannot be a UI-only panel. It is inside tool execution before `tool.call()`.
- Bypass/auto modes still keep safety guards; Zyra should preserve this distinction.
- The root permission implementation is `src/utils/permissions/**`; `src/cli/src/utils/permissions/**` appears to be a small shim/mirrored path and should not be mistaken for the source of truth.

Batch 03 verified detail:

- Deterministic evaluator order is deny rule -> ask rule -> tool-specific `checkPermissions` -> tool deny -> interactive-required ask -> content-specific ask -> safetyCheck ask -> bypass mode -> always allow -> passthrough-to-ask. Bypass does not override deny/safety/interactive-required checks.
- `resolveHookPermissionDecision()` preserves the invariant that PreToolUse hook allow does not override settings deny/ask or safety checks; hook allow is followed by `checkRuleBasedPermissions()`.
- `useCanUseTool()` handles ask through coordinator, swarm worker and interactive handlers. Interactive approval is a race among local UI, CCR bridge, channel relay, PermissionRequest hooks, Bash classifier and recheck callbacks.
- `PermissionContext` persists updates, logs decisions, builds allow/deny decisions and owns abort/cancel behavior without depending directly on React queue internals.
- `permissionLogging.ts` stores decision metadata in `toolUseContext.toolDecisions` keyed by `toolUseID`; `toolExecution.ts` later consumes and clears it.
- Auto mode is deterministic shell plus optional classifier: acceptEdits and safe-tool fast paths can allow; classifier blocks update denial tracking; denial limits fall back to prompting or abort in headless mode.
- `permissionSetup.ts` strips dangerous Bash/PowerShell/Agent allow rules when entering auto mode and restores them when leaving; entering auto is therefore a reversible context mutation, not only a mode flag.
- `yoloClassifier.ts` deliberately excludes assistant natural-language text from classifier input and only includes user text plus assistant tool_use blocks, preventing model-authored text from steering the classifier.
- 详细记录见 `batch-03-permission-runtime-hooks.md`。

## L4：API/retry/streaming 链路

| Node | Source paths | Entry | State / output |
| --- | --- | --- | --- |
| API client factory | `src/services/api/client.ts` | `getAnthropicClient()` | provider choice, headers, OAuth refresh, fetch wrapper |
| Query model | `src/services/api/claude.ts` | `queryModel()` | request params, tool schemas, prompt cache control, raw SSE, AssistantMessage |
| Retry | `src/services/api/withRetry.ts` | `withRetry()` | retry events, fallback, foreground/background distinction |
| Errors | `src/services/api/errors.ts` | `getAssistantMessageFromError()` and helpers | user-readable and machine-readable error details |
| Cost | `src/cost-tracker.ts` | cost accumulation / restore | per-model usage and session-level cost state |

Initial judgment:

- AsyncGenerator is both control flow and event protocol. Zyra should map each yielded retry/progress/assistant/tool message into event log spans.
- Foreground/background retry semantics are scheduler signals, not just API client details.

Batch 08 verified detail:

- `claude.ts` is a second-layer state machine under `query.ts`: it builds request params, locks beta/cache/fast-mode/task-budget fields, records prompt cache state, adds cache breakpoints, calls `withRetry()`, parses raw SSE events, maps errors to assistant messages, writes usage/cost and logs API spans.
- `client.ts` is provider-aware. `getAnthropicClient()` selects first-party/OAuth/API-key, Bedrock, Foundry or Vertex clients, attaches session/request headers, handles proxy/custom fetch and refreshes provider credentials.
- `withRetry.ts` owns mutable retry context (`model`, `maxTokensOverride`, `thinkingConfig`, `fastMode`), auth refresh, retry-after handling, persistent unattended retry, foreground-only 529 retry, fast-mode cooldown and `FallbackTriggeredError`.
- Raw SSE parsing yields assistant messages at `content_block_stop` and later mutates the same message object at `message_delta` to fill final usage and stop reason. Zyra event storage must represent this as either mutable transcript state or explicit patch events.
- Streaming watchdog and stall detection are built into `queryModel()`. Broken streams can fall back to non-streaming, but this path must coordinate with `query.ts` tombstones and `StreamingToolExecutor.discard()` to avoid duplicate tool execution.
- `executeNonStreamingRequest()` shares retry policy, caps fallback output at `MAX_NON_STREAMING_TOKENS`, adjusts thinking budget and records fallback cost in `finally` so early generator termination does not lose accounting.
- `errors.ts` is a recovery-control mapper, not only display text. Prompt-too-long, media-size, tool_use/tool_result mismatch, duplicate tool IDs, auth, rate limit, model availability, provider and SSL errors become typed assistant API messages with `errorDetails` where later recovery logic needs it.
- `promptCacheBreakDetection.ts` tracks per-query-source prompt cache state across system/tool/model/beta/fast-mode/auto-mode/overage/effort/extra-body changes and distinguishes expected cache deletion or TTL effects from real cache breaks.
- `logging.ts` and `cost-tracker.ts` attach API query/error/success events, gateway detection, OTel spans, permission mode, query chain/depth, TTFT, retry durations, cache tokens, cost, model usage and resumable session cost state.
- Detailed record: `batch-08-api-streaming-retry-client.md`.

## L5：Compact/context restore 链路

| Node | Source paths | Entry | State / output |
| --- | --- | --- | --- |
| Microcompact | `src/services/compact/microCompact.ts` | dependency-injected from `query()` | clears old tool results or schedules cache edits |
| Autocompact | `src/services/compact/autoCompact.ts` | `autoCompactIfNeeded()` | threshold, token warning, consecutive failure fuse |
| Full compact | `src/services/compact/compact.ts` | `compactConversation()` | PreCompact/PostCompact hooks, summary messages, boundary metadata, restored attachments |
| Session memory compact | `src/services/compact/sessionMemoryCompact.ts` | `trySessionMemoryCompaction()` | shortcut summary path using maintained session memory |
| Post compact cleanup | `src/services/compact/postCompactCleanup.ts` | `runPostCompactCleanup()` | resets caches and collapse state |

Initial judgment:

- Compact is not just summarization. It is a lifecycle: pre-hooks, summary, boundary, restored files/skills/plan/MCP/agent deltas, post-hooks.
- Zyra MemoryFabric should model compact boundary and restore artifacts explicitly.

Batch 04 verified detail:

- Auto compact is a query-loop preflight with output-token headroom, threshold buffers, source/mode gates, session-memory-first strategy and a three-failure circuit breaker. It must run before the model call, not as an after-the-fact cleanup task.
- Session-memory compact uses maintained session memory and carefully chooses the kept suffix. It preserves tool_use/tool_result pairing and assistant chunks sharing the same `message.id`, then emits the same `CompactionResult` shape as full compact.
- Microcompact is a lossy prompt-shaping reducer. The active time-based branch clears old compactable tool_result content when prompt-cache TTL is likely expired; it is not a durable summary or memory boundary.
- `compactConversation()` performs PreCompact hooks, restricted no-tool summarization, prompt-too-long retries by API-round truncation, post-compact file/skill/plan/plan-mode/async-agent/tool/MCP restore, SessionStart hooks, boundary creation, telemetry, metadata/transcript updates and PostCompact hooks.
- Partial compact has direction semantics: `from` preserves prefix, `up_to` preserves suffix. `annotateBoundaryWithPreservedSegment()` records head/anchor/tail UUIDs so the transcript/session loader can relink kept messages.
- Manual `/compact` uses the same compact service path after optional session-memory and reactive routes. It is therefore a runtime control command, not a separate UI-only behavior.
- `runPostCompactCleanup()` resets microcompact, selected main-thread caches, system prompt sections, classifier/speculative state, beta tracing, attribution and session message cache, but intentionally does not clear invoked skills.
- `handleStopHooks()` saves cache-safe params, fires prompt suggestion/memory extraction/auto dream, runs Stop/TaskCompleted/TeammateIdle hooks, and can block or prevent continuation. Stop hooks are part of query continuation control.
- `query/tokenBudget.ts` implements main-thread automatic continuation until the user-requested budget is nearly reached or progress shows diminishing returns; subagents are excluded.
- Current `reactiveCompact.ts`, `contextCollapse/index.ts`, `cachedMicrocompact.ts`, `snipCompact.ts`, `snipProjection.ts` and `query/transitions.ts` are generated/no-op stubs in this snapshot and should not be counted as implemented compact capabilities.
- 详细记录见 `batch-04-compact-context-restore.md`。

## L6：MCP 链路

| Node | Source paths | Entry | State / output |
| --- | --- | --- | --- |
| MCP config | `src/services/mcp/config.ts` | config loaders | server definitions, disabled state, scopes |
| MCP client | `src/services/mcp/client.ts` | connection and tool/resource builders | connected/needs-auth/failed states, tools, resources, prompts |
| MCP auth | `src/services/mcp/auth.ts`, `src/services/mcp/xaa*.ts` | OAuth and auth provider | token refresh, needs-auth cache, retry |
| MCP tools | `src/tools/MCPTool/*`, `ListMcpResourcesTool/*`, `ReadMcpResourceTool/*`, `McpAuthTool/*` | tool layer integration | tool call results, resource reads, auth prompts |
| MCP commands | `src/commands/mcp/*` | user/control commands | management UI/API equivalent |

Initial judgment:

- MCP is not just a client library. It affects tool pool, permission, auth, prompt cache, and control commands.
- Zyra should probably define a Python-owned MCP port and only migrate TS details where behavior is uniquely valuable.

Batch 05 verified detail:

- `config.ts` owns MCP source precedence, enterprise exclusive mode, project `.mcp.json` approval, policy filtering, env/header expansion, plugin/claude.ai dedupe and scope-specific writes. Zyra needs this as a config store with provenance, not a flattened server map.
- `client.ts` and `useManageMCPConnections.ts` form the runtime state machine: connect by transport, cache by serialized config, expose connected/failed/needs-auth/pending/disabled states, fetch tools/resources/prompts/skills, update AppState, handle list_changed/resources_changed, reconnect remote transports and clean stale plugin/config-changed clients.
- Remote MCP auth is a separate lifecycle. OAuth/XAA, step-up scope, refresh locking, token revocation, server-key hashing and 15-minute needs-auth cache decide whether tools are available before query-time tool execution.
- `fetchToolsForClient()` projects MCP annotations into the generic `Tool` contract: read-only/concurrency/destructive/open-world flags, `mcpInfo`, permission passthrough, progress events, session-expired retry and result `mcpMeta`/structured content.
- MCP resources and prompts are first-class runtime surfaces. Resources create deferred List/Read MCP resource tools; prompts become slash commands named `mcp__server__prompt`.
- MCP output is not plain text. `processMCPResult()`, `mcpValidation.ts` and `mcpOutputStorage.ts` handle structured content, content arrays, images, binary blobs, persisted artifacts and token-aware truncation.
- Elicitation is a pending request queue with hooks and retry semantics, not a simple exception. URL elicitation can retry MCP tool calls after user/API resolution.
- Server instructions enter context either through legacy system prompt sections or persisted `mcp_instructions_delta` attachments. The latter is designed to preserve prompt cache when servers connect late and must be part of compact restore.
- Channel notification/permission relay is gated by capability, runtime flag, OAuth, enterprise/team policy, session opt-in, marketplace verification and allowlist. It is a useful model for future Zyra remote-worker permission relay.
- 详细记录见 `batch-05-mcp-runtime-tools-auth.md`。

## L7：Skill/Agent/subagent/commands 链路

| Node | Source paths | Role | Zyra reading |
| --- | --- | --- | --- |
| Commands | `src/commands.ts`, `src/commands/*` | slash command registry and command-to-skill bridge | map to Zyra `ControlCommand` |
| REPL / PromptInput / TUI state | `src/screens/REPL.tsx`, `src/components/PromptInput/**`, `src/hooks/useCommandQueue.ts`, `src/utils/handlePromptSubmit.ts`, `src/utils/messageQueueManager.ts`, `src/components/**`, `src/ink/**` | prompt input, slash suggestions, command queue, local-jsx overlays, permission/status/dialog UI, terminal rendering | map to Zyra Web command input, prompt queue, overlay state, approval panels and event projections |
| SkillTool | `src/tools/SkillTool/*`, `src/skills/*` | Markdown skill invocation and context disclosure | map to Zyra SkillRuntime |
| AgentTool | `src/tools/AgentTool/AgentTool.tsx`, `runAgent.ts`, `forkSubagent.ts`, `loadAgentsDir.ts` | subagent spawn, background task, worktree/remote isolation | map to Zyra SubagentRuntime and WorkerLifecycle |
| Tasks | `src/tasks/LocalAgentTask/*`, `src/tasks/RemoteAgentTask/*` | background task state | map to scheduler task state and event log |
| Worktree | `src/utils/worktree.ts` | isolation and cleanup | map to workspace manager / sandbox gateway |

Initial judgment:

- AgentTool is already a mini runtime, not a simple tool. It touches permissions, tool pool, system prompt, worktree, background lifecycle, task output and session metadata.
- This should be migrated as a dedicated vertical slice after base Query/tool/permission runtime exists.

Batch 06 verified detail:

- Skill execution is a session-aware runtime. `SkillTool` validates prompt commands, applies `Skill` allow/deny rules, auto-allows only safe-property commands, asks with exact/prefix allow suggestions, then either expands inline messages or runs a forked sub-agent.
- Inline skill expansion goes through `processPromptSlashCommand()`, not a separate shortcut. It loads skill markdown, executes allowed shell substitutions, registers skill hooks, records `invokedSkills`, extracts attachments with `skipSkillDiscovery`, emits `command_permissions`, then modifies later permissions/model/effort through `contextModifier`.
- Forked skills use `runAgent()` with a new `agentId`, collect progress, and clear invoked skill state for that forked agent on completion.
- `loadSkillsDir.ts` owns managed/user/project/add-dir skill discovery, legacy commands-as-skills, dynamic nested skill discovery, conditional `paths` activation, frontmatter parsing, shell expansion, and MCP skill builder registration.
- `attachments.ts` announces `skill_listing` per agent, suppresses duplicate listing on resume, filters to bundled+MCP when skill search is enabled, and preserves used skill content via `invoked_skills` after compact.
- `compact.ts` restores invoked skill content by agent scope with per-skill and total token budgets instead of re-injecting the whole skill listing.
- Plugin commands/skills/hooks are a capability supply chain: `pluginLoader.ts` creates `LoadedPlugin`, `loadPluginCommands.ts` turns markdown/skill dirs into namespaced prompt commands, `loadPluginHooks.ts` atomically swaps plugin hooks, and `pluginOptionsStorage.ts` controls `${CLAUDE_PLUGIN_ROOT}`, `${CLAUDE_PLUGIN_DATA}` and `${user_config.KEY}` substitution.
- Current remote skill search and MCP skills are stubs in this snapshot (`services/skillSearch/*`, `DiscoverSkillsTool/prompt.ts`, `skills/mcpSkills.ts`). They should be treated as extension points, not completed capabilities.
- 详细记录见 `batch-06-skill-plugin-hooks.md`。

Batch 07 verified detail:

- `AgentTool` is a worker runtime entry. It dispatches between teammate spawn, remote CCR task, forked child and normal subagent, then wires model selection, tool pool, permission mode, async/background policy, cwd/worktree isolation and task lifecycle.
- `loadAgentsDir.ts` merges built-in, plugin, user, project, flag and policy agents. Agent definitions carry runtime semantics: tools/disallowedTools, permissionMode, MCP servers, hooks, skills, maxTurns, initialPrompt, memory, background and isolation.
- `runAgent.ts` constructs a worker query loop: resolves tools, builds agent prompt, executes SubagentStart hooks, registers agent hooks, preloads skills, initializes agent-specific MCP servers, creates an isolated ToolUseContext, records sidechain transcript/metadata and then calls `query()`.
- Fork subagents preserve cache-safe prefixes by reusing the parent rendered system prompt, exact tool pool, cloned content replacement state, placeholder tool_results and recursive-fork guard.
- `LocalAgentTask` and `runAsyncAgentLifecycle()` own background progress, kill/fail/complete transitions, task notification, partial result, handoff classifier and task output symlink to sidechain transcript.
- `SendMessageTool` is the continuation path for local agents: running tasks receive pending messages, terminal or evicted tasks are resumed from transcript plus metadata through `resumeAgentBackground()`.
- `worktree.ts` provides real isolation semantics: slug validation, hook/git creation paths, post-creation setup, `.worktreeinclude`, fail-closed change detection, stale ephemeral cleanup and branch/worktree removal.
- `RemoteAgentTask` models remote workers as restorable tasks with sidecar metadata, CCR polling, output append, completion checkers, remote review/ultraplan special cases and archive-on-kill.
- Swarm/in-process teammates reuse `runAgent()` but are resident workers: AsyncLocalStorage identity, AppState task, mailbox/pending-message loop, leader permission bridge, per-worker compact and idle notifications.
- 详细记录见 `batch-07-agent-subagent-task-isolation.md`。

Batch 09 verified detail:

- `commands.ts` is a dynamic runtime registry. It merges built-ins, feature-gated commands, dynamic skills, MCP skill commands, SkillTool commands, plugin commands and workflows, then applies availability, `isEnabled`, remote-safe and bridge-safe filters. Zyra should model this as `ControlCommandRegistry`, not a fixed enum.
- Command execution has three semantic classes: `prompt`, `local` and `local-jsx`. `local-jsx` receives a `ToolUseContext`-derived context and can mutate messages, AppState, MCP config, session resume state and UI overlays through `onDone`; it is not only a React component.
- TUI prompt handling is controlled by `QueryGuard`, `messageQueueManager`, `useCommandQueue`, `queueProcessor` and `handlePromptSubmit`. Queued commands carry priority, source, bridge origin, pasted content, workload, metadata and optional `agentId`; cancellation and batching are first-class behavior.
- Runtime commands are state mutations or isolated runtime side paths. `/context`, `/compact`, `/btw`, `/clear`, `/resume`, `/branch`, `/rewind`, `/mcp`, `/permissions`, `/memory`, `/plan`, `/model`, `/fast`, `/effort`, `/add-dir`, `/reload-plugins`, `/agents`, `/tasks`, `/diff`, `/doctor`, `/status`, `/session` and `/cost` all reach into session state, side-question fork state, tool/permission/MCP/plugin/agent state, file history or API configuration.
- `/btw` is a side-question command: it builds cache-safe fork params from current context/messages, runs a one-turn forked agent with all tools denied, skips cache write, and keeps the response separate from the main conversation transcript. Zyra should model this as `SideQuestionRuntime`, not a normal chat turn.
- `/clear` is a full session lifecycle command: SessionEnd hooks, cache eviction hints, background task preservation, foreground task kill, cache sweeps, AppState reset, MCP state reset, session id regeneration, task output symlink repair, worktree/mode persistence and SessionStart hooks.
- `/branch` copies transcript state with rewritten session ids, parent UUIDs and `content-replacement` entries, then resumes into the branch. This ties command control directly to tool result budget and prompt cache stability.
- `main.tsx` has a shared Commander `preAction` bootstrap for subcommands: settings/keychain waits, `init()`, log sinks, `--plugin-dir` inline plugin injection, migrations, remote managed settings, policy limits and settings sync.
- CLI subcommands are management surfaces for the same runtime systems: `mcp` starts/health-checks/configures servers and OAuth secret cleanup; `plugin` manages plugin/marketplace supply; `agents` exposes source/override/shadowed agent definitions; `auto-mode` exposes classifier config and critique; `doctor`, `auth`, `task` and `remote-control` are control/observability entries.
- `controlSchemas.ts`, `structuredIO.ts`, `remoteIO.ts` and `print.ts` form a headless/SDK control plane. It supports initialize, interrupt, permission ask, set_permission_mode, set_model, thinking tokens, MCP status/set/reconnect/toggle/message, context usage, rewind files, cancel async message, seed read state, reload plugins, stop task, settings, elicitation, keepalive and environment updates.
- `StructuredIO` owns pending control requests, abort/cancel, duplicate permission response suppression, hook-vs-SDK permission racing, bridge permission injection, SDK hook callbacks, MCP elicitation forwarding and sandbox network permission via synthetic `SandboxNetworkAccess`.
- `RemoteIO` adapts the same protocol to WebSocket/SSE/CCR, including token refresh, keepalive, delivery ack, session state/metadata reporting and CCR internal event readers/writers for transcript restore.
- `print.ts` headless mode is not a one-shot prompt runner. Its control dispatcher mutates AppState, MCP clients/tools/commands/resources, command/agent registry, settings, model/thinking config, prompt queue, task state and remote-control bridge, sharing the same runtime semantics as REPL.
- 详细记录见 `batch-09-tui-cli-commands-control.md`。

## Cross-cutting state ownership to resolve

| State | Upstream owner candidates | Zyra owner candidate |
| --- | --- | --- |
| Session messages | `QueryEngine`, `bootstrap/state.ts`, `sessionStorage` | `CodeWorkerSessionStore` + event log |
| Tool pool | `tools.ts`, MCP client, commands/skills | `ToolRegistryRuntime` |
| Permission mode/rules | `ToolPermissionContext`, permission setup/loader | `ToolPermissionRuntime` |
| File read state | `ToolUseContext.readFileState`, `FileStateCache` | `WorkspaceState` or `CodeWorkerSession` |
| CWD/worktree | `ToolUseContext`, `cwd`, `worktree` utils | `WorkspaceManager` |
| Token/cost | `query.ts`, `cost-tracker.ts`, API usage | `RuntimeBudgetState` + `ResourceScheduler` |
| Compact state | `autoCompactTracking`, compact boundary messages | `MemoryFabric` + `CodeWorkerSession` |
| MCP connection | `services/mcp/client.ts` | `McpRuntimeStore` |
| Background agents | `AgentTool`, `LocalAgentTask`, `RemoteAgentTask` | `WorkerLifecycleRuntime` |
| Command registry / queue | `commands.ts`, `messageQueueManager`, `handlePromptSubmit`, `StructuredIO` | `ControlCommandRegistry` + `PromptQueueRuntime` |
| CLI/TUI interaction state | `REPL.tsx`, `PromptInput/**`, `useCommandQueue`, `local-jsx` command components, `ink/**` | `WebCommandInput` + `CommandOverlayState` + `ApprovalPanelState` |
| Runtime control protocol | `controlSchemas.ts`, `structuredIO.ts`, `print.ts`, `remoteIO.ts` | `RuntimeControlDispatcher` + Web/API/CLI adapters |

## Recommended migration waves

1. CodeWorker session skeleton: QueryEngine state model, context snapshot, event mapping.
2. Tool loop skeleton: tool contract, registry, serial/concurrent executor, result artifact mapping.
3. Permission runtime: deterministic allow/deny/ask, denial fuse, API approval queue.
4. API and retry port: model client abstraction, streaming events, token/cost budget.
5. Compact/restore: microcompact boundary, auto compact trigger, post-compact file/skill restore.
6. MCP runtime: config, connection state, tools/resources/prompts/auth.
7. Skill and Agent runtime: Markdown skills, subagent spawn, background lifecycle, worktree isolation.
8. Command/control: context/compact/btw/cost/mcp/permissions/agents commands into Zyra ControlCommand and M2 UI.
9. CLI/TUI interaction productization: REPL/PromptInput command entry, queue preview/cancel, local-jsx overlays, permission/status/dialog flows and Ink renderer semantics into Web command input, overlay state and approval panels.
