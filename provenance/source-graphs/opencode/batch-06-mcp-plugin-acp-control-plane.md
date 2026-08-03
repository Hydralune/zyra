# Batch06：MCP、Plugin、ACP、Control Plane

日期：2026-07-08

本批次补读 opencode 的外部工具/插件/IDE 协议/工作区控制层，目标是判断它们对 Zyra MCPRuntime、PluginRuntime、IDE/control protocol、workspace runtime 的迁移价值。

## 1. 阅读范围

MCP：

- `packages/opencode/src/mcp/index.ts`
- `packages/opencode/src/mcp/auth.ts`
- `packages/opencode/src/mcp/catalog.ts`
- `packages/opencode/src/mcp/oauth-provider.ts`
- `packages/opencode/test/mcp/**`

Plugin：

- `packages/opencode/src/plugin/index.ts`
- `packages/opencode/src/plugin/loader.ts`
- `packages/opencode/src/plugin/shared.ts`
- `packages/opencode/src/plugin/install.ts`
- `packages/core/src/plugin/host.ts`
- `packages/core/src/plugin/internal.ts`
- plugin install/meta/provider/TUI tests

ACP：

- `packages/opencode/src/acp/service.ts`
- `packages/opencode/src/acp/event.ts`
- `packages/opencode/src/acp/permission.ts`
- `packages/opencode/src/acp/session.ts`
- ACP tests

Control plane：

- `packages/opencode/src/control-plane/workspace.ts`
- `packages/opencode/src/control-plane/types.ts`
- `packages/opencode/src/control-plane/workspace-adapter-runtime.ts`
- control-plane adapters/tests

## 2. MCP

MCP service 覆盖：

- local stdio server。
- remote StreamableHTTP/SSE server。
- OAuth pending transport/callback provider。
- dynamic registration、tokens、client info validation。
- roots。
- clients/defs/instructions。
- notifications。
- tool list changed -> `ToolsChanged`。
- prompts/resources/resourceTemplates/readResource/getPrompt。
- per-server/global timeout。
- structuredContent/schema fallback。
- finalizer close client and kill descendants。

测试覆盖：

- auth concurrent file update。
- lifecycle roots/cwd/cache/resources/prompts/templates。
- catalog structured content/schema。
- OAuth callback/browser。
- session recovery POST 404。

判断：

- MCP 是高价值来源，成熟度足以进入 Zyra M1 MCPRuntime 规划。
- 但当前 MCP 主要在 V1 product layer，尚未干净进入 V2 Core ToolRegistry。
- Zyra 应拆成 MCP connection/auth/token/resource/tool registry，而不是把 V1 service 当 sidecar 黑箱。

## 3. Plugin

V1 plugin layer：

- 内置 plugin：CodexAuth、Copilot、Gitlab、Poe、Cloudflare、Azure、DigitalOcean、Snowflake、Xai 等。
- 暴露 SDK/project/worktree/directory/experimental workspace/register/serverUrl/Bun `$`。
- 加载 config plugin origins，等待 dependencies，server plugin entrypoints。
- hooks：config/event、chat/message、chat params/headers、permission ask、tool execute before/after、system transform、compaction、tool definition 等。

Loader/install：

- spec/options/deprecated normalize。
- path/npm/exports `./server`/`./tui` 或 main/index resolve。
- compatibility via engines。
- install/entry/compat/load/missing stages。
- JSONC config patch，尽量保留 comments。

V2 `PluginHost`：

- plugin mutators：agent、AISDK sdk/language、catalog provider/model/default、command、integration、plugin add/remove、reference、skill。
- `PluginInternal` bootstraps config reference/agent/command/skill/provider、ProviderPlugins、external、variant、models-dev。

判断：

- 值得迁移的是 typed extension point、hook lifecycle、slot lifecycle、失败隔离。
- 动态 npm install/loading 不适合 Zyra 核心决策路径；可作为 marketplace/dev-time 能力，但不能作为第一阶段完成证明。

## 4. ACP

ACP service 将 SDK 映射为 Agent Client Protocol：

- `initialize` declares capabilities：loadSession、MCP http/sse、embeddedContext/image、session close/fork/list/resume、auth methods terminal-auth。
- new/load/resume/fork session snapshot directory，默认 model/variant/mode，register MCP，replay transcript。
- prompt content conversion、slash command、compact summarize、usage update、cancel mapping。
- event subscription 将 SDK global events 映射为 ACP content chunks/tool events，并做 per-session isolation。
- permission queue：requestPermission/reply once/always/reject，edit diff proposal/application。

判断：

- ACP 适合作为 Zyra IDE/control protocol adapter 参考。
- 它是 SDK adapter，不是核心 runtime；迁移时应接 Zyra session/event/permission API。

## 5. Workspace Control Plane

Control plane 负责：

- workspace schema/table/events。
- adapter interface：configure/create/list/remove/target。
- worktree adapter and plugin registration。
- remote `/global/event` SSE sync，history/replay/backoff。
- remote/local `runInWorkspace`。
- env 注入：`OPENCODE_AUTH_CONTENT`、`OPENCODE_WORKSPACE_ID`、`OPENCODE_EXPERIMENTAL_WORKSPACES`。
- sessionWarp：cancel/promote/replay/steal/copy diff。

判断：

- 对 Zyra workspace gateway、worktree isolation、远程 worker、会话迁移有参考价值。
- 它不替代 scheduler/fault recovery，但能补充 control plane 和 workspace adapter runtime。

## 6. Zyra 迁移裁决

推荐进入文档：

- M1 MCPRuntime：local/remote/OAuth/catalog/resource/tool conversion。
- M1/M2 PluginRuntime：typed extension point、hook lifecycle、TUI/Web slot lifecycle。
- M2/M3 control protocol：ACP session/event/permission adapter。
- M1 workspace runtime / M2 control plane：workspace adapter、remote event sync、session warp。

避免：

- 不要把 MCP V1 service 作为黑箱主路径。
- 不要把 dynamic npm plugin loading 计为核心能力。
- 不要把 ACP/control-plane 当作 agent loop 本身。

