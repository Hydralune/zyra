# Batch 02: Tools / Skills / MCP / Middleware

Date: 2026-07-07
Status: first-pass chain read complete; remaining work is line-level edge-case review
Repository: `G:\agent-zoo\agent-framework`

This batch follows `batch-01-core-agent-client-types.md`. Its purpose is to read the execution mechanisms that sit under `Agent.run()` and `BaseChatClient.get_response()`: tool-loop ownership, approval semantics, skill disclosure, MCP tool transport, and middleware interception.

## Read Scope So Far

Primary files:

- `python/packages/core/agent_framework/_tools.py`
- `python/packages/core/agent_framework/_skills.py`
- `python/packages/core/agent_framework/_mcp.py`
- `python/packages/core/agent_framework/_middleware.py`

Concrete ranges already inspected:

- `_tools.py`
  - `FunctionTool` construction and invocation: around lines 243-885
  - `_auto_invoke_function`: around lines 1391-1615
  - `_try_execute_function_calls`: around lines 1625-1878
  - `_handle_function_call_results`: around lines 2184-2239
  - `FunctionInvocationLayer.get_response`: around lines 2392-2845
- `_skills.py`
  - class and constant index for `SkillsProvider`, `SkillManifest`, loaders, resource tools, and script execution tools
  - `SkillsProvider.before_run` / `_create_tools` / `_load_skill` / `_run_skill_script` / `_read_skill_resource`
  - `FileSkillsSource` discovery, resource/script scanning, frontmatter parsing, path traversal and symlink checks
  - `MCPSkillsSource`, `MCPSkill`, `MCPSkillResource`
- `_mcp.py`
  - class and lifecycle index for `MCPTool`, transport-specific MCP tool types, task options, prompt/tool loading, task polling, and cancellation
  - lifecycle owner queue and connect/reconnect path
  - `load_prompts`, `load_tools`, `call_tool`, task-oriented tool execution and cancellation path
- `_middleware.py`
  - class and function index for agent/chat/function middleware contexts, middleware pipelines, and termination semantics
  - `AgentMiddlewarePipeline`, `ChatMiddlewarePipeline`, `FunctionMiddlewarePipeline`
  - `AgentMiddlewareLayer`, `ChatMiddlewareLayer`, `categorize_middleware`

## Execution Chain

The concrete tool chain currently reads as:

```text
Agent.run
  -> RawAgent._prepare_run_context
  -> BaseChatClient.get_response
  -> FunctionInvocationLayer.get_response
  -> underlying BaseChatClient / provider call
  -> tool_calls detected in ChatResponse
  -> _try_execute_function_calls
  -> _auto_invoke_function
  -> FunctionMiddlewarePipeline
  -> FunctionTool.invoke
  -> tool-result messages appended
  -> provider call repeats until stop condition
```

This confirms that the real tool loop is not owned by `Agent.run()`. `Agent.run()` owns session, context, option, and MCP preparation; `FunctionInvocationLayer` owns iterative tool execution.

## Key Findings

### 1. `FunctionTool` is a rich execution unit, not a thin callable wrapper

`FunctionTool` carries:

- JSON-schema style argument metadata inferred from Python signatures and docstrings.
- `approval_mode`, declaration-only mode, invocation limits, and exception limits.
- Internal context-parameter injection so runtime-only values can be passed without exposing them to the model.
- Sync and async invocation support, including off-event-loop execution for blocking callables.
- Result normalization into framework `Content` values.

For Zyra, this is directly relevant to `ToolPermissionRuntime` and `CodeWorkerRuntime`: the source pattern treats each tool as a policy-bearing runtime object, not just as a function pointer.

### 2. Approval is part of the tool loop protocol

`_try_execute_function_calls` distinguishes normal tools, declaration-only tools, approval tools, unknown tools, already-approved tool calls, and newly requested approvals.

Observed behavior:

- A tool with approval required can return a `function_approval_request` instead of invoking immediately.
- Approval requests are stored in session-visible state so the loop can resume after a later approval response.
- Approval responses are represented as content/messages, not as an out-of-band side channel only.
- The loop can stop and return a response containing pending approval content.

For Zyra, this is a better fit than treating permission as a pre-call yes/no helper. It suggests a resumable permission state machine embedded in the session and event stream.

### 3. User input and middleware termination are first-class loop exits

`UserInputRequiredException` and `MiddlewareTermination` can both interrupt normal tool execution. The loop can surface these as response content and stop instead of forcing a final assistant response.

This is important for Zyra's long-task runtime because it gives a source pattern for:

- human-in-the-loop pauses,
- recoverable tool interruption,
- policy/middleware-controlled early termination,
- later continuation using session state.

### 4. `FunctionInvocationLayer` owns budget and loop stop semantics

The layer tracks run-local budget and stop conditions:

- maximum tool loop iterations,
- maximum total function calls,
- maximum consecutive function errors,
- final provider call with tool choice disabled,
- mutable run-local tool list,
- streaming and non-streaming execution paths.

This is a strong source candidate for Zyra's `tool result budget`, scheduler-aware tool-loop throttling, and fault-triggered retry/stop behavior.

### 5. Skills use progressive disclosure through runtime tools

The `_skills.py` index shows that `SkillsProvider` injects instructions and tools during `before_run`, then exposes at least these core operations:

- `load_skill`
- `read_skill_resource`
- `run_skill_script`

This means skills are not simply preloaded into the prompt. The model can discover, load, read resources, and run scripts through explicit runtime tools. Approval can be configured differently for reading skill content versus executing scripts.

For Zyra, this aligns with a `SkillRuntime` that keeps Markdown skills, resources, and script execution behind permissioned runtime entry points rather than dumping all skill material into context.

### 6. File skills include concrete safety checks

`FileSkillsSource` searches for `SKILL.md`, validates frontmatter, discovers resources and scripts, and validates resolved paths. The implementation includes:

- search-depth controls,
- extension filters,
- user-provided resource/script filters,
- containment checks under the skill directory,
- symlink detection below the skill directory,
- directory-name/frontmatter-name consistency checks.

This is useful for Zyra because the source already separates skill discovery, resource enumeration, and script enumeration. Zyra should still own its final permission and sandbox model, but the discovery and validation structure is worth adapting.

### 7. Skills can also come from MCP resources

The end of `_skills.py` implements an MCP-backed skill source:

- `MCPSkillsSource` reads `skill://index.json`.
- The index entries are converted into `MCPSkill` objects.
- `MCPSkill.get_content()` lazily reads the remote `SKILL.md`.
- `MCPSkill.get_resource()` resolves sibling resources against the skill root URI and rejects unsafe names such as absolute paths, URI schemes, and parent traversal.

This widens the Zyra target: `SkillRuntime` should not be designed as local-file-only. A clean target model would support local skills, code-defined skills, and MCP-served skills behind the same progressive-disclosure tool interface.

### 8. MCP is modeled as lifecycle-managed tool infrastructure

The `_mcp.py` index shows `MCPTool` and transport-specific variants for stdio, streamable HTTP, and websocket. The MCP layer includes:

- connection lifecycle,
- prompt loading,
- tool loading,
- sampling/logging callbacks,
- normal tool calls,
- task-style tool calls,
- task polling,
- task cancellation.

For Zyra, the useful part is not only MCP transport support. The stronger pattern is a managed tool source that can load, refresh, call, and cancel remote-capability tasks under runtime ownership.

Additional details from the first implementation pass:

- `MCPTool` runs lifecycle operations through an owner task and queue, so connect/close operations are serialized.
- `load_tools()` and `load_prompts()` handle pagination and convert remote declarations into local `FunctionTool` instances.
- Remote tools keep local/normalized/remote name metadata so allow-lists and collision checks do not rely only on display names.
- Server-initiated sampling is denied by default unless an approval callback is explicitly supplied. There are also max-token and max-request caps.
- `call_tool()` routes tools with `execution.taskSupport == "required"` through the long-running task path.
- The long-running path creates a task, polls `tasks/get`, fetches `tasks/result`, handles timeouts, reconnects once on connection loss where safe, and attempts best-effort remote cancellation.

For Zyra, this is a good source for MCP runtime semantics, but it should be adapted into Zyra event/state ownership rather than copied as an opaque MCP client wrapper.

### 9. Middleware has three relevant levels

The middleware file exposes separate concepts for:

- agent middleware,
- chat-client middleware,
- function/tool middleware.

The important architectural point is that middleware is categorized and inserted at the correct runtime layer, rather than being one undifferentiated hook list. `Agent.run()` and `FunctionInvocationLayer` both use these categories to decide where interception belongs.

For Zyra, this maps naturally to:

- session/task middleware,
- model-call middleware,
- tool-call middleware,
- scheduler/recovery middleware.

Important propagation detail:

- Agent and chat middleware pipelines suppress `MiddlewareTermination` and can return an existing/overridden result or an empty stream.
- Function middleware does not suppress `MiddlewareTermination`; it bubbles back to the tool loop so tool execution can terminate intentionally.
- `FunctionInvocationContext` also exposes a live mutable tool list through `add_tools()` and `remove_tools()`, enabling progressive tool exposure on the next model iteration.

This gives Zyra two reusable patterns: explicit termination semantics per layer, and run-local dynamic tool exposure.

## Source-To-Zyra Candidate Mapping

| Source mechanism | Source path | Zyra candidate | Why it matters |
|---|---|---|---|
| Function tool object | `_tools.py::FunctionTool` | `ToolRuntime` / `ToolPermissionRuntime` | schema, approval, limits, context injection, result normalization |
| Iterative tool loop | `_tools.py::FunctionInvocationLayer` | `CodeWorkerRuntime` query loop | owns max iterations, errors, tool-call replay, streaming/non-streaming handling |
| Approval request/resume | `_tools.py::_try_execute_function_calls` | permission session state | approval is represented in conversation/session state |
| Middleware termination | `_middleware.py`, `_tools.py` | recovery and HITL control | gives non-exceptional loop interruption semantics |
| Progressive tools | `_middleware.py::FunctionInvocationContext` | runtime tool registry | tools can be added/removed during one run for the next model iteration |
| Skills provider | `_skills.py::SkillsProvider` | `SkillRuntime` | progressive disclosure via runtime tools |
| File skills source | `_skills.py::FileSkillsSource` | local skill loader | discovery, frontmatter validation, path/symlink guardrails |
| MCP skills source | `_skills.py::MCPSkillsSource` | remote skill source | `skill://index.json` discovery and lazy remote resource reads |
| MCP lifecycle | `_mcp.py::MCPTool` | `MCPRuntime` / tool-source manager | connect/load/call/cancel remote tools |

## Open Reading Items

This batch has a usable first-pass source graph. Remaining edge-case review:

- `_skills.py`
  - script execution sandbox and approval flow
  - inline/class skill script argument binding details
- `_mcp.py`
  - full error conversion behavior in `_call_tool_with_retries`
  - transport subclasses and auth/header behavior
- `_middleware.py`
  - stream hook edge cases when middleware terminates without a result

## Interim Judgment

Batch 02 already changes the migration picture: `agent-framework` is a credible source for Zyra's permissioned tool loop, skill runtime, MCP runtime, and middleware layering. It should not be reduced to provider adapters.

The main caveat is that this framework's abstractions are provider/client oriented. Zyra should migrate the mechanisms into Zyra-owned runtime/session/event schemas, not copy the package boundary as-is.
