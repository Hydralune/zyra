# Batch04: MCP Runtime / Workspace Gateway / Builtin Tool Security

日期：2026-07-07

目标：把 AgentScope 的 MCP runtime、workspace gateway、Docker/E2B sandbox backend 和 Bash security parser 读成可迁移的链路级 source graph，判断哪些机制适合进入 Zyra 的 `workspace/sandbox gateway`、`MCP runtime`、`ToolPermissionRuntime` 和 coding builtin tools。

## 1. 已读范围

MCP：

- `src/agentscope/mcp/__init__.py`
- `src/agentscope/mcp/_config.py`
- `src/agentscope/mcp/_mcp_client.py`
- `src/agentscope/tool/_adapters.py` 中 `MCPTool`

Workspace / gateway：

- `src/agentscope/workspace/_base.py`
- `src/agentscope/workspace/_local_workspace.py`
- `src/agentscope/workspace/_sandboxed_base.py`
- `src/agentscope/workspace/_gateway_client.py`
- `src/agentscope/workspace/_gateway_shim.py`
- `src/agentscope/workspace/_mcp_gateway/__main__.py`
- `src/agentscope/workspace/_mcp_gateway/_mcp_gateway_app.py`
- `src/agentscope/workspace/_utils.py`
- `src/agentscope/workspace/_offload_protocol.py`

Backends / provisioning：

- `src/agentscope/tool/_builtin/_backend.py`
- `src/agentscope/workspace/_docker/_docker_backend.py`
- `src/agentscope/workspace/_docker/_docker_workspace.py`
- `src/agentscope/workspace/_docker/_make_dockerfile.py`
- `src/agentscope/workspace/_docker/Dockerfile*.template`
- `src/agentscope/workspace/_e2b/_e2b_backend.py`
- `src/agentscope/workspace/_e2b/_e2b_workspace.py`
- `src/agentscope/workspace/_e2b/_bootstrap.py`
- `src/agentscope/app/workspace_manager/*.py`

Builtin Bash security：

- `src/agentscope/tool/_builtin/_bash_parser.py`
- `src/agentscope/tool/_builtin/_bash.py`
- `src/agentscope/tool/_constants.py`
- `src/agentscope/tool/_base.py` 中 path policy helpers
- `src/agentscope/permission/_context.py`
- `src/agentscope/permission/_engine.py`

Tests：

- `tests/mcp_sse_client_test.py`
- `tests/mcp_streamable_http_client_test.py`
- `tests/workspace_local_test.py`
- `tests/workspace_docker_test.py`
- `tests/workspace_e2b_test.py`
- `tests/backend_docker_test.py`
- `tests/backend_e2b_test.py`
- `tests/builtin_bash_test.py`
- `tests/permission_bash_parser_test.py`
- `tests/permission_engine_test.py`
- `tests/permission_mode_test.py`

## 2. Batch04 主链

```text
Agent / ChatService
  -> workspace_manager.get_workspace(workspace_id)
  -> WorkspaceBase.list_tools()
       -> Bash/Edit/Glob/Grep/Read/Write bound to BackendBase
  -> WorkspaceBase.list_mcps()
       LocalWorkspace:
         -> MCPClient(stateful or stateless)
         -> MCPTool(session or client_gen)
       Docker/E2B workspace:
         -> SandboxedWorkspaceBase
         -> in-sandbox FastAPI gateway
         -> GatewayClient(exec_shell shim transport)
         -> GatewayMCPClient
         -> GatewayMCPTool
  -> Toolkit.call_tool
  -> PermissionEngine.check_permission
  -> tool.check_permissions
       Bash:
         -> BashCommandParser(tree-sitter-bash)
         -> read-only / injection / dangerous command / sed / path checks
```

这条链路的核心设计是：Agent 不直接关心本地、Docker、E2B 的环境差异；差异被压到 `BackendBase` 的三种原语和 `WorkspaceBase` 的 lifecycle 中。MCP 也分两种：本地 workspace 直接持有 MCP session；沙箱 workspace 把 MCP session 移入沙箱内部 gateway，主进程通过 `backend.exec_shell` 调一个 shim 访问沙箱 loopback。

## 3. MCP Runtime

来源：

- `src/agentscope/mcp/_config.py`
- `src/agentscope/mcp/_mcp_client.py`
- `src/agentscope/tool/_adapters.py`

### 3.1 Config model

`StdioMCPConfig`：

- `type="stdio_mcp"`
- `command`
- `args`
- `env`
- `cwd`
- `encoding_error_handler`

`HttpMCPConfig`：

- `type="http_mcp"`
- `url`
- `headers`
- `timeout`

`MCPClient` 使用 pydantic discriminator 解析二者。

### 3.2 MCPClient state model

字段：

- `name`
- `is_stateful`
- `mcp_config`
- `enable_tools`
- `disable_tools`
- `execution_timeout`

私有状态：

- `_client`
- `_session`
- `_stack`
- `_is_connected`
- `_cached_tools`

关键约束：

- `name` 必须匹配 `[a-zA-Z0-9_-]+`，因为 tool name 会组装成 `mcp__{name}__{tool}`。
- STDIO MCP 必须 `is_stateful=True`。
- `enable_tools` 和 `disable_tools` 都必须是字符串列表，且不能重叠。
- STDIO client context manager 在构造后预创建；HTTP client context manager 延迟创建，因为 SSE / streamable HTTP context manager 是 one-shot。

### 3.3 Stateful / stateless MCP

Stateful：

```text
MCPClient.connect()
  -> create client context
  -> ClientSession(read_stream, write_stream)
  -> session.initialize()
  -> _is_connected = True

list_raw_tools()
  -> validate connected session
  -> session.list_tools()
  -> cache full tools
  -> apply enable/disable filter

get_tool(name)
  -> lookup from unfiltered cache
  -> MCPTool(session=_session)
```

Stateless HTTP：

```text
list_raw_tools()
  -> async with _get_client_gen()
  -> transient ClientSession
  -> session.initialize()
  -> session.list_tools()

get_tool(name)
  -> MCPTool(client_gen=_get_client_gen)

MCPTool.call()
  -> each call opens transient ClientSession
  -> session.call_tool(...)
```

这意味着 stateless MCP 不持有长期 session，但每次列工具或调用工具会重新握手。Zyra 如果采用，需要把连接开销和 server rate limit 放进 runtime budget。

### 3.4 MCPTool wrapper

`MCPTool` 做了几件重要事情：

- visible tool name：`mcp__{mcp_name}__{sanitized_tool}`。
- 原始 server-side tool name 保存在 `_tool.name`，调用时仍使用原始名。
- `input_schema` 保留完整 MCP `inputSchema`，包括 `$defs/anyOf/oneOf`，避免嵌套 schema 丢失。
- `annotations.readOnlyHint` 被映射到 `is_read_only`。
- permission：
  - read-only MCP tool -> `ALLOW`
  - 非 read-only MCP tool -> `ASK`
- content conversion：
  - `TextContent` -> `TextBlock`
  - image/audio base64 -> `DataBlock(Base64Source)`
  - embedded text resource -> `TextBlock(model_dump_json)`
  - resource URI -> `DataBlock(URLSource)`
  - `isError=True` -> `ToolResultState.ERROR`

迁移价值：

- MCPClient + MCPTool 是可直接参考的 Python MCP wrapper。
- `inputSchema` 保留完整结构和 `readOnlyHint` permission 映射值得迁入 Zyra。
- `enable_tools/disable_tools` 是 MCP server 裁剪工具面的简单机制，适合接入 Zyra capability routing。

迁移风险：

- stateful session lifecycle 必须被 Zyra session/runtime state 管控，不能只由 Python object 私有状态持有。
- MCP tool 调用结果应写 Zyra event log/artifact，而不是只回 `ToolChunk`。
- stateless HTTP 每次打开 session，长程任务里需要 budget/connection pooling 决策。

## 4. WorkspaceBase 与本地 Workspace

来源：

- `src/agentscope/workspace/_base.py`
- `src/agentscope/workspace/_local_workspace.py`

### 4.1 Workspace layout

```text
{workdir}/
  .mcp          # MCP registrations
  data/         # offloaded multimodal payloads
  skills/       # skill directories
  sessions/     # session context and tool-result files
```

`WorkspaceBase` 的职责：

- `list_tools()`：返回 `Bash/Edit/Glob/Grep/Read/Write`，全部绑定当前 backend。
- `list_mcps/add_mcp/remove_mcp()`：抽象 MCP 管理。
- `_save_mcp_file()` / `_restore_or_seed_mcps()`：`.mcp` 持久化。
- `offload_context()`：把 message context append 到 `sessions/<session_id>/context.jsonl`。
- `offload_tool_result()`：把 tool result 写到 `sessions/<session_id>/tool_result-<id>.txt`。
- `_offload_data_block()`：base64 data 按 sha256 写到 `data/`，再改成 `file://` URL。
- `list_skills/add_skill/remove_skill()`：管理 `skills/SKILL.md`。

### 4.2 Offload model

Context offload：

```text
offload_context(session_id, msgs)
  -> deep copy msgs
  -> DataBlock(Base64Source) -> data/<sha256>.<ext>
  -> Msg JSONL append to sessions/<sid>/context.jsonl
```

Tool result offload：

```text
offload_tool_result(session_id, ToolResultBlock)
  -> sessions/<sid>/tool_result-<id>.txt
  -> TextBlock concatenated
  -> DataBlock -> <data url='...' name='...' media_type='...'/>
  -> filename conflict adds (1), (2), ...
```

这个机制适合 Zyra 的长上下文和大工具结果处理，但需要落到 Zyra artifact schema，而不是只生成 workspace file path。

### 4.3 Skill management

`WorkspaceBase.add_skill()` 对远端 backend 使用 tar + `python3 -c` 解包，内置 `_EXTRACT_TAR_SHIM` 检查每个 tar member 的 realpath，防止解包逃逸。

`LocalWorkspace` 做了更完整的 `.skills` index：

- 对 `SKILL.md` frontmatter 解析 `name/description`。
- 按 `SKILL.md` 内容 sha256 去重。
- agent-facing name 冲突用 `Name (1)` 解决。
- 目录名冲突用 `_1` 解决。
- 手工增删 `skills/` 时通过 mtime reconcile `.skills` index。

这部分和 Claude Code Markdown skills 的产品化方向一致，适合作为 Zyra skill runtime 的辅助来源。

### 4.4 Local MCP lifecycle

`LocalWorkspace.initialize()`：

```text
if .mcp exists:
  load entries -> MCPClient.model_validate()
else:
  _mcps = default_mcps
  save .mcp

for mcp in _mcps:
  if stateful and not connected:
    mcp.connect()
    on failure remove from list
```

`LocalWorkspace.close()` 只关闭已连接的 stateful MCP；stateless MCP 无需 close。

注意：当前 `LocalWorkspace.add_mcp()` 读到的实现没有明显 duplicate-name 检查，而 `WorkspaceBase.add_mcp()` docstring 和 `SandboxedWorkspaceBase.add_mcp()` 都要求 duplicate name 抛 `ValueError`。迁入 Zyra 时应修正为统一约束。

## 5. Sandboxed Workspace Gateway

来源：

- `src/agentscope/workspace/_sandboxed_base.py`
- `src/agentscope/workspace/_gateway_client.py`
- `src/agentscope/workspace/_gateway_shim.py`
- `src/agentscope/workspace/_mcp_gateway/_mcp_gateway_app.py`

### 5.1 设计目标

Docker/E2B workspace 不让主进程直接连接沙箱端口。gateway 运行在沙箱内部，监听 `127.0.0.1:<port>`。主进程每次请求 gateway 时，通过 workspace backend 在沙箱里执行一个 Python shim：

```text
Host process
  -> BackendBase.exec_shell(["python3", "-c", SHIM_SCRIPT, ...])
      runs inside Docker/E2B
        -> urllib.request to http://127.0.0.1:<gateway_port>
        -> stdout JSON envelope
  -> host parses envelope
```

价值：

- 不需要 Docker host port mapping。
- 不依赖 E2B HTTPS proxy。
- gateway 不暴露给 host network。
- 统一到 `exec_shell/read_file/write_file` backend contract。

### 5.2 SandboxedWorkspaceBase lifecycle

```text
initialize()
  -> _provision_backend()
  -> _restore_or_seed_mcps()
  -> _gateway_token = uuid
  -> pkill stale gateway process
  -> _write_gateway_config()
       { token, servers: [MCPClient.model_dump()] }
  -> _start_gateway_process()
       nohup <gateway_python> -u <gateway_script> --config ... --port ...
  -> GatewayClient(backend, port, token)
  -> _wait_for_gateway()
  -> GatewayClient.list_mcps()
  -> _gateway_clients[name] = GatewayMCPClient
  -> _save_mcp_file()
  -> _seed_skills()
  -> is_alive = True
```

`reset()` 不销毁沙箱，只：

- deregister gateway MCP clients。
- 清空 `_gateway_clients` 和 `_mcps`。
- 删除 `sessions/`、`data/`、`skills/`。
- 写空 `.mcp`，避免下一次 fallback 到 default MCPs。

### 5.3 Gateway server

FastAPI routes：

```text
GET    /health                     # no auth
GET    /mcps                       # bearer auth
POST   /mcps                       # add MCP
DELETE /mcps/{name}                # remove MCP
GET    /mcps/{name}/tools          # list upstream raw tools
POST   /mcps/{name}/tools/{tool}   # call upstream tool
```

Server state：

- `clients: dict[str, MCPClient]`
- `token`
- `lock`

`_build_client(spec)`：

```text
MCPClient.model_validate(spec)
if stateful:
  connect()
list_raw_tools()  # prime cache
```

Tool call：

```text
client.get_tool(tool)
await tool_obj(**arguments)
return {"chunk": chunk.model_dump(mode="json")}
```

Auth：

- `/health` 不要求 auth。
- token 非空时，其它 endpoint 要求 `Authorization: Bearer <token>`。
- token 每次 workspace initialize 重新生成，不持久化。

### 5.4 GatewayClient transport

`GatewayClient.exec_request()`：

```text
if body:
  write JSON to /tmp/<uuid>.json in sandbox

backend.exec_shell([
  "python3", "-c", SHIM_SCRIPT,
  method,
  "http://127.0.0.1:<port><path>",
  token,
  body_file,
  inline_limit,
  tmp_dir
])

parse stdout JSON:
  {status, body: base64}
  or {status, body_file}
  or {status: -1, error}

if body_file:
  read_file(body_file)
  delete_path(body_file)

cleanup request body_file
return (status, bytes)
```

`BODY_INLINE_LIMIT = 4 MiB`。超过后 response 写沙箱临时文件，再由 host 通过 backend `read_file()` 取回，避免多 MB payload 走 exec stdout。

### 5.5 GatewayMCPClient / GatewayMCPTool

`GatewayMCPClient` 继承 `MCPClient`，但覆盖协议逻辑：

- `model_post_init()` no-op，不构建本地 stdio/HTTP client。
- `attach(gateway, connected)` 注入 private gateway handle。
- `connect()` -> `POST /mcps`。
- `close()` -> `DELETE /mcps/{name}`。
- `list_raw_tools()` -> `GET /mcps/{name}/tools`。
- `get_tool(name)` -> wrap `GatewayMCPTool`。

`GatewayMCPTool`：

- field surface 模仿 `MCPTool`。
- `readOnlyHint` -> `is_read_only`。
- permission 与 `MCPTool` 一致：read-only allow，否则 ask。
- `__call__()` -> `POST /mcps/{mcp}/tools/{tool}`。
- 4xx/5xx 返回 `ToolChunk(state=ERROR)`，2xx 无 chunk 则抛 `RuntimeError`。

迁移注意：

- `MCPTool` 会 sanitize upstream tool name，`GatewayMCPTool` 当前直接使用 `mcp__{mcp_name}__{tool.name}`，没有同样的非法字符替换逻辑。若 upstream MCP tool name 包含 `.`, `:`, `/` 等，LLM provider tool name 可能不合法。Zyra 迁入时应统一 sanitize + server-side original name map。
- gateway 现有测试证据偏间接，没有看到单独覆盖 auth、large body spill、duplicate MCP、4xx/5xx、tool name sanitization 的行为测试。

## 6. Backend / Docker / E2B

来源：

- `src/agentscope/tool/_builtin/_backend.py`
- `src/agentscope/workspace/_docker/**`
- `src/agentscope/workspace/_e2b/**`

### 6.1 BackendBase

Backend 必须实现的原语只有三个：

```text
exec_shell(command: list[str], cwd=None, timeout=None) -> ExecResult
read_file(path: str) -> bytes
write_file(path: str, data: bytes) -> None
```

派生 helper：

- `getcwd()`
- `expanduser()`
- `file_exists()`
- `is_dir()`
- `list_dir(recursive=False)`
- `stat_mtime()`
- `delete_path()`
- pure path helpers：`join_path/dirname/basename/isabs/normpath/abspath`

设计要点：

- `exec_shell` 接收 argv list，不默认经 shell，调用方需要 shell 特性时显式 `["sh", "-c", "..."]`。
- 远端 backend 默认路径语义是 `posixpath`。
- `abspath(path, cwd=...)` 要求显式 cwd，避免远端路径不小心使用 host `os.getcwd()`。
- `list_dir()` 用 `find -print0` / `-printf "%f\0"`，能处理空格和换行文件名，但默认假设 GNU find。
- `LocalBackend` 覆盖文件和路径操作，用 host-native `os` 和 `asyncio.create_subprocess_exec`。

迁移价值：

- 这是 Zyra sandbox/workspace gateway 最值得吸收的接口之一。
- `argv-first exec` 应成为 Zyra 的工具执行默认形式。
- 派生 helper 中 POSIX/GNU 假设需要在 Zyra backend capabilities 中显式记录。

### 6.2 DockerBackend / DockerWorkspace

DockerBackend：

- `exec_shell()` 使用 `aiodocker` container exec API，直接传 `cmd=command`。
- timeout 通过 `asyncio.wait_for()`，超时返回 `ExecResult(exit_code=-1, stderr=b"timed out")`。
- `read_file()` 通过 `get_archive()` 取 tar 中第一个 file。
- `write_file()` 创建 parent dir，再用 `put_archive()` 写 tar。

DockerWorkspace：

- `workdir` 固定为 `/workspace`。
- `host_workdir` 非空时 bind mount 到 `/workspace`，workspace persistent。
- 无 host port mapping；gateway 只在 container 内部访问。
- `_provision_backend()` build/reuse image + create/start container。
- `_teardown_backend()` kill/delete container，Linux bind mount 下尝试 chown 回 host uid/gid。

Image build：

- Dockerfile + COPY payload hash -> `agentscope-workspace:<12hex>`。
- Released install：安装同版本 `agentscope==version`，`--no-deps`。
- Dev install：copy source tree 到 build context，`uv pip install --no-deps /tmp/agentscope_src`。
- Gateway venv 基础依赖：`mcp`, `uvicorn`, `fastapi`。
- 额外复制：
  - `_mcp_gateway_app.py`
  - `_glob_helper.py`
- Dockerfile 安装 `ripgrep`，用于 Grep builtin tool。

### 6.3 E2BBackend / E2BWorkspace

E2BBackend：

- `exec_shell()` 把 argv list 用 `shlex.quote` 重新组装成 command line，调用 E2B `commands.run()`。
- `CommandExitException` 被转换成普通 `ExecResult`。
- 其它 SDK error -> `ExecResult(exit_code=-1, stderr=...)`。
- `read_file()` / `write_file()` 使用 E2B `files` API。

E2BWorkspace：

- `workdir = /home/user/workspace`。
- sandbox filesystem 是 persistence layer。
- `close()` 是 `sandbox.pause()`，不是 kill。
- `_attach_or_create_sandbox()`：
  - 根据 metadata `agentscope.workspace.id` 查 paused/running sandbox。
  - 多个候选时取最新并 warn。
  - 不存在则 create。
  - 等 `is_running()` 变 true。
- bootstrap marker 是 gateway script 是否存在。
- bootstrap 安装：
  - `ripgrep`
  - Astral `uv`
  - gateway venv
  - gateway base requirements
  - `agentscope --no-deps`
  - `_glob_helper.py`
  - `_mcp_gateway_app.py`

### 6.4 Workspace managers

`WorkspaceManagerBase` 只暴露：

- `get_workspace(user_id, agent_id, session_id, workspace_id)`
- `create_workspace(user_id, agent_id, session_id)`
- `close(workspace_id)`
- `close_all()`

Local manager：

- cache key 是 `workspace_id`。
- local workdir 是 `<basedir>/<agent_id>`。
- cache miss 时从 deterministic workdir reconstruct。
- TTL eviction 是 lazy-on-get。

Docker manager：

- workdir 是 `<basedir>/<user_id>/<agent_id>`，避免不同用户 agent_id 冲突。
- cache key 是 `workspace_id`。
- TTL eviction 由 background sweeper。
- `close_all()` 并发关闭容器。

E2B manager：

- 无 host workdir。
- `workspace_id` 是 cache key，也写进 sandbox metadata。
- user/agent 只作为 dashboard metadata，不参与 cache key。
- TTL sweeper pause idle sandbox。

迁移判断：

- `WorkspaceManagerBase` 适合作为 Zyra workspace lifecycle service 的参考。
- Zyra 需要把 workspace_id/session_id/worker_id 和自己的 durable schema 绑定，不能只放在 Python manager cache。

## 7. Bash Security Parser / Permission

来源：

- `src/agentscope/tool/_builtin/_bash_parser.py`
- `src/agentscope/tool/_builtin/_bash.py`
- `src/agentscope/tool/_constants.py`
- `src/agentscope/tool/_base.py`
- `src/agentscope/permission/_engine.py`

### 7.1 Parser capabilities

`BashCommandParser` 用 `tree_sitter_bash`：

- `is_read_only_command(command)`
- `_is_single_command_read_only(cmd)`
- `extract_file_paths(command)`
- `extract_redirections(command)`
- `extract_command_prefixes(command, max_prefixes=5)`
- `split_compound_command(root, command)`
- `check_dangerous_command(command)`
- `check_sed_constraints(command, dangerous_files)`
- `check_injection_risk(command)`

Read-only 判定：

- command 包含 `>` 直接非 read-only。
- compound command 拆成子命令，必须所有子命令 read-only。
- `READ_ONLY_COMMANDS` 覆盖 `ls/cat/head/tail/file/stat/wc/grep/rg/find/tree/pwd`、只读 git、只读 docker、只读 gh、版本查询等。
- `SAFE_COMMANDS` 包含 `echo/cat/ls/pwd/cd/true/false/printf/grep/tee`。

Command prefix extraction：

- 支持 `&&`, `||`, `;`, `|`。
- 从每个 simple command 提取前两词，如 `git commit`、`npm run`。
- 跳过 safe env vars。
- safe command 不生成建议。
- 最多 5 条，去重。

危险检测：

- `DANGEROUS_COMMANDS`：`rm -rf`, `sudo rm`, `dd`, `mkfs`, `fdisk`, `format`, `chmod 777`, `chmod -R 777`, `chown -R`, `kill -9`, `> /dev/`。
- 单短词用 word boundary，避免 `git add` 误命中 `dd`。
- `DANGEROUS_NODE_TYPES`：command/process substitution、complex expansion、subshell、for/while/until/if/case/function/test command。

Sed constraints：

- 允许：
  - `sed -n 'Np'`
  - `sed -n 'N,Mp'`
  - `sed 's///'` 及常见 substitution flags
- 拒绝：
  - write `w/W`
  - execute `e/E`
  - braces
  - negation
  - comments
  - 不在 allowlist 的 expression
  - `-i` 修改 dangerous files

### 7.2 Bash.check_permissions order

```text
0. injection risk -> ASK bypass_immune
1. read-only command -> ALLOW
2. dangerous command pattern -> ASK bypass_immune
3. sed constraints -> ASK bypass_immune
4. dangerous file/dir path -> ASK bypass_immune
5. dangerous rm/rmdir target -> ASK bypass_immune
6. ACCEPT_EDITS filesystem command
     if target paths extracted and all inside working dirs:
       ALLOW
7. PASSTHROUGH to PermissionEngine rules
```

这说明 AgentScope 的 Bash 安全不是单纯 regex allow/deny，而是 tool 自身先做语义风险分类，再把剩余请求交给 permission engine 的 rule matching。

### 7.3 Rule matching / suggestions

`Bash.match_rule()`：

- `None` 匹配全部 Bash 调用。
- `prefix:*` 匹配 `prefix` 或以 `prefix ` 开头的命令。
- 未转义 `*` 转 regex `.*`。
- `\*` 表示字面星号，`\\` 表示字面反斜杠。
- 无 wildcard 时做 substring match。

`Bash.generate_suggestions()`：

```text
"git commit -m 'x'" -> PermissionRule("Bash", "git commit:*", ALLOW)
"npm install"       -> PermissionRule("Bash", "npm install:*", ALLOW)
```

### 7.4 Path policy caveat

`ToolBase._path_in_allowed_working_path()` 和 `_is_dangerous_path()` 当前使用 host `os.path` / `Path`：

- `_path_in_allowed_working_path()` 的默认 current dir 是 host `os.getcwd()`。
- `_is_dangerous_path()` 也按 host `os.path.abspath(os.path.expanduser(...))` 解析。

但 `Bash` 在 Docker/E2B workspace 中执行的是 sandbox path。`_check_dangerous_removal_path()` 已经改成 backend-aware，会用 `backend.expanduser()`、`backend.getcwd()`、`backend.abspath()`；普通 dangerous path 和 ACCEPT_EDITS working dir 检查还不是完全 backend-aware。

Zyra 迁入时应把 path policy 统一改成：

```text
Tool path policy
  -> backend path module / workspace root
  -> Zyra PermissionContext.allowed_workspace_roots
  -> no implicit host cwd
```

否则 sandbox 中的 `/workspace/foo`、`~/file`、bind mount path 与 host process cwd 的关系会不稳定。

## 8. 测试证据

MCP：

- `mcp_sse_client_test.py`
  - stateless SSE client：get tool、重复 call、Toolkit 注册、Toolkit(mcps=[client])。
  - stateful SSE client：connect/list/call/close。
  - `$defs` preservation：MCP inputSchema 的 `$defs` 不丢失，Toolkit schema stripping title 后仍可解析 ref。
- `mcp_streamable_http_client_test.py`
  - streamable HTTP stateless client。
  - embedded content conversion。

Workspace / backend：

- `workspace_local_test.py`
  - context/tool result offload。
  - DataBlock offload dedupe。
  - skill copy、dedupe、invalid skill、manual list。
  - Local workspace with Agent offload integration。
  - persisted `.mcp` invalid entry skip，connect failure removal。
- `workspace_docker_test.py`
  - Docker offload/skills/lifecycle。
  - list builtin tools。
  - bind-mounted workdir persistence。
  - reset clears sessions/data。
- `workspace_e2b_test.py`
  - skipped unless E2B_API_KEY。
  - real E2B sandbox initialize and seeded MCP list_raw_tools。
- `backend_docker_test.py` / `backend_e2b_test.py`
  - exec stdout/nonzero/cwd/timeout or quote preservation。
  - write/read roundtrip, binary preservation, parent dir creation。
  - derived file_exists/is_dir/list_dir/stat/delete.

Bash / permission：

- `permission_bash_parser_test.py`
  - command prefix extraction。
  - read-only command and compound read-only。
  - file path/redirection extraction。
  - sed allowlist/denylist。
  - wildcard/prefix/substring rule matching。
  - injection risk node detection。
  - dangerous command pattern detection。
- `builtin_bash_test.py`
  - subprocess cwd/timeout/output/error。
  - injection check before read-only。
  - match_rule / suggestion。
  - dangerous removal path: `/`, root children, `~`, wildcard, compound command。
- `permission_engine_test.py` / `permission_mode_test.py`
  - permission mode ordering and bypass-immune behavior。

测试缺口：

- 没有看到 `GatewayClient` / `_mcp_gateway_app` 独立单测。
- gateway auth、response spill file、transport failure、duplicate MCP、tool name sanitization、4xx/5xx ToolChunk error 需要 Zyra 迁入时补。
- Docker/E2B 测试部分依赖真实外部环境或可跳过，Zyra 应补 fake backend + integration backend 双层测试。

## 9. Zyra Source-to-Target 裁决

高价值，可迁入/改写：

- `MCPClient` + `MCPTool` 的 stateful/stateless 模型、tool filtering、schema preservation、readOnlyHint permission。
- `BackendBase` 三原语和 argv-first exec contract。
- `GatewayClient` 的 in-sandbox shim transport 设计。
- `SandboxedWorkspaceBase` 的 gateway lifecycle 模板。
- `BashCommandParser` 的 tree-sitter 分析、read-only 判定、危险命令、sed constraints、command prefix suggestions。
- `WorkspaceManagerBase` 的 workspace_id keyed cache / TTL lifecycle 模式。
- `WorkspaceBase.offload_context/offload_tool_result` 的大上下文/大结果落盘思路。

需要改造后迁入：

- `GatewayMCPTool` tool name sanitization 要与 `MCPTool` 统一。
- path policy 必须 backend-aware + Zyra-owned permission roots，不能隐式使用 host `os.getcwd()`。
- `.mcp` 和 `.skills` 不能作为 Zyra 事实源，只能是导入/兼容层；Zyra 需要自己的 schema/store/event log。
- Docker/E2B provisioning 不能直接成为 Zyra 完成形态；应抽出 gateway bootstrap、backend contract、workspace lifecycle，落到 Zyra `packages/**` 和 `apps/**`。
- gateway server 的 FastAPI routes 可参考，但 Zyra 需要记录 tool call、permission decision、MCP status 到 Zyra event/control plane。

低优先级或延后：

- E2B-specific SDK lifecycle 可以作为可选 backend，不应成为第一条必需主路径。
- AgentScope image build 的 dev/released install 分支对 Zyra 有启发，但不应原样迁入，Zyra 应建立自己的 runtime image/build artifact。

与其它来源分工：

- `claude-code-best` 更适合作为 coding agent query loop、permission runtime、commands/skills/subagent 的主要产品化来源。
- AgentScope Batch04 更适合作为 Python-native sandbox/backend/MCP/tool security 的实现来源。
- `agent-framework` 如果后续提供多 agent orchestration，可与 AgentScope workspace/MCP runtime 组合，但不替代此处的 backend/tool security。

## 10. Batch04 自检

验收问题对照：

- MCP stateful/stateless client 如何包装为 `MCPTool` 或 `GatewayMCPTool`？
  - 已读透：`MCPClient` stateful 持有 `ClientSession`，stateless 传 `client_gen`；sandbox 侧通过 `GatewayMCPClient` 和 `GatewayMCPTool` 代理 gateway。
- gateway process 的 auth、config、lifecycle、tool invocation 是否可迁入 Zyra sandbox gateway？
  - 已读透：token config、FastAPI routes、`SandboxedWorkspaceBase.initialize()`、shim transport、4MiB spill 机制都已记录。结论是设计适合迁，但测试和 Zyra event/control 接入必须补。
- Bash parser 的 command/read-only/dangerous path/injection 检测是否适合直接 port？
  - 适合部分直接 port：tree-sitter parser、sed constraints、dangerous command 和 rule suggestion 可迁。path policy 不能原样 port，需要 backend-aware 改造。
- Docker/E2B backend 的路径、进程、文件语义和 Zyra workspace/sandbox 是否匹配？
  - 语义基本匹配：三原语、argv-first exec、read/write file、workdir layout 都可参考。需要把 POSIX/GNU assumptions、E2B SDK lifecycle、Docker image build 和 Zyra own workspace schema 解耦。

批判性结论：

- Batch04 不是轻参考，而是一条可落地的 source-to-target 链：`BackendBase -> WorkspaceBase -> gateway -> MCPTool -> PermissionEngine/BashParser`。
- 最值得迁的是接口和控制流，不是目录结构。
- 迁入 Zyra 时必须先修两个防伪内化点：一是 path policy 的状态归属，二是 gateway/MCP 行为测试。否则容易变成“能调用 sidecar，但无法证明 Zyra 接管安全语义”的薄封装。
