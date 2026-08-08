from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath


class SourceDisposition(StrEnum):
    """How a reviewed upstream MCP mechanism participates in Zyra."""

    ACTIVE = "active"
    ADAPTER = "adapter"
    CONTRACT_ONLY = "contract-only"
    REFERENCE_ONLY = "reference-only"
    DEFERRED = "deferred"


class SourceAuthority(StrEnum):
    PRIMARY = "primary"
    SUPPLEMENTAL = "supplemental"


@dataclass(frozen=True, slots=True)
class McpSourceDecision:
    repository: str
    source_path: str
    disposition: SourceDisposition
    authority: SourceAuthority
    mechanisms: tuple[str, ...]
    target_paths: tuple[str, ...] = ()
    runtime_entry: str | None = None
    test_target: str | None = None
    main_path_evidence: str = ""
    limitations: tuple[str, ...] = ()
    replacement: str | None = None
    next_owner: str | None = None
    rationale: str = ""

    @property
    def key(self) -> str:
        return f"{self.repository}:{self.source_path}"

    @property
    def claims_runtime_ownership(self) -> bool:
        return self.disposition in {SourceDisposition.ACTIVE, SourceDisposition.ADAPTER}


@dataclass(frozen=True, slots=True)
class SourceAuditIssue:
    source_key: str
    code: str
    message: str
    blocking: bool = True


@dataclass(frozen=True, slots=True)
class SourceAuditReport:
    decisions: tuple[McpSourceDecision, ...]
    issues: tuple[SourceAuditIssue, ...]
    counts: Mapping[SourceDisposition, int] = field(default_factory=dict)

    @property
    def blocking_issues(self) -> tuple[SourceAuditIssue, ...]:
        return tuple(issue for issue in self.issues if issue.blocking)

    @property
    def complete(self) -> bool:
        return not self.blocking_issues

    def require_complete(self) -> "SourceAuditReport":
        if self.blocking_issues:
            detail = "; ".join(
                f"{issue.source_key} [{issue.code}]: {issue.message}"
                for issue in self.blocking_issues
            )
            raise SourceAuditError(detail)
        return self


class SourceAuditError(RuntimeError):
    pass


_MCP_ROOT = "packages/integrations/zyra_integrations/mcp"
_TS_ROOT = "packages/integrations/claude-mcp/src"
_TS_PACKAGE = "@zyra/claude-mcp"

# ``1b1ffeb`` productized the MCP client runtime into TypeScript and deleted the
# Python modules that used to own these mechanisms.  A target must name the file
# that owns the mechanism today; the Python package keeps durable state, result
# budgets, events and credential custody, so those targets stay Python.
_CONFIG = f"{_TS_ROOT}/config/config-store.ts"
_CONNECTION = f"{_TS_ROOT}/connection/connection-runtime.ts"
_TRANSPORT_CONTRACTS = f"{_TS_ROOT}/connection/contracts.ts"
_TRANSPORT_STDIO = f"{_TS_ROOT}/connection/stdio-transport.ts"
_TRANSPORT_HTTP = f"{_TS_ROOT}/connection/http-transport.ts"
_TRANSPORT_SSE = f"{_TS_ROOT}/connection/sse-parser.ts"
_PROTOCOL = f"{_TS_ROOT}/core/protocol.ts"
_AUTH = f"{_TS_ROOT}/auth/oauth-runtime.ts"
_CAPABILITIES = f"{_TS_ROOT}/catalog/capability-catalog.ts"
_PROJECTION = f"{_TS_ROOT}/projection/tool-projection.ts"
_ELICITATION = f"{_TS_ROOT}/runtime/elicitation-runtime.ts"
_SAMPLING = f"{_TS_ROOT}/runtime/sampling-runtime.ts"
_INSTRUCTIONS = f"{_TS_ROOT}/projection/instruction-runtime.ts"
_TASKS = f"{_TS_ROOT}/runtime/task-runtime.ts"
_CREDENTIALS = f"{_MCP_ROOT}/credentials.py"
_OUTPUT = f"{_MCP_ROOT}/output.py"
_EVENTS = f"{_MCP_ROOT}/events.py"
_STORE = f"{_MCP_ROOT}/store.py"
_MODELS = f"{_MCP_ROOT}/models.py"

_CONFIG_ENTRY = f"{_TS_PACKAGE}/config/config-store#McpConfigStore"
_CONNECTION_ENTRY = f"{_TS_PACKAGE}/connection/connection-runtime#McpConnectionRuntime"
_AUTH_ENTRY = f"{_TS_PACKAGE}/auth/oauth-runtime#McpOAuthRuntime"
_CAPABILITY_ENTRY = f"{_TS_PACKAGE}/catalog/capability-catalog#McpCapabilityCatalog"
_PROJECTION_ENTRY = f"{_TS_PACKAGE}/projection/tool-projection#McpToolProjection"
_OUTPUT_ENTRY = "zyra_integrations.mcp.output.McpOutputBudgetRuntime"
_ELICITATION_ENTRY = f"{_TS_PACKAGE}/runtime/elicitation-runtime#McpElicitationRuntime"
_SAMPLING_ENTRY = f"{_TS_PACKAGE}/runtime/sampling-runtime#McpSamplingRuntime"
_INSTRUCTIONS_ENTRY = f"{_TS_PACKAGE}/projection/instruction-runtime#McpInstructionRuntime"
_TASK_ENTRY = f"{_TS_PACKAGE}/runtime/task-runtime#McpTaskRuntime"

_CLAUDE_MCP_TEST = "packages/integrations/claude-mcp/test"
_CLAUDE_RUNTIME_TEST = "packages/runtime/claude-runtime/test"
# The behavior suite is organized by custody boundary rather than by module, so
# several roles legitimately resolve to the same file.  Each name still records
# which behavior the row is claiming.
_CONFIG_TEST = f"{_CLAUDE_MCP_TEST}/e02/mcp-custody.behavior.test.ts"
_TRANSPORT_TEST = f"{_CLAUDE_MCP_TEST}/e02/mcp-live-transport.behavior.test.ts"
_AUTH_TEST = f"{_CLAUDE_MCP_TEST}/e02/mcp-custody.behavior.test.ts"
_CAPABILITY_TEST = f"{_CLAUDE_MCP_TEST}/e02/mcp-catalog-matrix.test.ts"
_WORKER_TEST = f"{_CLAUDE_RUNTIME_TEST}/e04/permission-mcp-source-recovery.behavior.test.ts"
_API_TEST = f"{_CLAUDE_MCP_TEST}/e02/mcp-custody.behavior.test.ts"
_ALLOWED_TEST_TARGETS = frozenset(
    {_CONFIG_TEST, _TRANSPORT_TEST, _AUTH_TEST, _CAPABILITY_TEST, _WORKER_TEST, _API_TEST}
)


def _decision(
    repository: str,
    source_path: str,
    disposition: SourceDisposition,
    mechanisms: tuple[str, ...],
    *,
    targets: tuple[str, ...],
    entry: str | None = None,
    test: str | None = None,
    evidence: str = "",
    authority: SourceAuthority = SourceAuthority.SUPPLEMENTAL,
    limitations: tuple[str, ...] = (),
    replacement: str | None = None,
    next_owner: str | None = None,
    rationale: str = "",
) -> McpSourceDecision:
    return McpSourceDecision(
        repository=repository,
        source_path=source_path,
        disposition=disposition,
        authority=authority,
        mechanisms=mechanisms,
        target_paths=targets,
        runtime_entry=entry,
        test_target=test,
        main_path_evidence=evidence,
        limitations=limitations,
        replacement=replacement,
        next_owner=next_owner,
        rationale=rationale,
    )


def _claude(
    path: str,
    disposition: SourceDisposition,
    mechanisms: tuple[str, ...],
    *,
    targets: tuple[str, ...],
    entry: str | None = None,
    test: str | None = None,
    evidence: str = "",
    limitations: tuple[str, ...] = (),
    replacement: str | None = None,
    next_owner: str | None = None,
    rationale: str = "",
) -> McpSourceDecision:
    return _decision(
        "claude-code-best",
        path,
        disposition,
        mechanisms,
        targets=targets,
        entry=entry,
        test=test,
        evidence=evidence,
        authority=SourceAuthority.PRIMARY,
        limitations=limitations,
        replacement=replacement,
        next_owner=next_owner,
        rationale=rationale,
    )


_CONFIG_CHAIN = "McpConfigStore -> McpConnectionRuntime -> session mcp_runtime state and MCP diagnostics API"
_CONNECTION_CHAIN = "McpConnectionRuntime -> transport JSON-RPC initialize -> capability snapshot replacement"
_TOOL_CHAIN = "McpToolProjection -> ToolRegistryRuntime -> ToolExecutionRuntime -> ToolPermissionRuntime"
_CONTROL_CHAIN = "MCP API/control request -> durable MCP state/event -> exact resolve or compact restore"


MCP_SOURCE_DECISIONS: tuple[McpSourceDecision, ...] = (
    # Claude is the primary product behavior source.  Prompt copy and React/Ink
    # views are deliberately not treated as runtime code.
    _claude("src/services/mcp/types.ts", SourceDisposition.CONTRACT_ONLY,
            ("server/config/status vocabulary", "tool/resource/prompt contracts"),
            targets=(_MODELS,), replacement="Zyra immutable MCP models with revision and provenance"),
    _claude("src/services/mcp/config.ts", SourceDisposition.ACTIVE,
            ("source precedence", "project approval", "dedupe and disabled policy"),
            targets=(_CONFIG, _STORE), entry=_CONFIG_ENTRY, test=_CONFIG_TEST, evidence=_CONFIG_CHAIN),
    _claude("src/services/mcp/envExpansion.ts", SourceDisposition.ACTIVE,
            ("bounded environment expansion", "missing variable diagnostics"),
            targets=(_CONFIG,), entry=_CONFIG_ENTRY, test=_CONFIG_TEST, evidence=_CONFIG_CHAIN,
            limitations=("expanded secret values never enter durable public state",)),
    _claude("src/services/mcp/normalization.ts", SourceDisposition.ACTIVE,
            ("server identity normalization", "transport normalization"),
            targets=(_CONFIG, _MODELS), entry=_CONFIG_ENTRY, test=_CONFIG_TEST, evidence=_CONFIG_CHAIN),
    _claude("src/services/mcp/utils.ts", SourceDisposition.ACTIVE,
            ("configuration equality", "server signature and URL policy"),
            targets=(_CONFIG,), entry=_CONFIG_ENTRY, test=_CONFIG_TEST, evidence=_CONFIG_CHAIN),
    _claude("src/services/mcp/mcpStringUtils.ts", SourceDisposition.CONTRACT_ONLY,
            ("stable display naming", "bounded diagnostic strings"),
            targets=(_MODELS, _EVENTS), replacement="Zyra canonical identifiers and redacted event strings"),
    _claude("src/services/mcp/headersHelper.ts", SourceDisposition.ACTIVE,
            ("header template materialization", "sensitive header separation"),
            targets=(_CONFIG, _CREDENTIALS), entry=_CONFIG_ENTRY, test=_CONFIG_TEST, evidence=_CONFIG_CHAIN,
            limitations=("authorization and cookie material is credential-vault owned",)),
    _claude("src/services/mcp/officialRegistry.ts", SourceDisposition.REFERENCE_ONLY,
            ("registry metadata shape", "known server presentation"),
            targets=(_CONFIG,), replacement="explicit Zyra config sources; no network registry is a trust root",
            rationale="A changing external registry cannot own configuration precedence or approval."),
    _claude("src/components/MCPServerApprovalDialog.tsx", SourceDisposition.DEFERRED,
            ("project-server approval presentation",), targets=(_CONFIG,),
            replacement="backend approval state and API are owned by this slice", next_owner="M2-04A",
            rationale="React/Ink presentation is a frontend unit; backend approval is already deterministic."),
    _claude("src/services/mcp/client.ts", SourceDisposition.ACTIVE,
            ("initialize handshake", "tools/resources/prompts", "server request routing"),
            targets=(_CONNECTION, _PROTOCOL, _CAPABILITIES), entry=_CONNECTION_ENTRY,
            test=_TRANSPORT_TEST, evidence=_CONNECTION_CHAIN),
    _claude("src/services/mcp/MCPConnectionManager.tsx", SourceDisposition.ADAPTER,
            ("connection ownership", "status projection", "reconnect coordination"),
            targets=(_CONNECTION, _STORE, _EVENTS), entry=_CONNECTION_ENTRY,
            test=_TRANSPORT_TEST, evidence=_CONNECTION_CHAIN,
            limitations=("React context is replaced by Zyra state custody",)),
    _claude("src/services/mcp/useManageMCPConnections.ts", SourceDisposition.ACTIVE,
            ("list-change refresh", "stale cleanup", "elicitation and instructions callbacks"),
            targets=(_CONNECTION, _CAPABILITIES, _ELICITATION, _INSTRUCTIONS), entry=_CONNECTION_ENTRY,
            test=_CAPABILITY_TEST, evidence=_CONNECTION_CHAIN),
    _claude("src/services/mcp/InProcessTransport.ts", SourceDisposition.ACTIVE,
            ("in-process request/notification exchange", "deterministic fake-server transport"),
            targets=(_TRANSPORT_CONTRACTS, _PROTOCOL), entry=_CONNECTION_ENTRY,
            test=_TRANSPORT_TEST, evidence=_CONNECTION_CHAIN),
    _claude("src/services/mcp/SdkControlTransport.ts", SourceDisposition.ADAPTER,
            ("control-channel request/reply", "transport close propagation"),
            targets=(_TRANSPORT_CONTRACTS, _PROTOCOL), entry=_CONNECTION_ENTRY,
            test=_TRANSPORT_TEST, evidence=_CONNECTION_CHAIN),
    _claude("src/services/mcp/vscodeSdkMcp.ts", SourceDisposition.DEFERRED,
            ("editor-provided MCP registration", "SDK lifecycle bridge"), targets=(_CONNECTION,),
            replacement="the MCP config/API boundary accepts editor adapters without owning editor state",
            next_owner="M2-04A", rationale="VS Code host integration is outside the backend foundation."),
    _claude("src/utils/mcpWebSocketTransport.ts", SourceDisposition.REFERENCE_ONLY,
            ("WebSocket framing", "close and reconnect notification"),
            targets=(_TRANSPORT_STDIO, _TRANSPORT_HTTP, _TRANSPORT_SSE),
            replacement="stdio, in-process, Streamable HTTP and SSE transports",
            rationale="WebSocket MCP is not required for this foundation and cannot bypass transport policy."),
    _claude("src/services/mcp/claudeai.ts", SourceDisposition.ADAPTER,
            ("provider-hosted server discovery", "provider identity mapping"), targets=(_CONFIG, _AUTH),
            entry=_CONFIG_ENTRY, test=_CONFIG_TEST, evidence=_CONFIG_CHAIN,
            limitations=("provider discovery is an input source, never a privileged policy source",)),
    _claude("src/services/mcp/auth.ts", SourceDisposition.ACTIVE,
            ("OAuth lifecycle", "needs-auth cache", "refresh and revoke"), targets=(_AUTH, _CREDENTIALS),
            entry=_AUTH_ENTRY, test=_AUTH_TEST, evidence=_CONTROL_CHAIN),
    _claude("src/services/mcp/oauthPort.ts", SourceDisposition.ACTIVE,
            ("loopback callback port lease", "state-bound callback"), targets=(_AUTH,),
            entry=_AUTH_ENTRY, test=_AUTH_TEST, evidence=_CONTROL_CHAIN),
    _claude("src/services/mcp/xaa.ts", SourceDisposition.ACTIVE,
            ("XAA metadata", "step-up scopes", "identity-provider selection"), targets=(_AUTH,),
            entry=_AUTH_ENTRY, test=_AUTH_TEST, evidence=_CONTROL_CHAIN),
    _claude("src/services/mcp/xaaIdpLogin.ts", SourceDisposition.ADAPTER,
            ("IdP login challenge", "step-up completion proposal"), targets=(_AUTH,),
            entry=_AUTH_ENTRY, test=_AUTH_TEST, evidence=_CONTROL_CHAIN,
            limitations=("interactive browser/UI is a caller; durable auth state stays in Zyra",)),
    _claude("src/tools/MCPTool/MCPTool.ts", SourceDisposition.ACTIVE,
            ("remote tool invocation", "server/tool provenance", "result normalization"),
            targets=(_PROJECTION, _OUTPUT), entry=_PROJECTION_ENTRY, test=_WORKER_TEST, evidence=_TOOL_CHAIN),
    _claude("src/tools/MCPTool/prompt.ts", SourceDisposition.CONTRACT_ONLY,
            ("MCP tool presentation guidance",), targets=(_PROJECTION,),
            replacement="schema-derived ToolSpec metadata; prompt prose is a runtime asset"),
    _claude("src/tools/MCPTool/classifyForCollapse.ts", SourceDisposition.REFERENCE_ONLY,
            ("result-collapse suggestion",), targets=(_OUTPUT,),
            replacement="deterministic output budget and artifact externalization",
            rationale="A classifier cannot own result budgets or decide whether binary data stays inline."),
    _claude("src/tools/ListMcpResourcesTool/ListMcpResourcesTool.ts", SourceDisposition.ACTIVE,
            ("resource pagination", "resource descriptor projection"), targets=(_CAPABILITIES,),
            entry=_CAPABILITY_ENTRY, test=_CAPABILITY_TEST, evidence=_TOOL_CHAIN),
    _claude("src/tools/ListMcpResourcesTool/prompt.ts", SourceDisposition.CONTRACT_ONLY,
            ("resource-list presentation guidance",), targets=(_CAPABILITIES,),
            replacement="typed resource descriptors and API diagnostics"),
    _claude("src/tools/ReadMcpResourceTool/ReadMcpResourceTool.ts", SourceDisposition.ACTIVE,
            ("resource read", "binary/text content normalization"), targets=(_CAPABILITIES, _OUTPUT),
            entry=_CAPABILITY_ENTRY, test=_CAPABILITY_TEST, evidence=_TOOL_CHAIN),
    _claude("src/tools/ReadMcpResourceTool/prompt.ts", SourceDisposition.CONTRACT_ONLY,
            ("resource-read presentation guidance",), targets=(_CAPABILITIES,),
            replacement="typed read result plus artifact references"),
    _claude("src/tools/McpAuthTool/McpAuthTool.ts", SourceDisposition.ADAPTER,
            ("auth control tool", "server-scoped login request"), targets=(_AUTH,),
            entry=_AUTH_ENTRY, test=_API_TEST, evidence=_CONTROL_CHAIN),
    _claude("src/utils/mcpOutputStorage.ts", SourceDisposition.ACTIVE,
            ("large result spill", "binary artifact storage", "safe inline preview"), targets=(_OUTPUT,),
            entry=_OUTPUT_ENTRY, test=_CAPABILITY_TEST, evidence=_TOOL_CHAIN),
    _claude("src/utils/mcpValidation.ts", SourceDisposition.ACTIVE,
            ("tool input schema validation", "server/tool identity validation"), targets=(_PROJECTION, _MODELS),
            entry=_PROJECTION_ENTRY, test=_WORKER_TEST, evidence=_TOOL_CHAIN),
    _claude("src/commands/mcp/index.ts", SourceDisposition.ADAPTER,
            ("MCP command registration", "diagnostic command routing"), targets=(_CONNECTION,),
            entry=_CONNECTION_ENTRY, test=_API_TEST, evidence=_CONTROL_CHAIN),
    _claude("src/commands/mcp/addCommand.ts", SourceDisposition.ADAPTER,
            ("validated server add", "scope selection and approval"), targets=(_CONFIG,),
            entry=_CONFIG_ENTRY, test=_API_TEST, evidence=_CONTROL_CHAIN),
    _claude("src/commands/mcp/mcp.tsx", SourceDisposition.DEFERRED,
            ("interactive MCP status panel", "auth and reconnect actions"), targets=(_CONNECTION,),
            replacement="live backend diagnostics and control API", next_owner="M2-04A",
            rationale="The TUI view is deferred; a static source crosswalk is not completion evidence."),
    _claude("src/commands/mcp/xaaIdpCommand.ts", SourceDisposition.ADAPTER,
            ("XAA IdP control command", "step-up response"), targets=(_AUTH,),
            entry=_AUTH_ENTRY, test=_API_TEST, evidence=_CONTROL_CHAIN),
    _claude("src/constants/prompts.ts", SourceDisposition.CONTRACT_ONLY,
            ("server-instruction placement", "untrusted instruction delimiter"), targets=(_INSTRUCTIONS,),
            replacement="provenance-bearing instruction deltas restored as external-untrusted context"),
    _claude("src/utils/attachments.ts", SourceDisposition.ACTIVE,
            ("mcp_instructions_delta attachment", "session-scoped restore input"), targets=(_INSTRUCTIONS,),
            entry=_INSTRUCTIONS_ENTRY, test=_API_TEST, evidence=_CONTROL_CHAIN),
    _claude("src/utils/mcpInstructionsDelta.ts", SourceDisposition.ACTIVE,
            ("replace/clear instruction delta", "generation and digest"), targets=(_INSTRUCTIONS,),
            entry=_INSTRUCTIONS_ENTRY, test=_API_TEST, evidence=_CONTROL_CHAIN),
    _claude("src/services/mcp/channelAllowlist.ts", SourceDisposition.ADAPTER,
            ("channel allowlist", "server/channel binding"), targets=(_CONFIG, _AUTH),
            entry=_CONFIG_ENTRY, test=_CONFIG_TEST, evidence=_CONTROL_CHAIN),
    _claude("src/services/mcp/channelNotification.ts", SourceDisposition.ADAPTER,
            ("notification projection", "delivery-safe metadata"), targets=(_EVENTS, _CONNECTION),
            entry=_CONNECTION_ENTRY, test=_API_TEST, evidence=_CONTROL_CHAIN),
    _claude("src/services/mcp/channelPermissions.ts", SourceDisposition.ADAPTER,
            ("channel permission proposal", "server identity propagation"), targets=(_PROJECTION,),
            entry=_PROJECTION_ENTRY, test=_WORKER_TEST, evidence=_TOOL_CHAIN,
            limitations=("M1-03A ToolPermissionRuntime remains authoritative",)),
    _claude("src/services/mcp/elicitationHandler.ts", SourceDisposition.ACTIVE,
            ("elicitation create", "resolve/cancel/expiry", "sensitive response sink"),
            targets=(_ELICITATION, _EVENTS), entry=_ELICITATION_ENTRY, test=_API_TEST, evidence=_CONTROL_CHAIN),
    _claude("src/skills/mcpSkills.ts", SourceDisposition.REFERENCE_ONLY,
            ("MCP skill extension point",), targets=(_CAPABILITIES,),
            replacement="MCP resource primitives now; runnable MCP skill loading is owned by M1-03C",
            next_owner="M1-03C",
            rationale="The upstream file is generated/no-op and cannot close a runnable skill gate."),

    # Agent Framework supplies protocol mechanisms missing or incomplete in the
    # Claude implementation, notably pagination and long-running tasks.
    _decision("agent-framework", "python/packages/core/agent_framework/_mcp.py",
              SourceDisposition.ACTIVE,
              ("lifecycle owner queue", "pagination", "sampling guard", "long task poll/cancel/reconnect"),
              targets=(_CONNECTION, _CAPABILITIES, _SAMPLING, _TASKS), entry=_CONNECTION_ENTRY,
              test=_CAPABILITY_TEST, evidence=_CONNECTION_CHAIN,
              limitations=("Zyra defaults sampling to deny and never replays an outcome-unknown tool call",)),
    _decision("agent-framework", "python/packages/core/agent_framework/_skills.py",
              SourceDisposition.DEFERRED,
              ("MCPSkillsSource index discovery", "lazy remote resource reads"), targets=(_CAPABILITIES,),
              replacement="MCP resources are active; SkillRuntime binding is not claimed by this slice",
              next_owner="M1-03C", rationale="Skill loading has a separate state owner and acceptance unit."),

    # opencode complements lifecycle with a strong catalog, session projection,
    # token concurrency and Streamable HTTP recovery implementation.
    _decision("opencode", "packages/opencode/src/mcp/index.ts", SourceDisposition.ACTIVE,
              ("stdio/HTTP/SSE lifecycle", "roots/instructions", "dynamic list notifications"),
              targets=(_CONNECTION, _TRANSPORT_STDIO, _TRANSPORT_HTTP, _TRANSPORT_SSE, _CAPABILITIES),
              entry=_CONNECTION_ENTRY,
              test=_TRANSPORT_TEST, evidence=_CONNECTION_CHAIN),
    _decision("opencode", "packages/opencode/src/mcp/catalog.ts", SourceDisposition.ACTIVE,
              ("tool catalog conversion", "structured content and schema fallback"),
              targets=(_CAPABILITIES, _PROJECTION, _OUTPUT), entry=_CAPABILITY_ENTRY,
              test=_CAPABILITY_TEST, evidence=_TOOL_CHAIN),
    _decision("opencode", "packages/opencode/src/mcp/auth.ts", SourceDisposition.ADAPTER,
              ("concurrent token update", "auth status", "server-scoped token lookup"),
              targets=(_AUTH, _CREDENTIALS), entry=_AUTH_ENTRY, test=_AUTH_TEST, evidence=_CONTROL_CHAIN),
    _decision("opencode", "packages/opencode/src/mcp/oauth-provider.ts", SourceDisposition.ADAPTER,
              ("OAuth provider contract", "PKCE verifier/state", "refresh persistence"),
              targets=(_AUTH, _CREDENTIALS), entry=_AUTH_ENTRY, test=_AUTH_TEST, evidence=_CONTROL_CHAIN),
    _decision("opencode", "packages/opencode/src/mcp/oauth-callback.ts", SourceDisposition.ADAPTER,
              ("callback state binding", "bounded listener lifecycle"), targets=(_AUTH,),
              entry=_AUTH_ENTRY, test=_AUTH_TEST, evidence=_CONTROL_CHAIN),
    _decision("opencode", "packages/opencode/src/mcp/browser.ts", SourceDisposition.DEFERRED,
              ("browser launch abstraction", "auth URL presentation"), targets=(_AUTH,),
              replacement="auth runtime returns a safe authorization URI to an explicit UI adapter",
              next_owner="M2-04A", rationale="Opening a browser is not a backend core decision."),
    _decision("opencode", "packages/opencode/src/session/tools.ts", SourceDisposition.ACTIVE,
              ("MCP tool/resource session projection", "permission pattern propagation"),
              targets=(_PROJECTION,), entry=_PROJECTION_ENTRY, test=_WORKER_TEST, evidence=_TOOL_CHAIN),
    _decision("opencode", "packages/core/src/config.ts", SourceDisposition.CONTRACT_ONLY,
              ("V2 MCP config slot", "typed integration boundary"), targets=(_CONFIG,),
              replacement="McpConfigStore source/precedence/approval model",
              rationale="The typed slot informs compatibility but does not own Zyra state."),

    # AgentScope remains a lightweight Python adapter/reference.  Its sandbox
    # gateway is explicitly not smuggled into this client-runtime slice.
    _decision("agentscope", "src/agentscope/mcp/_config.py", SourceDisposition.ADAPTER,
              ("stdio/http discriminated config", "stateful/stateless constraint"),
              targets=(_MODELS, _CONFIG), entry=_CONFIG_ENTRY, test=_CONFIG_TEST, evidence=_CONFIG_CHAIN),
    _decision("agentscope", "src/agentscope/mcp/_mcp_client.py", SourceDisposition.REFERENCE_ONLY,
              ("stateful/stateless client wrapper", "connect-on-call model"), targets=(_CONNECTION,),
              replacement="Zyra connection ownership plus explicit reconnect/outcome-unknown state",
              rationale="The lightweight wrapper lacks Zyra session/event/auth custody."),
    _decision("agentscope", "src/agentscope/tool/_adapters.py", SourceDisposition.ADAPTER,
              ("MCPTool schema preservation", "readOnlyHint permission projection", "tool-name sanitization"),
              targets=(_PROJECTION,), entry=_PROJECTION_ENTRY, test=_WORKER_TEST, evidence=_TOOL_CHAIN,
              limitations=("readOnlyHint is risk evidence, not an unconditional allow",)),
    _decision("agentscope", "src/agentscope/workspace/_mcp_gateway/_mcp_gateway_app.py",
              SourceDisposition.DEFERRED,
              ("sandbox-local MCP gateway", "gateway tool/resource routes"), targets=(_CONNECTION,),
              replacement="direct Zyra-owned transports in this slice", next_owner="M1-05B",
              rationale="Sandbox gateway custody belongs to workspace/sandbox runtime and cannot be a sidecar shortcut."),

    # Hermes contributes hardened lifecycle/OAuth details.  Its default-enabled
    # sampling and its MCP-server direction are intentionally not adopted.
    _decision("hermes-agent", "tools/mcp_tool.py", SourceDisposition.ACTIVE,
              ("dedicated server lifecycle", "schema normalization", "dynamic stale cleanup", "elicitation context replay"),
              targets=(_CONNECTION, _CAPABILITIES, _PROJECTION, _ELICITATION), entry=_CONNECTION_ENTRY,
              test=_CAPABILITY_TEST, evidence=_CONNECTION_CHAIN,
              limitations=("Hermes default-enabled sampling is rejected; Zyra McpSamplingRuntime is default-deny",)),
    _decision("hermes-agent", "tools/mcp_oauth.py", SourceDisposition.ADAPTER,
              ("OAuth 2.1 PKCE", "absolute expiry", "atomic restricted credential persistence"),
              targets=(_AUTH, _CREDENTIALS), entry=_AUTH_ENTRY, test=_AUTH_TEST, evidence=_CONTROL_CHAIN),
    _decision("hermes-agent", "tools/mcp_oauth_manager.py", SourceDisposition.ADAPTER,
              ("401 dedupe", "invalid-client poison", "metadata cache and reconnect signal"),
              targets=(_AUTH, _CREDENTIALS, _CONNECTION), entry=_AUTH_ENTRY,
              test=_AUTH_TEST, evidence=_CONTROL_CHAIN),
    _decision("hermes-agent", "mcp_serve.py", SourceDisposition.DEFERRED,
              ("Zyra-as-MCP-server control bridge", "event cursor and long poll"), targets=(_PROTOCOL,),
              replacement="this unit owns an MCP client, not a server facade", next_owner="M3-01A",
              rationale="DB polling and server export do not participate in the client runtime main path."),
    _decision("hermes-agent", "gateway/run.py", SourceDisposition.REFERENCE_ONLY,
              ("gateway session routing", "MCP reconnect control"), targets=(_CONNECTION, _EVENTS),
              replacement="Zyra session/event/control-plane ownership",
              rationale="Hermes gateway state and platform routing are not imported into Zyra."),
)


REQUIRED_SOURCE_PATHS: Mapping[str, frozenset[str]] = {
    "claude-code-best": frozenset({
        "src/services/mcp/types.ts",
        "src/services/mcp/config.ts",
        "src/services/mcp/envExpansion.ts",
        "src/services/mcp/normalization.ts",
        "src/services/mcp/utils.ts",
        "src/services/mcp/mcpStringUtils.ts",
        "src/services/mcp/headersHelper.ts",
        "src/services/mcp/officialRegistry.ts",
        "src/components/MCPServerApprovalDialog.tsx",
        "src/services/mcp/client.ts",
        "src/services/mcp/MCPConnectionManager.tsx",
        "src/services/mcp/useManageMCPConnections.ts",
        "src/services/mcp/InProcessTransport.ts",
        "src/services/mcp/SdkControlTransport.ts",
        "src/services/mcp/vscodeSdkMcp.ts",
        "src/utils/mcpWebSocketTransport.ts",
        "src/services/mcp/claudeai.ts",
        "src/services/mcp/auth.ts",
        "src/services/mcp/oauthPort.ts",
        "src/services/mcp/xaa.ts",
        "src/services/mcp/xaaIdpLogin.ts",
        "src/tools/MCPTool/MCPTool.ts",
        "src/tools/MCPTool/prompt.ts",
        "src/tools/MCPTool/classifyForCollapse.ts",
        "src/tools/ListMcpResourcesTool/ListMcpResourcesTool.ts",
        "src/tools/ListMcpResourcesTool/prompt.ts",
        "src/tools/ReadMcpResourceTool/ReadMcpResourceTool.ts",
        "src/tools/ReadMcpResourceTool/prompt.ts",
        "src/tools/McpAuthTool/McpAuthTool.ts",
        "src/utils/mcpOutputStorage.ts",
        "src/utils/mcpValidation.ts",
        "src/commands/mcp/index.ts",
        "src/commands/mcp/addCommand.ts",
        "src/commands/mcp/mcp.tsx",
        "src/commands/mcp/xaaIdpCommand.ts",
        "src/constants/prompts.ts",
        "src/utils/attachments.ts",
        "src/utils/mcpInstructionsDelta.ts",
        "src/services/mcp/channelAllowlist.ts",
        "src/services/mcp/channelNotification.ts",
        "src/services/mcp/channelPermissions.ts",
        "src/services/mcp/elicitationHandler.ts",
        "src/skills/mcpSkills.ts",
    }),
    "agent-framework": frozenset({
        "python/packages/core/agent_framework/_mcp.py",
        "python/packages/core/agent_framework/_skills.py",
    }),
    "opencode": frozenset({
        "packages/opencode/src/mcp/index.ts",
        "packages/opencode/src/mcp/catalog.ts",
        "packages/opencode/src/mcp/auth.ts",
        "packages/opencode/src/mcp/oauth-provider.ts",
        "packages/opencode/src/mcp/oauth-callback.ts",
        "packages/opencode/src/mcp/browser.ts",
        "packages/opencode/src/session/tools.ts",
        "packages/core/src/config.ts",
    }),
    "agentscope": frozenset({
        "src/agentscope/mcp/_config.py",
        "src/agentscope/mcp/_mcp_client.py",
        "src/agentscope/tool/_adapters.py",
        "src/agentscope/workspace/_mcp_gateway/_mcp_gateway_app.py",
    }),
    "hermes-agent": frozenset({
        "tools/mcp_tool.py",
        "tools/mcp_oauth.py",
        "tools/mcp_oauth_manager.py",
        "mcp_serve.py",
        "gateway/run.py",
    }),
}


def _valid_target(path: str) -> bool:
    pure = PurePosixPath(path)
    if path.startswith("/") or (pure.parts and ":" in pure.parts[0]):
        return False
    lowered = {part.casefold() for part in pure.parts}
    if lowered & {"vendor", "vendor-runtimes", "source-pool", "runtime-sources", "third_party"}:
        return False
    return bool(pure.parts) and pure.parts[0] in {"packages", "apps", "skills", "scripts"}


def audit_mcp_sources(
    decisions: Iterable[McpSourceDecision] = MCP_SOURCE_DECISIONS,
    *,
    required_source_paths: Mapping[str, Iterable[str]] = REQUIRED_SOURCE_PATHS,
) -> SourceAuditReport:
    materialized = tuple(decisions)
    issues: list[SourceAuditIssue] = []
    keys: set[str] = set()

    for decision in materialized:
        if decision.key in keys:
            issues.append(SourceAuditIssue(decision.key, "duplicate", "source was decided more than once"))
        keys.add(decision.key)
        if not decision.mechanisms:
            issues.append(SourceAuditIssue(decision.key, "missing-mechanism", "no mechanism was identified"))
        if not decision.target_paths:
            issues.append(SourceAuditIssue(decision.key, "missing-target", "no Zyra target/replacement boundary"))
        for target in decision.target_paths:
            if not _valid_target(target):
                issues.append(SourceAuditIssue(decision.key, "invalid-target", f"unsafe or non-product target: {target}"))

        if decision.claims_runtime_ownership:
            if not decision.runtime_entry:
                issues.append(SourceAuditIssue(decision.key, "missing-entry", "active source has no runtime entry"))
            if not decision.test_target:
                issues.append(SourceAuditIssue(decision.key, "missing-test", "active source has no behavior test"))
            elif decision.test_target not in _ALLOWED_TEST_TARGETS:
                issues.append(SourceAuditIssue(decision.key, "unknown-test", decision.test_target))
            if not decision.main_path_evidence:
                issues.append(SourceAuditIssue(decision.key, "missing-main-path", "active source has no main-path evidence"))
        elif not (decision.replacement or decision.next_owner or decision.rationale):
            issues.append(SourceAuditIssue(
                decision.key,
                "unexplained-downgrade",
                "contract/reference/deferred decision lacks replacement, owner, or rationale",
            ))

    by_repo: dict[str, set[str]] = {}
    for decision in materialized:
        by_repo.setdefault(decision.repository, set()).add(decision.source_path)
        registered = required_source_paths.get(decision.repository)
        if registered is None or decision.source_path not in set(registered):
            issues.append(SourceAuditIssue(
                decision.key,
                "unregistered-source",
                "decision is not present in the explicit parent source requirement set",
            ))
    for repository, required in required_source_paths.items():
        present = by_repo.get(repository, set())
        for missing in sorted(set(required) - present):
            issues.append(SourceAuditIssue(
                f"{repository}:{missing}",
                "required-source-missing",
                "M1-03B requires an explicit source disposition",
            ))

    counts = Counter(decision.disposition for decision in materialized)
    return SourceAuditReport(materialized, tuple(issues), dict(counts))


def source_decision(repository: str, source_path: str) -> McpSourceDecision:
    key = f"{repository}:{source_path}"
    for decision in MCP_SOURCE_DECISIONS:
        if decision.key == key:
            return decision
    raise KeyError(key)


def assert_mcp_source_coverage() -> SourceAuditReport:
    return audit_mcp_sources().require_complete()


__all__ = [
    "MCP_SOURCE_DECISIONS",
    "REQUIRED_SOURCE_PATHS",
    "McpSourceDecision",
    "SourceAuditError",
    "SourceAuditIssue",
    "SourceAuditReport",
    "SourceAuthority",
    "SourceDisposition",
    "assert_mcp_source_coverage",
    "audit_mcp_sources",
    "source_decision",
]
