# Batch 05：MCP runtime / tools / resources / prompts / auth source graph

日期：2026-07-07  
范围：`claude-code-best` MCP 链路深读。  
状态：已完成本批只读探索；未修改 `zyra` 生产代码。

## 本批结论

MCP 在 Claude Code 中不是一个边缘 client，也不是只在 `/mcp` 面板里存在的配置功能。它同时进入：

- tool pool：MCP server 的 tool 被转换成标准 `Tool`，参与 `query()` 的工具选择、permission、并发、安全分类和结果预算。
- resource/prompt surface：resources 通过 `ListMcpResourcesTool` / `ReadMcpResourceTool` 暴露，prompts 被转换成 slash command。
- auth lifecycle：远程 server 的 OAuth、XAA、step-up scope、needs-auth cache 会改变工具是否出现在模型可用工具池中。
- prompt/cache behavior：server instructions 既可以进 system prompt，也可以通过 `mcp_instructions_delta` attachment 进入会话，避免 late connect 打破 prompt cache。
- app state/control UI：`useManageMCPConnections()` 持有连接状态、重连、enabled/disabled、list_changed refresh、elicitation UI、channel notification 和 permission relay。
- policy boundary：enterprise managed config、project `.mcp.json` approval、server allow/deny policy、channel allowlist 和 plugin MCP 去重共同决定 server 是否可用。

因此 Zyra 不能把 MCP 内化成一个“可调用外部 MCP SDK”的薄 wrapper。后续需要至少拆成：

1. `McpConfigStore`：配置来源、scope、policy、dedupe、project approval、plugin/managed merge。
2. `McpConnectionRuntime`：transport 连接、状态机、缓存、重连、list_changed refresh。
3. `McpAuthRuntime`：OAuth/XAA/token/step-up/needs-auth 状态。
4. `McpToolProjection`：MCP tool/resource/prompt 到 Zyra Tool/ControlCommand/Artifact 的投影。
5. `McpElicitationRuntime`：elicitation + result hooks + UI/API approval queue。
6. `McpPromptInstructionRuntime`：server instructions delta 与 session memory/compact restore 的接入。

## 已读源码

### MCP config / policy / utility

- `src/services/mcp/types.ts`
- `src/services/mcp/config.ts`
- `src/services/mcp/envExpansion.ts`
- `src/services/mcp/normalization.ts`
- `src/services/mcp/utils.ts`
- `src/services/mcp/mcpStringUtils.ts`
- `src/services/mcp/headersHelper.ts`
- `src/services/mcp/officialRegistry.ts`
- `src/components/MCPServerApprovalDialog.tsx`

### MCP connection / runtime lifecycle

- `src/services/mcp/client.ts`
- `src/services/mcp/MCPConnectionManager.tsx`
- `src/services/mcp/useManageMCPConnections.ts`
- `src/services/mcp/InProcessTransport.ts`
- `src/services/mcp/SdkControlTransport.ts`
- `src/services/mcp/vscodeSdkMcp.ts`
- `src/utils/mcpWebSocketTransport.ts`
- `src/services/mcp/claudeai.ts`

### MCP auth / XAA

- `src/services/mcp/auth.ts`
- `src/services/mcp/oauthPort.ts`
- `src/services/mcp/xaa.ts`
- `src/services/mcp/xaaIdpLogin.ts`

### MCP tools/resources/prompts

- `src/tools/MCPTool/MCPTool.ts`
- `src/tools/MCPTool/prompt.ts`
- `src/tools/MCPTool/classifyForCollapse.ts`
- `src/tools/ListMcpResourcesTool/ListMcpResourcesTool.ts`
- `src/tools/ListMcpResourcesTool/prompt.ts`
- `src/tools/ReadMcpResourceTool/ReadMcpResourceTool.ts`
- `src/tools/ReadMcpResourceTool/prompt.ts`
- `src/tools/McpAuthTool/McpAuthTool.ts`
- `src/utils/mcpOutputStorage.ts`
- `src/utils/mcpValidation.ts`

### MCP commands / prompt instructions

- `src/commands/mcp/index.ts`
- `src/commands/mcp/addCommand.ts`
- `src/commands/mcp/mcp.tsx`
- `src/commands/mcp/xaaIdpCommand.ts`
- `src/constants/prompts.ts` 相关 `getMcpInstructions*` 片段
- `src/utils/attachments.ts` 相关 `mcp_instructions_delta` 片段
- `src/utils/mcpInstructionsDelta.ts`

### Channel / notification / permission relay

- `src/services/mcp/channelAllowlist.ts`
- `src/services/mcp/channelNotification.ts`
- `src/services/mcp/channelPermissions.ts`
- `src/services/mcp/elicitationHandler.ts`

## L6.1：配置、scope、policy 与去重

### 类型层

`src/services/mcp/types.ts` 定义了 MCP runtime 的几组关键状态：

- server scope：`local`、`user`、`project`、`dynamic`、`enterprise`、`claudeai`、`managed`。
- transport：`stdio`、`sse`、`sse-ide`、`http`、`ws`、`sdk`、`claudeai-proxy`，schema 中也出现 `ws-ide`。
- connection state：`connected`、`failed`、`needs-auth`、`pending`、`disabled`。
- CLI/API state：`MCPCliState` 会序列化 clients/configs/tools/resources/normalized names，供 UI/CLI 状态消费。

Zyra 迁移时不能只保存 server URL。scope、policy、connection state、tool/resource snapshots、normalized name map 都是运行时行为输入。

### merge order 与 source precedence

`src/services/mcp/config.ts` 是配置 source-of-truth：

- enterprise managed config 路径为 `managed-mcp.json`。一旦存在，enterprise config 独占所有 MCP server 配置，不再合并 user/project/local/plugin。
- 非 enterprise 路径下，先加载 user/project/local，再加载 plugin MCP，再合并 claude.ai connector。
- plugin MCP 与手工 MCP 按 server signature 去重；手工配置优先。
- claude.ai connectors 也按内容去重；与显式启用的手工 server 冲突时，手工 server 优先。
- project `.mcp.json` 一般从 root 到 cwd 逐层遍历，离 cwd 更近的配置覆盖更远的配置。
- `mcp add` 写 project scope 时只写当前目录 `.mcp.json`，并保留 `permissions` 字段，避免覆盖 project 文件中的其它 MCP 权限信息。

这意味着 Zyra 的 `McpConfigStore` 需要记录 config 来源层级和覆盖关系。只存最终 server map 会丢失可解释性：用户在 UI 里看到的“为什么这个 server 生效/被遮蔽”无法还原。

### server signature

`config.ts` 对 server 去重使用 signature：

- stdio：command + args 等命令数组。
- remote URL：URL。
- Claude Code Router proxy 会读取 header 中的原始 `mcp_url`，用原始 MCP URL 去重。

这对 Zyra 有迁移价值：不同来源生成的同一 server 不应进入 tool pool 两次。否则工具名、permission rule、resource list 和 prompt cache 都会重复。

### policy

enterprise policy 支持：

- server name allow/deny。
- stdio command array allow/deny。
- URL wildcard allow/deny。
- denylist 优先。
- SDK/in-process server 在 `filterMcpServersByPolicy()` 中被豁免。

`addMcpConfig()` 也会校验：

- server name/config 合法。
- 不能使用保留 server name，例如 Chrome / computer-use 相关内置名。
- enterprise exclusive mode 下不能写普通 MCP config。
- policy deny/allow。
- target scope 是否已有 duplicate。

Zyra 不能只在连接时失败；配置写入时也应该有 policy validation 和可解释错误。

### project approval

`MCPServerApprovalDialog.tsx` 处理 `.mcp.json` 中新发现 project MCP server：

- `yes`：写入 `localSettings.enabledMcpjsonServers`。
- `yes_all`：写入 server 并设置 `enableAllProjectMcpServers`。
- `no`：写入 `localSettings.disabledMcpjsonServers`。

`utils.ts` 的 `getProjectMcpServerStatus()` 进一步决定 pending/disabled/auto-approved：

- project MCP 默认 pending，除非在 enabled/rejected setting 中。
- dangerous bypass 模式只有在用户/local/flag/policy 允许 skip prompt 时才会自动 approve，不会直接信任 project setting。
- non-interactive 场景可以基于 projectSettings enabled 自动 approve。

Zyra 应把 project MCP approval 纳入 permission/control-command 体系，而不是仅当作 UI local state。

### env / header expansion

`envExpansion.ts` 支持：

- `${VAR}`
- `${VAR:-default}`
- missing env warnings

`headersHelper.ts` 支持：

- static headers 与 dynamic header helper。
- dynamic header helper 输出会覆盖 static header。
- project/local config 中的 helper command 需要 trust check，non-interactive 例外路径不同。
- helper 失败被记录，但不一定阻断连接。

迁移时要注意：dynamic headers 是可执行命令，不是纯配置。它与 workspace trust / command permission 有交叉。

## L6.2：连接生命周期与 transport

### connection entry

`src/services/mcp/client.ts` 是 MCP runtime 主体。它负责：

- 连接不同 transport。
- 构造 SDK client。
- 获取 tools/resources/prompts/skills。
- 将 MCP tool 转成 Claude Code `Tool`。
- 处理远程 OAuth/needs-auth。
- 维护连接 cache、transport cleanup、session expiry retry。

支持 transport：

- `stdio`
- `sse`
- `sse-ide`
- `ws`
- `ws-ide`
- `http`
- `sdk`
- `claudeai-proxy`
- in-process Chrome / computer-use server via `InProcessTransport`

连接批量策略：

- local batch size：3。
- remote batch size：20。
- connection timeout 默认 30 秒。
- connection cache key 是 server name + serialized config。

Zyra 迁移时要把“连接并发”和“工具执行并发”分开。MCP server discovery 可以高并发；工具执行仍由 tool flags 和 permission 约束。

### remote auth state

`client.ts` 对 remote server 有 needs-auth cache：

- 文件名：`mcp-needs-auth-cache.json`。
- TTL：15 分钟。
- remote auth failure 会返回 `needs-auth` connection 并写 cache。
- discovery 发现需要 token 但本地无 token 时，可以跳过无意义 401 探测，直接暴露 `McpAuthTool`。

Zyra 需要 `needs_auth` 作为一等 connection state。否则 tool pool 会在每轮 query 中反复尝试连接失败 server，污染延迟与日志。

### HTTP/SSE fetch wrapper

`wrapFetchWithTimeout()` 行为：

- POST 每次请求有 fresh timeout。
- GET SSE 长连接无普通请求 timeout。
- 强制设置 `Accept: application/json, text/event-stream`。

`createClaudeAiProxyFetch()`：

- 为 claude.ai proxy 请求附加 OAuth bearer token。
- 401 时只有 token 发生变化或刷新成功才 retry 一次。

迁移价值：MCP remote 不是普通 HTTP client。SSE、session token、OAuth refresh、proxy retry 的语义必须被保存，否则远程 server 会出现“偶尔连接上，但续期/重连失败”的隐性问题。

### root / elicitation capabilities

Claude Code MCP client 声明 capabilities：

- `roots`：`ListRoots` 返回 original cwd。
- `elicitation`：server 可以发起 structured elicitation。

这意味着 MCP server 可以依赖 workspace root 语义；Zyra 不能在 sidecar 中随便改变 cwd 或只返回 process cwd。

### cleanup 与 reconnect

`client.ts` cleanup：

- stdio 先 `SIGINT`，再 `SIGTERM`，最后 `SIGKILL`。
- 所有 transport 都注册 cleanup。
- error/onclose 会清理 connection cache 和 fetch cache。
- remote terminal errors 达到阈值会 close transport。
- HTTP session expiry 通过 JSON-RPC 404 / `-32001` 或 closed connection 判定，清 cache 后重连。

`useManageMCPConnections.ts` lifecycle：

- connected client 注册 elicitation handler。
- remote transport onclose 后指数退避自动 reconnect，最多 5 次。
- stdio/sdk 不自动 reconnect。
- list_changed handlers 会刷新 tools/prompts/resources/skills。
- resources_changed 会刷新 MCP skills 并清 skill search cache。
- toggle server 会先持久化 enabled/disabled，再断开或重连。
- stale plugin/config-changed clients 会被移除，旧 tools/commands/resources 被剪掉。

Zyra 的 MCP runtime 必须有 event-driven refresh；只在 session start 拉一次工具列表不够。

### in-process / sdk / websocket

`InProcessTransport.ts` 是同进程 client/server transport，用 linked callbacks 传 JSON-RPC message。

`SdkControlTransport.ts` 是 CLI 和 SDK 之间的 JSONRPC bridge，通过 control messages 发收。

`mcpWebSocketTransport.ts`：

- 同时支持 Bun native WebSocket 和 Node `ws`。
- `start()` 只能调用一次。
- message 会 JSON parse 并过 `JSONRPCMessageSchema`。
- close 时清理 listeners。
- send 前检查 socket open。

Zyra 的 Claude-derived MCP 主链默认保留 TypeScript 并裁剪迁入正式 package；transport 细节按采用范围迁移，不要求逐行复制，Python 控制平面只承担 typed boundary，不替代 TypeScript MCP core；以下状态语义必须保留：start-once、schema validate、close cleanup、send open check、transport-specific retry。

## L6.3：tool/resource/prompt projection

### MCP tool 到 Tool contract

`client.ts` 的 `fetchToolsForClient()` 将 MCP tool 转为 Claude Code `Tool`：

- tool name：一般为 `mcp__server__tool`；SDK no-prefix 环境例外。
- `mcpInfo` 保留 server/tool 原始信息。
- `description` truncation 上限约 2048 chars。
- `inputSchema` 直接来自 MCP schema。
- `searchHint`、`alwaysLoad` 从 MCP annotations/metadata 推导。
- `isConcurrencySafe` / `isReadOnly` 来自 `readOnlyHint`。
- `isDestructive` 来自 `destructiveHint`。
- `isOpenWorld` 来自 `openWorldHint`。
- classifier 输入会 stringify。
- `checkPermissions` 走 passthrough，但会提示可以通过 allow rule 持久授权。
- `call()` 调 `callMCPToolWithUrlElicitationRetry()`，带进度事件、session-expired retry、`mcpMeta` 和 `structuredContent`。

Zyra 迁移时必须保留 annotations 到 Tool flags 的映射。否则 scheduler/permission/concurrency 会把 MCP 工具当成未知工具处理，安全性和性能都会退化。

### output budget / persistence

`src/utils/mcpValidation.ts` 定义 MCP output token cap：

- 优先 `MAX_MCP_OUTPUT_TOKENS`。
- 其次 feature flag `tengu_satin_quoll.mcp_tool`。
- 默认 25,000 tokens。
- 小输出用粗估避开 API token count。
- 接近阈值后调用 token estimation API。
- text 直接截断并附 truncation message。
- content blocks 会按 text/image 估算截断；image 可尝试压缩。

`src/utils/mcpOutputStorage.ts`：

- 二进制 content 根据 MIME 映射扩展名。
- binary 写入 tool-results 目录，不 stringifies。
- 大输出文件 instruction 会要求模型按 offset/limit 读完整文件再总结。
- unknown MIME 保守写 `.bin`。

`client.ts` 的 `processMCPResult()` 会处理：

- `toolResult`
- `structuredContent`
- `contentArray`
- text/audio/image/resource/resource_link block 转 Anthropic content blocks。
- blob/image/resource 可能持久化到文件。
- 大输出如果开启保存则落盘，否则截断。

这条链路直接影响 Zyra artifact schema：MCP tool result 不只是字符串，可能是 text blocks、image blocks、binary artifacts、resource links、structured JSON 和 truncation warning。

### resource tools

`ListMcpResourcesTool`：

- deferred tool。
- read-only。
- concurrency-safe。
- 可以列出所有 connected server 或单个 server 的 resources。
- 使用 `ensureConnectedClient()` 和 cached `fetchResourcesForClient()`。

`ReadMcpResourceTool`：

- deferred tool。
- read-only。
- 调 `resources/read`。
- binary blob 会持久化。
- text/path 返回给模型。

`reconnectMcpServerImpl()` 与 `getMcpToolsCommandsAndResources()` 只在 resources supported 时把 List/Read MCP resource tools 加入 tool pool 一次。

Zyra 的 tool registry 需要支持“工具由 MCP server capabilities 动态启用”。不能静态注册所有 MCP resource 工具，否则缺少 server 时会暴露空能力。

### prompts 到 commands

`fetchCommandsForClient()` 把 MCP `prompts/list` 转成 `Command`：

- command name 形如 `mcp__server__prompt`。
- `getPromptForCommand()` 调 `client.getPrompt()`。
- prompt content 经 `transformResultContent()` 转成 Claude Code message content。

这说明 MCP prompt 是 slash command/control command 的来源之一。Zyra 后续控制台如果支持 MCP prompts，需要把 prompt invocation 接到同一个 command execution graph，而不是绕开 query loop。

### collapse classification

`src/tools/MCPTool/classifyForCollapse.ts` 是手工 allowlist：

- 分 `SEARCH_TOOLS` 与 `READ_TOOLS`。
- 覆盖 Github、Linear、Datadog、Sentry、Notion、Gmail、Drive、Calendar、Atlassian、Asana、filesystem、DB、Grafana、PagerDuty、Supabase、Stripe、PubMed、BigQuery、Firecrawl、Exa、Perplexity、Tavily、Obsidian、Figma、Playwright、Puppeteer、MongoDB、Neo4j、Elasticsearch、Airtable、Todoist、AWS、Kubernetes 等大量工具名。
- `normalize()` 会把 camelCase / kebab-case 转为 snake_case。
- unknown tool 默认不标记 search/read，保守不 collapse。

Zyra 不必照搬这份具体 allowlist，但需要保留“未知 MCP tool 保守处理”的设计。否则 context collapse 可能错误压缩具有副作用或不可恢复输出的工具结果。

## L6.4：auth / OAuth / XAA / step-up

### OAuth provider

`src/services/mcp/auth.ts` 是认证复杂度最高的文件之一。核心行为：

- `normalizeOAuthErrorBody()` 将 2xx OAuth error body 归一化，并把 Slack 类 `invalid_refresh_token` / `expired` / `token_expired` 映射到 `invalid_grant`。
- `fetchAuthServerMetadata()` 支持配置 metadata URL，也支持 RFC 9728 -> RFC 8414 discovery，并保留路径感知 fallback。
- `getServerKey()` 使用 serverName + hash(type/url/headers)，避免 credential 在不同配置间串用。
- revoke 使用 RFC7009，先尝试 refresh token，再按 `client_secret_basic` / `post` / bearer fallback，最后清本地 token。
- step-up scope/resource metadata 会被缓存，reauth 时可以保留。
- OAuth callback server 支持 manual callback URL、state validation、browser open optional、5 分钟 timeout。
- refresh 有 lockfile，避免多进程同时 refresh。
- `invalid_grant` 会先检查其它进程是否已经刷新成功，再决定是否清 token。
- transient refresh failure 会 retry。
- client secret 可从 env/TTY 读取并单独保存/清除。

迁移裁决：Zyra 如果短期不迁 OAuth 全量实现，至少要把 token custody、server key、needs-auth、reauth、step-up 和 refresh failure 作为状态机字段建出来，而不是把认证失败当普通连接失败。

### XAA

`src/services/mcp/xaa.ts`：

- PRM -> AS metadata -> RFC8693 id_token to ID-JAG -> RFC7523 JWT bearer -> access_token。
- 对 resource 和 issuer URL 做 normalization，防 mix-up。
- token endpoints 必须是 https。
- error 会 redacts token-bearing fields。
- token exchange 4xx 清 IdP id_token，5xx 保留。

`src/services/mcp/xaaIdpLogin.ts`：

- `CLAUDE_CODE_ENABLE_XAA` gate。
- settings 中保存 `xaaIdp`。
- id_token 根据 normalized issuer 缓存在 keychain，提前 60 秒视为过期。
- IdP client secret 单独存储。
- OIDC discovery 会 append `.well-known` 并保留 issuer path。
- callback server state validation。
- `acquireIdpIdToken()` 返回 cached id_token 或走 auth_code + PKCE。

`mcp xaa` command 提供 setup/login/show/clear。迁移到 Zyra 时，XAA 可以先作为 deferred/unsupported，但文档和 schema 必须明确它不是普通 OAuth token。

## L6.5：elicitation、channel notification、permission relay

### elicitation

`src/services/mcp/elicitationHandler.ts`：

- 注册 MCP `Elicit` request handler。
- 先走 hooks。
- 若 hook 未处理，则把 elicitation 入队到 AppState，并提供 respond callback。
- URL elicitation 使用 waiting state。
- completion notification 会标记 queued event completed。
- result hooks 可以 modify/block，notification hooks 会触发。

`callMCPToolWithUrlElicitationRetry()`：

- 处理 `UrlElicitationRequired`（`-32042`）。
- 先跑 hooks，再进入 UI/SDK queue。
- result hooks 可修改或阻断。
- 最多 retry 3 次。

Zyra 迁移必须把 elicitation 建成真实 pending request queue。它不是普通 exception retry。

### channel notification

`channelAllowlist.ts`：

- 使用 GrowthBook ledger。
- runtime gate `tengu_harbor`。
- marketplace/plugin approved server 可以通过。

`gateChannelServer()` 顺序：

1. server capability。
2. runtime gate。
3. OAuth auth。
4. team/enterprise policy。
5. session `--channels`。
6. marketplace verification。
7. allowlist / dev bypass。

`channelNotification.ts`：

- `wrapChannelMessage()` XML-escape meta keys/values。
- meta key 有安全 regex。
- allowed server 的 channel notification 会入队成 meta prompt。

`channelPermissions.ts`：

- permission relay 有单独 GrowthBook gate。
- server 必须声明 `claude/channel` 和 `claude/channel/permission`。
- structured event 解析 request IDs，不靠正则。
- `shortRequestId` 把 toolUseID hash 成 5 个字母，带 blocklist。
- preview truncation 200 chars。

迁移裁决：channel 可以晚于基础 MCP 落地，但 gating order 与 structured permission relay 值得保留。Zyra 后续多 worker/远程 worker 的 permission relay 可借鉴这套“capability + policy + session opt-in + allowlist”的层级。

## L6.6：instructions delta 与 prompt cache

`src/constants/prompts.ts` 有旧路径：

- `getMcpInstructionsSection(mcpClients)` 调 `getMcpInstructions(mcpClients)`。
- 只包含 `connected` 且有 `instructions` 的 server。
- 输出 `# MCP Server Instructions`，按 server name 分块。

但 dynamic prompt section 里有 gate：

- 若 `isMcpInstructionsDeltaEnabled()` 为 true，则 system prompt 不再每轮重算 MCP instructions。
- 说明注释明确：late MCP connect 时，用 persisted `mcp_instructions_delta` attachments 避免 bust prompt cache。

`src/utils/attachments.ts`：

- attachment union 包含 `type: 'mcp_instructions_delta'`。
- `getAttachments()` 会调用 `getMcpInstructionsDeltaAttachment(...)`。
- `getMcpInstructionsDeltaAttachment()`：
  - gate 关闭直接返回空。
  - 在 ToolSearch 可用、模型支持 tool reference、ToolSearch 工具可用时，合成 Chrome ToolSearch client-side instructions。
  - 调 `getMcpInstructionsDelta(mcpClients, messages, clientSide)`。
  - 有 delta 才返回 attachment。

`src/utils/mcpInstructionsDelta.ts` 已读结论：

- 根据历史 messages 中的 attachment 计算 server instructions added/removed delta。
- 包含真实 server instructions 与 client-side instructions。
- 用于 persisted delta attachments，而不是每轮重新拼 system prompt。

对 Zyra 的关键意义：MCP instructions 是会话状态的一部分，需要进入 compact restore。否则 compact 后模型会忘记 late-connected server 的使用说明，或者每轮都重算导致缓存失效。

## L6.7：CLI/control surface

`src/commands/mcp/addCommand.ts`：

- 支持 stdio/sse/http。
- 支持 local/user/project scope。
- 支持 env/header。
- 支持 OAuth client-id/client-secret/callback-port。
- 支持 XAA 预校验。
- URL-looking stdio command 会 warning。
- client secrets 分离保存，不直接塞进普通 config。

`src/commands/mcp/mcp.tsx`：

- `/mcp` JSX UI。
- 支持 no-redirect、reconnect、enable/disable。
- 常规路径进入 `MCPSettings`。
- 某些 build/gate 下可 redirect 到 PluginSettings。

`src/commands/mcp/xaaIdpCommand.ts`：

- `setup` / `login` / `show` / `clear`。
- issuer 必须 https 或 loopback http。
- callback port 必须正数。
- secret 写 keychain。
- login 可用 injected id-token 测试或浏览器 auth。

Zyra M2 控制台不应把 MCP 命令做成静态表单。它需要调用同一套 `McpConfigStore`、`McpConnectionRuntime` 和 `McpAuthRuntime`。

## L6.8：AppState bridge

`src/services/mcp/useManageMCPConnections.ts` 是 React/AppState 层 glue，但里面包含真实运行责任：

- 批量更新 `AppState.mcp` clients/tools/commands/resources。
- disabled/failed 会清理对应 artifacts。
- connected 后注册 real elicitation handler。
- remote onclose 自动 reconnect。
- stdio/sdk 不自动 reconnect。
- channel notification handlers 在 gate 通过时注册。
- channel permission notification 会 resolve pending callbacks。
- `list_changed` 刷新 tools/prompts/resources。
- `resources_changed` 刷新 MCP skills 并清 skill search cache。
- session/reload 时 initialize pending servers。
- cleanup stale plugin/config-changed clients。
- phase 1 快速加载 Claude Code configs。
- phase 2 异步加载 claude.ai configs 并 dedupe。
- 暴露 `reconnectMcpServer` 和 `toggleMcpServer`。

Zyra 若只在后端做 MCP runtime，需要把这些 AppState mutation 映射成 event log / API state update。否则 UI 可以看到按钮，但 query loop 的 tool pool 不会变化。

## Source graph

```text
settings / .mcp.json / managed-mcp / plugins / claude.ai
  -> config.ts
     -> normalize + env/header expansion
     -> enterprise policy + project approval + dedupe
     -> MCPServerConfig[]
  -> useManageMCPConnections()
     -> getClaudeCodeMcpConfigs()
     -> getAllMcpConfigs()
     -> connectToServer()
        -> transport factory
           -> stdio | http | sse | ws | sdk | claudeai-proxy | in-process
        -> ClaudeAuthProvider / XAA / needs-auth cache
        -> SDK Client initialize(capabilities: roots, elicitation)
        -> connection state
     -> fetch tools/resources/prompts/skills
        -> MCP tools -> Tool contract
        -> MCP resources -> List/Read MCP resource tools
        -> MCP prompts -> Command
        -> MCP skills -> Skill registry
     -> AppState.mcp update
        -> query() tool pool refresh
        -> command registry refresh
        -> UI connection status
        -> elicitation queue
        -> channel notification / permission relay

query()
  -> active tools include MCP Tool projections
  -> runToolUse()
     -> tool checkPermissions passthrough
     -> general permission runtime
     -> callMCPToolWithUrlElicitationRetry()
        -> callMCPTool()
           -> client.callTool()
           -> progress notifications
           -> session expiry retry
           -> OAuth/needs-auth errors
        -> processMCPResult()
           -> content blocks
           -> structured content
           -> binary/resource persistence
           -> token truncation
  -> tool_result / artifact / event output

system prompt / attachments
  -> if delta disabled: getMcpInstructionsSection()
  -> if delta enabled: getMcpInstructionsDeltaAttachment()
     -> persisted mcp_instructions_delta attachment
     -> compact restore must preserve effective server instructions
```

## Zyra 迁移裁决

### 应优先内化

- config merge/policy/dedupe/project approval：这是运行边界，不是 SDK 功能。
- MCP connection state machine：connected/failed/needs-auth/pending/disabled 需要进入 Zyra session/runtime store。
- tool/resource/prompt projection：MCP 能力必须进入 Zyra `ToolRegistryRuntime` 与 `ControlCommand`。
- auth state abstraction：OAuth/XAA 可分期实现，但 token custody、needs-auth、step-up、reauth 不能丢。
- output budget/persistence：MCP result 应进入 Zyra artifact/result storage，不应只截成字符串。
- elicitation queue：必须进入 API/UI approval queue。
- instructions delta：需要进入 session message/attachment 与 compact restore。

### 可延期或裁剪

- claude.ai hosted connectors：可先作为 adapter/source reference，Zyra 未必需要完全复刻。
- channel notifications：可在多 worker/control UI 需要时再内化，但 gating model 值得保留。
- VS Code SDK MCP：如果 Zyra 暂无 VS Code extension，可 deferred。
- 官方 MCP registry：可作为非核心 discovery/verification；不能阻塞基础 MCP。
- XAA 全流程：可先以 schema/unsupported state 表达，后续再实现。

### 不应照搬

- 上游 React `MCPSettings` UI 组件不应直接进入 M1 后端内化；M2 控制台再按 Zyra API 重建。
- `classifyForCollapse.ts` 的长 allowlist 不应被当成“能力实现行数”。可保留为数据/规则源，但有效内化是 collapse 使用它产生真实行为。
- `McpAuthTool` 的文案和 UI 状态不能替代后端 needs-auth runtime。

## 对 M1 切片的影响

建议后续将 MCP 分成至少三个可执行切片，而不是一次性吞完整 auth + UI：

1. `McpConfigStore + connection state`
   - 目标：配置 merge、scope、policy、project approval、dedupe、enabled/disabled、connection state event。
   - 行为测试：不同 scope 冲突、project pending、enterprise exclusive、policy deny、plugin stale cleanup。
2. `McpToolProjection + resources/prompts`
   - 目标：server tool/resource/prompt 进入 Zyra ToolRegistry/ControlCommand，并能被 query loop 触发。
   - 行为测试：MCP tool call 进入 permission -> result artifact；resource list/read 可用；prompt 转 command。
3. `McpAuth + elicitation + instructions delta`
   - 目标：needs-auth tool、OAuth/XAA state placeholder、elicitation queue、server instructions delta 与 compact restore。
   - 行为测试：needs-auth server 不反复连接；elicitation pending 可 resolve/block；late connect instructions compact 后仍保留。

## 本批自检

- 配置链路：已覆盖 `types/config/envExpansion/normalization/utils/headersHelper/officialRegistry/project approval dialog`。
- 连接链路：已覆盖 `client/useManageMCPConnections/MCPConnectionManager/transports/claudeai/vscodeSdk`。
- auth 链路：已覆盖 `auth/oauthPort/xaa/xaaIdpLogin`。
- tool/resource/prompt 链路：已覆盖 MCP generic tool、resource tools、auth pseudo tool、collapse classifier、output budget/storage。
- instructions 链路：已覆盖 system prompt 旧路径与 `mcp_instructions_delta` attachment 新路径。
- UI/command 链路：已覆盖 `/mcp add`、`/mcp`、`mcp xaa`，但未深读 M2 级 `components/mcp/**` 具体面板。该缺口不阻断本批后端 source graph；后续 M2 UI batch 再读。
- 未把任何 root source 目录当成 Zyra 内化完成证据；本批仅产生分析文档。
