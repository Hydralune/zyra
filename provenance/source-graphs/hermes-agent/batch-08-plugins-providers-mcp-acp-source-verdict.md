# Batch 08 - Plugins / Providers / MCP / ACP / Source-to-Target Verdict

## 1. 阅读范围

本批是 Hermes Agent source graph 的收束批次，重点读取扩展运行时、provider 解析、MCP client/server、ACP adapter，以及这些机制对 Zyra 的 source-to-target 裁决。

已读主文件：

- `hermes_cli/plugins.py`
- `hermes_cli/plugins_cmd.py`
- `hermes_cli/middleware.py`
- `plugins/plugin_utils.py`
- `plugins/**` 目录结构 inventory
- `providers/base.py`
- `providers/__init__.py`
- `hermes_cli/providers.py`
- `hermes_cli/provider_catalog.py`
- `hermes_cli/runtime_provider.py`
- `hermes_cli/mcp_config.py`
- `hermes_cli/mcp_catalog.py`
- `hermes_cli/mcp_security.py`
- `hermes_cli/mcp_startup.py`
- `tools/mcp_tool.py`
- `tools/mcp_oauth.py`
- `tools/mcp_oauth_manager.py`
- `mcp_serve.py`
- `acp_adapter/auth.py`
- `acp_adapter/edit_approval.py`
- `acp_adapter/entry.py`
- `acp_adapter/events.py`
- `acp_adapter/permissions.py`
- `acp_adapter/provenance.py`
- `acp_adapter/server.py`
- `acp_adapter/session.py`
- `acp_adapter/tools.py`
- `acp_registry/agent.json`
- related tests：`tests/hermes_cli/test_plugins.py`、`tests/hermes_cli/test_project_plugin_rce_bypass.py`、provider/MCP tests、`tests/tools/test_mcp_*.py`、`tests/acp/**`、`tests/acp_adapter/**`。

没有逐个展开所有 bundled platform/image/video/memory/provider 插件实现。它们大多是第三方产品接入；本批只读取目录 inventory、注册边界、测试覆盖和代表性机制，避免把外部产品 adapter 误判为 Zyra 主路径内化对象。

## 2. 总体拓扑

Hermes 的扩展与外部协议层可以分成四条主链：

```text
plugin source
  -> manifest discovery
  -> opt-in / disabled / trusted override policy
  -> PluginContext registration
  -> tools / hooks / middleware / commands / providers / skills / platforms
  -> AIAgent tool loop / CLI slash / gateway / dashboard
```

```text
provider config / credential pool / custom provider
  -> provider profile / models.dev overlay / alias normalization
  -> runtime_provider.resolve_runtime_provider
  -> api_mode + base_url + auth material
  -> AIAgent transport
```

```text
mcp_servers config / ACP-provided MCP servers
  -> security validation + startup discovery
  -> MCPServerTask on dedicated event loop
  -> tool/resource/prompt registration in tool registry
  -> AIAgent tool executor
  -> OAuth/reconnect/refresh/elicitation/sampling
```

```text
ACP client
  -> acp_adapter.entry stdio server
  -> HermesACPAgent
  -> SessionManager-backed AIAgent
  -> tool/progress/permission/model/session updates
  -> editor client
```

这说明 Hermes 的扩展层不是松散插件集合，而是挂在同一个 tool registry、session state、permission runtime、provider resolver 和控制面 event stream 上。

## 3. Plugin Runtime

`hermes_cli/plugins.py` 是插件运行时核心。

主要 hook：

- `pre_tool_call`
- `post_tool_call`
- `transform_tool_result`
- `pre_llm_call`
- `post_llm_call`
- `transform_llm_output`
- API request hooks
- session hooks
- subagent hooks
- gateway dispatch hook
- approval lifecycle hooks
- kanban task lifecycle hooks

插件来源：

- bundled `plugins/`
- bundled `plugins/platforms/`
- user `~/.hermes/plugins`
- project `./.hermes/plugins`，默认禁用，只有 `HERMES_ENABLE_PROJECT_PLUGINS` truthy 才加载
- pip entry points `hermes_agent.plugins`

manifest `kind`：

- `standalone`
- `backend`
- `exclusive`
- `platform`
- `model-provider`

加载语义：

- disabled deny-list 总是优先。
- bundled backend 自动加载。
- platform 只注册 deferred loader，首次使用平台时再 import heavy SDK。
- standalone 默认 opt-in，通过 `plugins.enabled` 启用。
- `exclusive` memory provider 会被记录但不由普通 PluginManager 直接加载。
- `model-provider` 不由 PluginManager import，而由 provider discovery 处理。
- 后扫描来源覆盖前扫描来源，但会保留 trust/override policy。

`PluginContext` 暴露的注册面：

- `ctx.llm` host-owned LLM facade。
- `register_tool` 向全局 `tools.registry` 注册工具，并记录 plugin tool names。
- `register_cli_command` / `register_command` 注册 CLI 或 slash command，slash command 不允许覆盖 built-in。
- `dispatch_tool` 让插件内部调用 tool registry。
- `register_context_engine`。
- 注册 image/video/web/browser/TTS/transcription providers。
- `register_platform`。
- `register_auxiliary_task`。
- `register_hook` / `register_middleware`。
- `register_skill`，以 `<plugin>:<skill>` 明确命名空间注册，只通过 `skill_view` 显式读取，不进入 flat system prompt skills index。

工具覆盖边界：

- bundled plugin 视为 trusted。
- 非 bundled 插件覆盖 built-in tool 时，必须配置 `plugins.entries.<plugin_id>.allow_tool_override: true`。
- 插件 load 前后 snapshot registry/hook/middleware/command，用于 attribution、list 和 debug。
- plugin toolset 从 registry 反推出，不写死到静态 `toolsets.TOOLSETS`。

Hook 与 middleware 语义：

- `pre_llm_call` 可注入 context，但注入 user message，不写 system prompt，以保留 prompt cache。
- `pre_tool_call` 能返回 block message；thread tool whitelist 先于 plugin hook 生效。
- `pre_verify` 支持 Hermes 形态 `{"action":"continue"}`，也兼容 Claude Code 风格 `{"decision":"block"}`。
- async slash handler 在已有 event loop 时跑 helper thread，30 秒 timeout。
- middleware 类型包括 `tool_request`、`tool_execution`、`llm_request`、`llm_execution`。
- `apply_tool_request_middleware` 在 pre-tool hook、guardrail、approval 和 dispatch 前改写 args。
- execution middleware 是 chain-of-responsibility；`next_call` 单次使用。
- middleware 异常处理 fail-soft，但不掩盖 downstream 已经发生的异常。

安装与安全：

- `plugins_cmd.py` 支持 Git URL、GitHub shorthand、URL tree subdir、`.git/subdir`。
- `_resolve_subdir_within` 防 subdir escape。
- `_sanitize_plugin_name` 防 path traversal。
- install clone 到 temp，validate manifest version，再 move 到 `~/.hermes/plugins/<name>`。
- enable 时解析 canonical key/source；非 bundled plugin 申请工具 override 时会询问并写入 allow policy。
- dashboard update/remove 只允许 user plugin，bundled 不能删除。
- `test_project_plugin_rce_bypass.py` 覆盖 truthy env gate、API path sanitizer、project API route 拒绝和 static `.py` source 拒绝。

Zyra 判断：

- 高价值可迁移的是 plugin runtime contract，而不是 bundled plugin 目录本身。
- 应迁移为 `ExtensionRuntime`：manifest discovery、enable/disable policy、trusted override gate、tool/hook/middleware/command/provider/skill registration、plugin skill namespacing、hook/middleware execution order、debug/list attribution。
- 不宜迁移大批平台、image/video、dashboard product 插件和品牌化功能。它们可作为以后具体外部集成的来源，但不应成为 M1/M2 主路径对象。

## 4. Provider Runtime

Hermes provider 层分为 declarative profile、catalog、runtime resolver 三层。

`ProviderProfile`：

- identity：`name`、`aliases`、display metadata、signup URL。
- transport：`api_mode`、`base_url`、`models_url`、hostname。
- auth：`env_vars`、`auth_type`、health check。
- capability：vision、vision tool messages、fallback models。
- defaults：headers、temperature、max tokens、aux model。
- hooks：`prepare_messages`、`build_extra_body`、`build_api_kwargs_extras`、`get_max_tokens`、`fetch_models`。

Provider discovery：

- bundled provider 插件来自 `plugins/model-providers/<name>`。
- user provider 插件来自 `$HERMES_HOME/plugins/model-providers/<name>`。
- legacy single-file providers 仍支持。
- user provider 能覆盖 bundled provider。
- bundled module name 稳定，user module name 使用 `_hermes_user_provider_<name>` 避免冲突。

Catalog：

- `hermes_cli/providers.py` 以 models.dev 为基础，再叠加 Hermes overlay 和 user config。
- alias map 将 openai、kimi、copilot 等用户输入归一到 canonical provider。
- `determine_api_mode` 根据 provider、URL、path 推断 `codex_responses`、`anthropic_messages`、`bedrock_converse` 等 transport。
- `custom:<slug>` 支持用户自定义 provider。
- `provider_catalog.py` 面向 CLI/TUI/desktop 统一 provider picker，auth tab 分 `accounts` 和 `keys`。

`runtime_provider.resolve_runtime_provider` 是实际主路径：

- secret 读取走 `agent.secret_scope.get_secret`，不是直接 `os.getenv`。
- credential pool entry 可覆盖 provider/api_mode/base_url/model。
- custom provider 先查新 `providers:` dict，再兼容 legacy `custom_providers`。
- bare custom base URL 只有在明确 custom/alias 或 loopback 时才可信，避免 stale cloud URL 劫持。
- host-derived API key 只从可验证 host label 派生 `<VENDOR>_API_KEY`，并跳过 openai/openrouter/ollama 特殊分支，降低 lookalike credential leak。
- Anthropic base URL override 只允许官方、Azure、`/anthropic`、Kimi `/coding` 等安全形态。
- OpenRouter/custom/auto resolution 有 host-gated key selection，避免非 OpenRouter URL 误用 OpenRouter key。
- Azure Foundry 分支支持 Entra token provider、model-family api_mode inference、Anthropic endpoint path strip。
- auto 模式优先处理 non-cloud base URL，避免环境中的 cloud credential 盖过本地 provider。
- xAI/Qwen/Nous/Codex/Copilot/OAuth/Bedrock/API-key providers 都有专门分支。
- Copilot ACP external process 可作为 runtime provider 返回 command/args。

Zyra 判断：

- 高价值可迁移的是 provider resolution discipline。
- 应迁移 `ProviderRuntime`：declarative provider contract、provider catalog、secret-scope credential lookup、api_mode/transport 分离、credential pool、自定义 provider identity、host-gated base_url/key selection、provider/model switch 持久化。
- 不建议迁移全部 vendor provider 细节。Zyra 可先内化 resolver skeleton、OpenAI-compatible/custom/local/OAuth 几个主分支，再把具体 vendor plugin 作为后续扩展。

## 5. MCP Client Runtime

Hermes MCP client 由配置、启动发现、安全校验、连接任务、OAuth、tool registry refresh 组成。

配置与启动：

- `mcp_security.py` 扫 IOC、恶意 marker、网络 exfil、shell persistence、SSH/sudoers/PAM/cron/rc file 写入。
- `validate_mcp_server_entry` 在 config save、runtime load、dashboard/profile write、probe 路径中复用。
- `mcp_startup.py` 只有在配置含 `mcp_servers` 时启动 background discovery thread。
- 后台 discovery 使用 `suppress_interactive_oauth()`，不会弹浏览器或读 stdin。
- `wait_for_mcp_discovery(timeout)` bounded wait，供 CLI/TUI agent build 前短等。
- `mcp_config.py` 覆盖 `add/remove/list/test/login/reauth/configure/serve/catalog/install`。
- `mcp_catalog.py` 从 `optional-mcps/<name>/manifest.yaml` 安装 curated MCP，写 env/config，probe 后选择工具。

核心 event loop：

```text
mcp_servers config
  -> mcp_security validation
  -> startup background discovery
  -> dedicated MCP event loop
  -> MCPServerTask
  -> stdio/http/sse connect
  -> initialize + list_tools
  -> schema repair + include/exclude + capability gate
  -> registry toolset mcp-<server>
  -> AIAgent tool executor
```

`tools/mcp_tool.py` 创建进程级 dedicated MCP event loop；每个 server 对应 long-lived `MCPServerTask`。stdio/http/sse transport 都在该 task 内打开和关闭，避免 anyio context 被跨 task cleanup。同步 tool handler 通过 `_run_on_mcp_loop` 投递 coroutine。

`MCPServerTask` 关键状态：

- `session`
- `_tools`
- `_registered_tool_names`
- `_auth_type`
- `_rpc_lock`
- `_refresh_lock`
- `_pending_call_context`
- `initialize_result`
- ping unsupported latch

生命周期：

- stdio safe env allowlist，stderr 写 per-profile `mcp-stderr.log`，跟踪 child PID/PGID。
- stdio command 经过 filtered PATH resolution。
- runtime 启动前有 best-effort OSV malware preflight，12 秒上限，fail-open。
- HTTP 支持 content-type preflight、OAuth、mTLS client cert、SSE、streamable HTTP、cross-origin redirect Authorization strip。
- initial retry budget 和 reconnect budget 分离；auth error 初始阶段不盲重试。
- retry 用尽后 deregister tools 并 park，等待 reconnect signal。
- shutdown 并行关闭 server；loop 停止后 best-effort reap orphan stdio child/pgroup。

工具注册：

```text
list_tools
  -> _normalize_mcp_input_schema
  -> _convert_mcp_schema
  -> include/exclude filter
  -> prompt-injection description scan
  -> built-in collision guard
  -> registry.register(toolset=mcp-<server>)
  -> provenance map mcp_tool_name -> server
```

schema normalizer 修复：

- `definitions` -> `$defs`
- `#/definitions/...` -> `#/$defs/...`
- object 缺 type 时补 `type: object`
- object 缺 properties 时补 `{}`
- prune `required` 中不存在于 properties 的字段
- nullable union 折叠成 non-null branch，保留 nullable hint

utility tools：

- `mcp_<server>_list_resources`
- `mcp_<server>_read_resource`
- `mcp_<server>_list_prompts`
- `mcp_<server>_get_prompt`

utility registration 受 config `tools.resources` / `tools.prompts` 和 initialize advertised capabilities 双 gate。ClientSession 方法存在不再被当作能力证据，避免 tools-only server 被注册出会 32601 的 stub。

动态刷新：

- `tools/list_changed` notification 触发 `_refresh_tools`。
- refresh deregister stale、register fresh、记录 diff。
- `refresh_agent_mcp_tools` 用 registry generation 防 stale publish。
- rebuild agent snapshot 时重新注入 memory provider tools 和 context-engine tools，避免刷新 MCP 时误删 post-build tool families。
- `supports_parallel_tool_calls: true` 是 server 级 opt-in；parallel safety 用 registration provenance map，不靠名字前缀猜 server。

Sampling / elicitation：

- `SamplingHandler` 支持 max RPM、timeout、max tokens cap、max tool rounds、allowed models。
- MCP SamplingMessage 转 OpenAI message format，支持 text/image/tool-use/tool-result。
- 使用 `agent.auxiliary_client.call_llm(task="mcp")`，受 timeout 约束。
- 支持 server-provided tools 和 bounded tool loop。
- `ElicitationHandler` 处理 `elicitation/create`；URL mode declined，form mode 走 `tools.approval.request_elicitation_consent`。
- elicitation replay `MCPServerTask._pending_call_context` 的 contextvars，确保 gateway session/platform routing 不丢；异常 fail-closed。

OAuth：

- `mcp_oauth.py` 实现 OAuth 2.1 + PKCE glue。
- token、client registration、OAuth metadata 分别存到 `HERMES_HOME/mcp-tokens/<server>.json`、`.client.json`、`.meta.json`。
- JSON 写入用 restricted permission + atomic replace，并 secure parent dir。
- token 存 absolute `expires_at`，冷启动时重算剩余 TTL，修复进程重启后 expired token 被误判 valid。
- metadata 持久化，避免冷启动 refresh 错猜 `{server_url}/token`。
- 非交互且无 cached token 时 fail fast；后台 discovery 通过 ContextVar 禁用交互。
- 交互模式支持浏览器、SSH port-forward 提示、粘贴 redirect URL fallback、用户 skip。
- `mcp_oauth_manager.py` 是 process-wide manager：provider cache、URL change rebuild、disk mtime reload、401 dedup、invalid_client poison、reconnect signalling。
- manager subclass MCP SDK `OAuthClientProvider`，在 auth flow 前做 disk-watch，冷启动预取 metadata，结束后持久化 metadata，并桥接 async generator 的 `.asend(response)`。

Zyra 判断：

- MCP 是 Hermes 对 Zyra M1 最直接的高价值来源之一。
- 应迁移为 `MCPRuntime`、`MCPConfigRuntime`、`MCPAuthRuntime`：server task lifecycle、schema sanitizer、tool/resource/prompt registration、include/exclude policy、capability gating、dynamic refresh、OAuth persistence、401 dedup、sampling bridge、elicitation bridge、stdio child custody、HTTP/SSE/mTLS policy。
- 必须 Zyra 化：MCP config 进入 Zyra schema/store；tool execution 写 Zyra event log/tool call/permission trace；elicitation 走 Zyra approval/control command；OAuth storage 进入 Zyra secret/session custody；refresh 更新 Zyra runtime session tool snapshot。
- 不应迁移 Hermes profile/HERMES_HOME path shape、CLI 文案和 dashboard forms。

## 6. Hermes as MCP Server

`mcp_serve.py` 是另一方向：把 Hermes 暴露给外部 MCP client。

工具面：

- `conversations_list`
- `conversation_get`
- `messages_read`
- `attachments_fetch`
- `events_poll`
- `events_wait`
- `messages_send`
- `channels_list`
- `permissions_list_open`
- `permissions_respond`

运行方式：

- `hermes mcp serve` 启动 stdio FastMCP server。
- `EventBridge` 后台线程轮询 `state.db` 和 `sessions.json`。
- 事件队列是进程内 ring buffer，上限 1000。
- `events_wait` 是 long-poll。
- `messages_send` 直接调用 `tools.send_message_tool`。
- `permissions_respond` 只对本 bridge 观察到的 pending approval 做 best-effort resolve。

Zyra 判断：

- 可作为 `ZyraMcpServer` 外部控制桥参考：session/message/artifact/event/approval 作为 MCP tools 暴露，event cursor / long-poll model 可复用思想。
- 不应迁移 DB polling 实现。Zyra 应从自身 event log、artifact store、control command 读取。

## 7. ACP Adapter

ACP adapter 是 Hermes 面向 editor client 的协议层，比 `mcp_serve.py` 更接近 Zyra M2 控制台需要的语义。

Entry / auth：

- `entry.py` harden import path，stdout 保留给 ACP JSON-RPC，日志写 stderr。
- 加载 `~/.hermes/.env`。
- 支持 `--check`、`--setup`、`--setup-browser`。
- 启动前执行 `discover_mcp_tools()`，避免 lazy import 冻结 gateway loop。
- 对 `ping`/`health` 探活导致的 method-not-found traceback 做 stderr 降噪，但协议错误响应仍保留。
- `auth.py` 通过 `resolve_runtime_provider()` 判断是否已有 runtime credential；callable `api_key` 也算有效 credential。
- 初始化时总是 advertised terminal setup auth；如果 provider 已配置，再 advertised provider auth method。

Session manager：

- `SessionState` 持有 ACP session id、AIAgent、cwd、model、history、cancel_event、runtime_lock、queued prompts、current/interrupted prompt。
- `SessionManager` 同时维护内存 session 和 `SessionDB` 持久化。
- `create_session` 创建 fresh `AIAgent`，source=`acp`，并注册 task-local cwd。
- `get_session` 缓存 miss 时从 DB restore。
- `fork_session` deep-copy history 到新 session。
- `list_sessions` 合并内存和 DB，支持 cwd filter、title/preview/updated_at。
- `_persist` 使用 `replace_messages` 原子替换历史，避免半写坏库。
- `_restore` 从 DB 恢复 history、model、provider、base_url、api_mode，并重新构造 AIAgent。
- `_make_agent` 使用 `platform="acp"`、`enabled_toolsets=["hermes-acp", "mcp-..."]`、quiet mode、shared session DB、runtime provider resolution。
- Windows path 在 WSL 中转 `/mnt/<drive>/...`，避免 editor cwd 与 tool cwd 不一致。

Server / prompt runtime：

- `initialize` advertised load/fork/list/resume、image prompt capability、auth methods。
- `new_session/load_session/resume_session/fork_session/list_sessions` 对应 ACP session lifecycle。
- `load_session` 与 `resume_session` 在响应前 replay full transcript。
- history replay 包括 user/assistant message、reasoning/thought、tool start/complete、todo plan update。
- session 可以接受 ACP-provided MCP servers，转换成 Hermes MCP config map 后注册，并 refresh agent tool surface。
- model selector 用 provider/model encoding，保留 provider context。
- mode selector 把 edit approval policy 映射成 `default`、`accept_edits`、`dont_ask`。

Prompt 主链：

```text
ACP prompt blocks
  -> text/image/resource/embedded resource conversion
  -> slash command interception
  -> busy? queue prompt
  -> mark running
  -> attach callbacks
  -> executor thread + contextvars.copy_context
  -> agent.run_conversation
  -> persist history
  -> provenance/session info update
  -> queued prompt drain
  -> usage update
  -> PromptResponse(stop_reason, usage)
```

关键语义：

- file/image resource 会 inline 成 OpenAI-style multimodal content；超过 512KB 的资源截断或拒绝 inline。
- `/steer` 在 idle session 中会 rewrite 为普通 prompt，或 salvage 被取消的上一个 prompt。
- 同 session 正在运行时，新普通 prompt 进入 FIFO queue，不并发跑同一个 history。
- `cancel` 同时设置 cancel_event，并调用 `agent.interrupt()`。
- run_conversation 在 ThreadPoolExecutor 中执行；approval callback、interactive context、session vars、edit approval requester 都在 executor thread 内设置。
- 使用 contextvars 隔离并发 ACP session，避免 reused executor thread 泄漏 session key、sudo cache、approval context。
- `HERMES_SESSION_ID` 会围绕一次 run 保存恢复，供 kanban 等工具标注 side effect。
- 如果 compression 导致内部 Hermes session id 旋转，会发送 `_meta.hermes.sessionProvenance`。
- final response 如果已经 stream 过通常不重复发；如果 plugin transform 改写 final response，会补发。

Tool / event / permission：

- `events.py` 将 tool started -> ACP `ToolCallStart`，step callback prev_tools -> ACP completion update。
- same-name parallel tool call 用 per-tool FIFO queue 配对 tool call id。
- todo result 解析成 ACP native `plan` update。
- thinking/reasoning/message stream 分别推给 ACP thought/message updates。
- `tools.py` 把 Hermes tool 映射为 ACP `read/edit/search/execute/fetch/think/other`。
- completion content 对 todo/read/search/execute/process/delegate/session_search/memory/skill/web/browser/media/cron 做结构化渲染，不直接 dump raw JSON。
- edit/diff 可转换为 ACP diff content block。
- terminal/dangerous command approval 通过 ACP `request_permission`，timeout/异常/未知 option 默认 deny。
- edit approval requester 通过 ContextVar 绑定到当前 ACP run；支持 `write_file` 和 `patch replace`；sensitive path 自动审批禁用。
- `provenance.py` 从 compression chain 推导 `_meta.hermes.sessionProvenance`，不新增持久化状态。

Zyra 判断：

- ACP adapter 是 Zyra 控制台/API 协议层的重要来源。
- 应迁移机制：`ExternalClientProtocolRuntime`、`SessionProtocolAdapter`、`PromptBlockConverter`、`ToolEventProjector`、`PlanUpdateProjector`、`PermissionBridge`、`EditApprovalBridge`、`ModelModeControl`、`SessionHistoryReplay`、`SessionProvenanceMeta`。
- 必须 Zyra 化：SessionManager 不持有 Hermes AIAgent，而持有 Zyra runtime；history persistence 写 Zyra event log/message/artifact store；tool events 从 Zyra spans 投影；edit approval 走 Zyra permission runtime；model/mode switch 写 Zyra control command 与 session state。
- 不宜迁移 Zed 特定显示 workaround、Hermes provider setup/browser install 文案，也不应让 Zyra 主 runtime 变成 ACP-only。

## 8. 测试证据

插件：

- `tests/hermes_cli/test_plugins.py` 覆盖 discovery/loading/hooks/pre_tool_call/pre_verify/thread whitelist/context/tool visibility/plugin commands/dispatch/debug/profile。
- `tests/hermes_cli/test_project_plugin_rce_bypass.py` 覆盖 project plugin env gate、API path sanitizer、untrusted API route 拒绝和 PoC block。

Provider：

- `tests/hermes_cli/test_runtime_provider_resolution.py`
- `tests/hermes_cli/test_provider_catalog.py`
- `tests/hermes_cli/test_provider_precedence.py`
- `tests/hermes_cli/test_provider_parity.py`
- `tests/hermes_cli/test_custom_provider_identity.py`
- `tests/hermes_cli/test_model_switch_custom_providers.py`
- 多个 vendor/provider auth tests。

MCP：

- `tests/tools/test_mcp_tool.py` 覆盖 config/status/schema/check/run-on-loop/tool handler/server task/toolset/shutdown/safe env/http/reconnect/timeout/utility/sampling/selective loading/collision/parallel safety。
- `tests/tools/test_mcp_oauth_manager.py` 覆盖 singleton/cache/url change/remove/disk watch/401 dedup/invalid_client poison/pre-registered no poison/bidirectional auth flow。
- `tests/tools/test_mcp_capability_gating.py`、`test_mcp_dynamic_discovery.py`、`test_mcp_elicitation.py`、`test_mcp_client_cert.py`、`test_mcp_reconnect_signal.py`、`test_mcp_utility_capability_gating.py`、`test_refresh_agent_mcp_tools.py` 覆盖关键边界。
- `tests/hermes_cli/test_mcp_security.py`、`test_mcp_startup.py`、`test_mcp_config.py`、`test_mcp_catalog.py` 覆盖 CLI/config/startup/security。

ACP：

- `tests/acp/test_server.py` 覆盖 initialize/auth/session/mode/model/prompt/history replay/usage/tool updates。
- `tests/acp/test_mcp_e2e.py` 覆盖 ACP-provided MCP servers -> register -> prompt -> tool events。
- `tests/acp/test_permissions.py`、`test_edit_approval.py`、`test_approval_isolation.py` 覆盖 permission/edit approval/context isolation。
- `tests/acp/test_session.py`、`test_session_provenance.py`、`test_events.py`、`test_tools.py` 覆盖 session persistence/provenance/event/tool projection。
- `tests/acp_adapter/test_acp_images.py`、`test_acp_commands.py`、`test_detect_provider_entra.py` 覆盖 prompt blocks、commands 和 provider detection。

## 9. Source-to-Target 总裁决

强迁移：

- turn lifecycle / tool loop / result budget / compression discipline：Batch01。
- tool registry / toolsets / approval / tool search bridge：Batch02。
- SessionDB / context compression / memory prefetch / skill memory：Batch03。
- slash/control command / skills lifecycle：Batch04。
- gateway run / messaging active guard / blocking prompt / suspended resume：Batch05。
- delegation / async completion / cron / kanban worker dispatch：Batch06。
- JSON-RPC control plane / event protocol / PTY bridge / UI event reducer：Batch07。
- plugin extension runtime / provider runtime / MCP runtime / ACP protocol adapter：Batch08。

选择性迁移：

- `run_agent.py`
- `gateway/run.py`
- `tui_gateway/server.py`
- `hermes_cli/web_server.py`
- `tools/mcp_tool.py`
- `hermes_cli/runtime_provider.py`
- `acp_adapter/server.py`
- `hermes_cli/plugins.py`

这些文件机制成熟，但职责过宽。Zyra 应拆成 runtime、registry、permission、session、event、scheduler、adapter、control API、UI projector，而不是复制上游巨文件边界。

参考或暂缓：

- Hermes product plugins：pet、achievements、billing、voice、branding。
- 大量 messaging platform adapters，除非 Zyra 明确要做某个平台接入。
- image/video/web provider 具体 vendor adapters。
- Electron packaging/updater/native system integration。
- hosted dashboard OAuth/ticket 全套产品化体系。
- Hermes `HERMES_HOME` profile path model。

## 10. 对 Zyra 执行文档的影响

Hermes 读完后，M1/M2 slice 文档应避免继续把 Hermes 写成“参考灵感”。更准确的口径是：

- Hermes 是 `ToolRuntime`、`ExtensionRuntime`、`ProviderRuntime`、`MCPRuntime`、`ControlCommandRuntime`、`LongTaskRuntime`、`ACP/ExternalProtocolAdapter` 的高价值来源。
- 代码迁移时不应复制上游目录形状，而应让 Zyra schema、event log、permission、scheduler、worker、artifact/control command 承担运行责任。
- 每个引用 Hermes 的 slice 都要标明：来源机制、目标 Zyra 模块、运行入口、event/control/API/UI 接入点、断开即失败验证。

## 11. Batch08 自检

已覆盖的验收问题：

- plugin hook、provider profile、MCP dynamic tools、ACP adapter 的边界已拆清。
- 已区分 extension runtime contract 与 bundled product plugin。
- MCP client 与 Hermes-as-MCP-server 两个方向已分开裁决。
- ACP adapter 的 session/prompt/tool/permission/history/provenance 主路径已读到代码级。
- 已给出 Hermes 对 Zyra 的 source-to-target 总裁决。

未继续深读的内容及原因：

- 各 vendor model/image/video/web provider 的业务细节未逐个读；它们不是当前 source graph 的架构主链。
- 各 messaging platform plugin 未逐个读；Batch05 已覆盖 gateway/platform 抽象，具体平台 adapter 以后按需要单独迁移。
- MCP SDK 内部未读；Hermes 代码已经清楚展示它如何接入 SDK。

结论：Hermes Agent 的链路级 source graph 第一轮可以判定完成。后续若进入实现阶段，应按目标 slice 重新打开对应来源文件做局部二次精读，而不是把这份 source graph 当成可直接替代实现审查的证据。
