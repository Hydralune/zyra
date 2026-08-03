"""Freeze the immutable Execution 04 (M1-R01) G0 input set.

This generator deliberately runs before any E04 production edit.  It reuses the
already frozen E01-E03 source inventories, revalidates every selected blob at
the immutable upstream commit, inventories the current target owner, captures
the two known default-path failures, and writes the seven schema-v4 inputs.

The frozen authority inputs ship under ``zyra/provenance``. This script refuses
to overwrite them; a bad freeze must be explicitly abandoned instead of edited
in place. A new freeze additionally requires an explicit upstream source
workspace, so verification never reaches outside the Zyra repository by default.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


SCHEMA_VERSION = "4.0"
EXECUTION_ID = "E04"
GENERATOR_VERSION = "1.3.0"
BASELINE_COMMIT = "299b708d3559da7a5da1f9d6d55d2d1f1b155249"
BASELINE_TREE = "897924b1d7b47fe5dcfdf6f0ea91d8b0a717fb00"

ZYRA_ROOT = Path(__file__).resolve().parents[2]
INTEGRATIONS_ROOT = ZYRA_ROOT / "packages" / "integrations"
if str(INTEGRATIONS_ROOT) not in sys.path:
    sys.path.insert(0, str(INTEGRATIONS_ROOT))

from zyra_integrations.source_provenance import BundledSourceProvenance  # noqa: E402

AUTHORITY_ROOT = ZYRA_ROOT / "provenance" / "authority" / "m1-r01-claude-source-custody"
MANIFEST_ROOT = AUTHORITY_ROOT / "manifests"
_EXTERNAL_SOURCE_WORKSPACE: Path | None = None

SOURCE_REPOS = {
    "claude-code-best": {
        "commit": "c57f5a29e88e9a814bea47abeb9a0a6f725dc102",
        "tree": "38ad533a122dd662c12dec5aada600eba480baae",
    },
    "opencode": {
        "commit": "adf178a6b95c61506ddaadaf4dd062badb4a8fda",
        "tree": "9aabba801accd1b3fd0da7b80bee5ecc9ffaba85",
    },
    "OpenClaw": {
        "commit": "b63e06f68aa0f5fc3dc809c37615b8b1012b180b",
        "tree": "ff37a77d65d9d1e0a4767c09cabe5865e284e6c2",
    },
}

DOMAINS = (
    "query_loop",
    "session_context_compact",
    "tool_orchestration",
    "permission",
    "mcp",
    "skill_plugin_command",
    "agent_subagent",
    "isolation_control",
)


@dataclass(frozen=True)
class RecoverySelection:
    legacy_manifest: str
    legacy_mapping_id: str
    semantic_domain: str
    target_path: str
    target_symbol: str
    default_entry_id: str
    state_store: str
    state_effect_kind: str
    state_effect_assertion: str
    anchor_kind: str
    retained_semantics: str
    crop_reason: str


SELECTIONS = (
    RecoverySelection(
        "execution-01-source-manifest.jsonl", "e01-src-0006", "query_loop",
        "packages/runtime/claude-runtime/src/query/lifecycle-runtime.ts", "QueryLifecycleRuntime",
        "CodeWorkerApplication.runTaskRuntime", "QueryLifecycleSnapshot", "state_transition",
        "A user turn is admitted, reasoned, and cannot finish before its tool observations settle.",
        "call_sequence", "QueryEngine.ask is the mature user-input to query-loop admission boundary.",
        "Remove React rendering, telemetry and product UI branches; retain input, loop and failure ordering.",
    ),
    RecoverySelection(
        "execution-01-source-manifest.jsonl", "e01-rej-0002", "query_loop",
        "packages/runtime/claude-runtime/src/query/lifecycle-runtime.ts", "QueryLifecycleRuntime",
        "CodeWorkerApplication.runTaskRuntime", "QueryLifecycleSnapshot", "failure_route",
        "The continuation loop observes tool/model outcomes and revises until a deterministic terminal reason.",
        "loop", "The tail of queryLoop contains mature continuation, stop and error routing.",
        "Remove UI message rendering and provider telemetry while preserving loop termination and observation flow.",
    ),
    RecoverySelection(
        "execution-01-source-manifest.jsonl", "e01-src-0180", "session_context_compact",
        "packages/runtime/claude-runtime/src/compact/compaction-custody-runtime.ts", "CompactionSourceCustodyRuntime",
        "CodeWorkerApplication.runTaskRuntime", "CompactCheckpointStore", "checkpoint",
        "Automatic compaction checks budget and boundary eligibility before replacing context.",
        "branch", "autoCompactIfNeeded is the upstream budget-triggered compaction decision path.",
        "Replace Claude settings/telemetry access with Zyra budget and checkpoint ports.",
    ),
    RecoverySelection(
        "execution-01-source-manifest.jsonl", "e01-src-0296", "session_context_compact",
        "packages/runtime/claude-runtime/src/compact/restore-runtime.ts", "CompactRestoreRuntime",
        "CodeWorkerApplication.runTaskRuntime", "CompactCheckpointStore", "restore",
        "Resume reconstructs post-compact context in stable order and records omitted or invalid attachments.",
        "data_flow", "processResumedConversation is the mature resume and post-compact restoration flow.",
        "Replace filesystem globals with explicit Zyra attachment, artifact and checkpoint inputs.",
    ),
    RecoverySelection(
        "execution-01-source-manifest.jsonl", "e01-src-0231", "tool_orchestration",
        "packages/runtime/claude-runtime/src/tools/execution-runtime.ts", "ToolExecutionRuntime",
        "CodeWorkerApplication.runTaskRuntime", "ToolExecutionSnapshot", "tool_execution",
        "Tool calls are partitioned, leased, executed and observed with serial/concurrent ordering intact.",
        "call_sequence", "runTools is the mature tool-batch execution coordinator.",
        "Replace Claude tool context and UI progress with Zyra registry, permission and event ports.",
    ),
    RecoverySelection(
        "execution-01-source-manifest.jsonl", "e01-src-0232", "tool_orchestration",
        "packages/runtime/claude-runtime/src/tools/execution-runtime.ts", "ToolExecutionRuntime",
        "CodeWorkerApplication.runTaskRuntime", "ToolExecutionSnapshot", "route",
        "Concurrency-safe tools are separated from serial side-effecting tools before execution.",
        "branch", "partitionToolCalls carries the upstream serial versus parallel safety decision.",
        "Use Zyra tool metadata and effect classes while retaining partition ordering.",
    ),
    RecoverySelection(
        "execution-01-source-manifest.jsonl", "e01-src-0271", "tool_orchestration",
        "packages/runtime/claude-runtime/src/tools/result-runtime.ts", "ToolResultRuntime",
        "CodeWorkerApplication.runTaskRuntime", "ToolResultAccumulator", "budget",
        "Oversized tool results are truncated or persisted before they enter the next model context.",
        "data_flow", "enforceToolResultBudget is the mature tool-result budget and spill path.",
        "Replace Claude storage globals with Zyra artifact receipts and deterministic token accounting.",
    ),
    RecoverySelection(
        "execution-02-source-manifest.jsonl", "e02-src-0001", "permission",
        "packages/runtime/claude-runtime/src/permission/evaluator.ts", "PermissionEvaluator",
        "CodeWorkerApplication.runCapabilityApiPort", "PermissionDecisionStore", "permission_decision",
        "Coordinator permission requests preserve hook ordering and resolve exactly once.",
        "call_sequence", "handleCoordinatorPermission is the mature coordinator approval boundary.",
        "Replace terminal UI prompts with Zyra ask transport and durable continuation records.",
    ),
    RecoverySelection(
        "execution-02-source-manifest.jsonl", "e02-src-0157", "permission",
        "packages/runtime/claude-runtime/src/permission/evaluator.ts", "PermissionEvaluator",
        "CodeWorkerApplication.runCapabilityApiPort", "PermissionDecisionStore", "permission_decision",
        "Rule precedence deterministically selects deny, ask or allow before default policy.",
        "branch", "checkRuleBasedPermissions contains the upstream rule-precedence decision path.",
        "Use Zyra canonical identities and grants while preserving deny/ask/allow precedence.",
    ),
    RecoverySelection(
        "execution-02-source-manifest.jsonl", "e02-src-0489", "mcp",
        "packages/integrations/claude-mcp/src/connection/connection-runtime.ts", "McpConnectionRuntime",
        "CodeWorkerApplication.runCapabilityApiPort", "McpConnectionStore", "mcp_request",
        "Connection setup selects transport/auth, initializes once and routes failure to reconnect or terminal state.",
        "failure_route", "The selected connectToServer branch covers mature transport and connection failure routing.",
        "Remove Claude UI notifications and global caches; retain transport/auth/initialize ordering.",
    ),
    RecoverySelection(
        "execution-02-source-manifest.jsonl", "e02-src-0540", "mcp",
        "packages/integrations/claude-mcp/src/runtime/client-runtime.ts", "McpClientRuntime",
        "CodeWorkerApplication.runCapabilityApiPort", "McpRequestJournal", "mcp_request",
        "Every capability request requires a connected client and fails closed when the session is unavailable.",
        "branch", "ensureConnectedClient is the upstream live-client guard used by MCP capability calls.",
        "Replace module globals with the Zyra connection registry and request journal.",
    ),
    RecoverySelection(
        "execution-02-source-manifest.jsonl", "e02-src-1021", "skill_plugin_command",
        "packages/runtime/claude-runtime/src/skills/runtime.ts", "TypeScriptSkillRuntime",
        "CodeWorkerApplication.runCapabilityApiPort", "SkillRegistryStore", "skill_invocation",
        "Skill discovery validates roots, reads frontmatter and registers deterministic command identities.",
        "call_sequence", "loadSkillsFromSkillsDir is the mature discovery-to-registration path.",
        "Replace Claude global skill caches with Zyra roots, revisions and registry custody.",
    ),
    RecoverySelection(
        "execution-02-source-manifest.jsonl", "e02-src-1045", "skill_plugin_command",
        "packages/runtime/claude-runtime/src/skills/runtime.ts", "TypeScriptSkillRuntime",
        "CodeWorkerApplication.runCapabilityApiPort", "SkillInvocationJournal", "skill_invocation",
        "Forked skill execution constructs a bounded child context and returns its terminal result.",
        "call_sequence", "executeForkedSkill is the mature skill-to-subagent execution boundary.",
        "Replace Claude query globals with the Zyra AgentTool port and explicit budget inputs.",
    ),
    RecoverySelection(
        "execution-02-source-manifest.jsonl", "e02-src-1179", "skill_plugin_command",
        "packages/runtime/claude-runtime/src/plugins/coordinator.ts", "PluginCoordinator",
        "CodeWorkerApplication.runCapabilityApiPort", "SkillRegistryStore", "plugin_hook_dispatch",
        "Plugin hook reload swaps validated matchers atomically without duplicating active handlers.",
        "state_transition", "loadPluginHooks is the mature plugin-hook reload boundary.",
        "Replace Claude settings watchers with explicit Zyra revision and reload commands.",
    ),
    RecoverySelection(
        "execution-03-source-manifest.jsonl", "e03-src-0127", "agent_subagent",
        "packages/runtime/claude-runtime/src/agents/run-agent.ts", "runAgent",
        "CodeWorkerApplication.runAgentControlPort", "AgentExecutionStore", "agent_transition",
        "Agent run initializes scoped tools/MCP/context, executes, settles usage and publishes terminal ownership.",
        "call_sequence", "runAgent is the mature AgentTool child-run lifecycle.",
        "Replace Claude globals/UI with Zyra task, lease, budget, event and checkpoint ports.",
    ),
    RecoverySelection(
        "execution-03-source-manifest.jsonl", "e03-src-0122", "agent_subagent",
        "packages/runtime/claude-runtime/src/agents/execution-runtime.ts", "AgentBackgroundSupervisor",
        "CodeWorkerApplication.runAgentControlPort", "AgentExecutionStore", "agent_transition",
        "Background resume validates ownership, reconstructs context and resumes only a resumable terminal boundary.",
        "failure_route", "resumeAgentBackground is the mature background ownership and resume path.",
        "Replace Claude task globals with Zyra leases and durable task/checkpoint records.",
    ),
    RecoverySelection(
        "execution-03-source-manifest.jsonl", "e03-src-0178", "isolation_control",
        "packages/runtime/claude-runtime/src/isolation/request-runtime.ts", "IsolationRequestRuntime",
        "CodeWorkerApplication.runAgentControlPort", "IsolationRequestJournal", "physical_effect_request",
        "Worktree requests validate identity, reuse safe existing worktrees, or create and receipt a new isolated workspace.",
        "branch", "getOrCreateWorktree is the mature workspace isolation lifecycle.",
        "Split logical request/lease custody from the Python physical workspace effect port.",
    ),
    RecoverySelection(
        "execution-03-source-manifest.jsonl", "e03-src-0018", "isolation_control",
        "packages/runtime/claude-runtime/src/control/runtime.ts", "TypeScriptControlRuntime",
        "CodeWorkerApplication.runAgentControlPort", "ControlReceiptStore", "control_mutation",
        "Cancel/kill is idempotent, ownership checked and followed by a terminal task transition.",
        "state_transition", "killAsyncAgent is the mature cancellation and terminal-control path.",
        "Replace process globals and UI notification with Zyra control receipts and process-supervision port.",
    ),
)


# Earlier execution manifests sometimes froze parser fragments that ended before
# the executable body.  E04 recovery credit is intentionally narrower and is
# bound to the mature branch that is actually migrated.
SOURCE_RANGE_OVERRIDES = {
    "e01-src-0006": (1211, 1320),
    "e01-rej-0002": (1707, 1732),
    "e01-src-0180": (241, 351),
    "e01-src-0296": (409, 551),
    "e02-src-0001": (26, 62),
    "e02-src-0157": (1071, 1156),
    "e02-src-0489": (1022, 1083),
    "e02-src-1021": (407, 480),
    "e02-src-1045": (205, 275),
    "e02-src-1179": (91, 157),
    "e03-src-0127": (368, 502),
    "e03-src-0122": (42, 265),
    "e03-src-0178": (235, 375),
}

SOURCE_SYMBOL_OVERRIDES = {
    "e02-src-0157": "src/utils/permissions/permissions.ts::checkRuleBasedPermissions",
    "e02-src-0489": "src/services/mcp/client.ts::connectToServer",
    "e02-src-1021": "src/skills/loadSkillsDir.ts::loadSkillsFromSkillsDir",
    "e02-src-1045": "src/tools/SkillTool/SkillTool.ts::executeForkedSkill",
}

# The target symbol frozen at G0 belongs to the immutable baseline inventory.
# Candidate verification resolves these distinct executable symbols in the new
# tree instead of reusing a whole-class range for several source mechanisms.
CANDIDATE_TARGET_SYMBOLS = {
    "e01-src-0006": "ClaudeRuntimeCore.run",
    "e01-rej-0002": "QueryLifecycleRuntime.advanceAfterObservation",
    "e01-src-0180": "ContextCompactionRuntime.autoCompactIfNeeded",
    "e01-src-0296": "CompactRestoreRuntime.processResumedConversation",
    "e01-src-0231": "ToolExecutionRuntime.runTools",
    "e01-src-0232": "ToolExecutionRuntime.partitionToolCalls",
    "e01-src-0271": "ToolResultRuntime.enforceToolResultBudget",
    "e02-src-0001": "PermissionEvaluator.evaluateAsync",
    "e02-src-0157": "PermissionEvaluator.selectEffect",
    "e02-src-0489": "McpConnectionRuntime.performConnect",
    "e02-src-0540": "McpClientRuntime.ensureConnectedClient",
    "e02-src-1021": "SkillReloadRuntime.loadSkillsFromSkillsDir",
    "e02-src-1045": "TypeScriptSkillRuntime.executeForkedSkill",
    "e02-src-1179": "PluginCoordinator.replaceActiveHooks",
    "e03-src-0127": "e03ChildRunInput",
    "e03-src-0122": "AgentBackgroundSupervisor.resumeInput",
    "e03-src-0178": "IsolationRequestRuntime.prepare",
    "e03-src-0018": "TypeScriptControlRuntime.decideAgentTerminalMutation",
}

CANDIDATE_TARGET_PATHS = {
    "e01-src-0006": "packages/runtime/claude-runtime/src/query-engine.ts",
    "e01-src-0180": "packages/runtime/claude-runtime/src/compact/context-runtime.ts",
    "e02-src-1021": "packages/runtime/claude-runtime/src/skills/reload-runtime.ts",
}

TARGET_BASELINE_SYMBOLS = {
    "e01-src-0006": "ClaudeRuntimeCore",
    "e01-src-0180": "ContextCompactionRuntime",
    "e02-src-1021": "SkillReloadRuntime",
}


TARGET_EDGES = {
    "query_loop": ["runTaskRuntime", "ClaudeRuntimeCore.run", "E01RuntimeCoordinator.beginCanonicalTurn", "QueryLifecycleRuntime"],
    "session_context_compact": ["runTaskRuntime", "ClaudeRuntimeCore.run", "ContextCompactionRuntime.autoCompactIfNeeded", "CompactionSourceCustodyRuntime"],
    "tool_orchestration": ["runTaskRuntime", "PermissionedCapabilityHost.executeBatch", "ToolExecutionRuntime"],
    "permission": ["runCapabilityApiPort", "E02RuntimeCoordinator", "PermissionEvaluator"],
    "mcp": ["runCapabilityApiPort", "E02RuntimeCoordinator", "McpConnectionRuntime"],
    "skill_plugin_command": ["runCapabilityApiPort", "E02RuntimeCoordinator", "SkillCoordinator.reloadNow", "SkillReloadRuntime.loadSkillsFromSkillsDir"],
    "agent_subagent": ["runAgentControlPort", "E03RuntimeCoordinator", "runAgent"],
    "isolation_control": ["runAgentControlPort", "E03RuntimeCoordinator", "IsolationRequestRuntime"],
}

TEST_IDS = {
    "query_loop": ("e04-query-reason-observe", "e04-query-model-failure", "e04-query-resume", "e04-query-disable"),
    "session_context_compact": ("e04-compact-boundary", "e04-compact-failure", "e04-compact-restore", "e04-compact-disable"),
    "tool_orchestration": ("e04-tool-batch", "e04-tool-failure", "e04-tool-resume", "e04-tool-disable"),
    "permission": ("e04-permission-allow-ask-deny", "e04-permission-hook-failure", "e04-permission-continuation", "e04-permission-disable"),
    "mcp": ("e04-mcp-lifecycle", "e04-mcp-connect-failure", "e04-mcp-resume", "e04-mcp-disable"),
    "skill_plugin_command": ("e04-skill-plugin-command", "e04-skill-hook-failure", "e04-skill-reload", "e04-skill-disable"),
    "agent_subagent": ("e04-agent-run-resume", "e04-agent-failure", "e04-agent-resume", "e04-agent-disable"),
    "isolation_control": ("e04-isolation-control", "e04-isolation-failure", "e04-isolation-resume", "e04-isolation-disable"),
}


def run(command: list[str], cwd: Path = ZYRA_ROOT, *, check: bool = True, timeout: int = 180) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"{result.stdout.decode(errors='replace')}\n{result.stderr.decode(errors='replace')}"
        )
    return result


def git_text(root: Path, *args: str) -> str:
    return run(["git", *args], root).stdout.decode("utf-8", errors="strict").strip()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def write_json(path: Path, value: Any) -> None:
    path.write_bytes(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n")


def write_jsonl(path: Path, values: Iterable[dict[str, Any]]) -> None:
    path.write_bytes(b"".join(canonical_bytes(value) for value in values))


def safe_relative(path: str) -> str:
    posix = PurePosixPath(path)
    if posix.is_absolute() or ".." in posix.parts or any(part in {"", "."} for part in posix.parts):
        raise ValueError(f"unsafe relative path: {path}")
    return posix.as_posix()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def command_version(command: list[str], cwd: Path = ZYRA_ROOT) -> str:
    result = run(command, cwd, check=False, timeout=30)
    output = (result.stdout + result.stderr).decode("utf-8", errors="replace").strip().splitlines()
    return output[0] if output else f"exit:{result.returncode}"


def source_blob(repo: str, snapshot: str, path: str) -> bytes:
    safe_relative(path)
    if _EXTERNAL_SOURCE_WORKSPACE is not None:
        return run(
            ["git", "show", f"{snapshot}:{path}"],
            _EXTERNAL_SOURCE_WORKSPACE / repo,
        ).stdout
    identity = SOURCE_REPOS[repo]
    if snapshot != identity["commit"]:
        raise ValueError(f"bundled source snapshot mismatch: {repo}:{snapshot}")
    return BundledSourceProvenance(ZYRA_ROOT).source_file(repo, path).read_bytes()


@lru_cache(maxsize=None)
def target_blob(snapshot: str, path: str) -> bytes:
    safe_relative(path)
    return run(["git", "show", f"{snapshot}:{path}"], ZYRA_ROOT).stdout


def target_paths(snapshot: str, *roots: str) -> list[str]:
    command = ["git", "ls-tree", "-r", "--name-only", snapshot, "--", *roots]
    return [
        safe_relative(line)
        for line in run(command, ZYRA_ROOT).stdout.decode("utf-8", errors="strict").splitlines()
        if line.strip()
    ]


def normalized_fingerprint(value: str) -> str:
    without_comments = re.sub(r"/\*.*?\*/|//[^\n]*", "", value, flags=re.S)
    normalized = re.sub(r"\s+", " ", without_comments).strip()
    return sha256_bytes(normalized.encode("utf-8"))


def lines_for_symbol_text(text: str, symbol: str) -> tuple[int, int]:
    lines = text.splitlines()
    escaped = re.escape(symbol)
    pattern = re.compile(rf"\b(?:class|function)\s+{escaped}\b|\b{escaped}\s*[(:]")
    start = next((index for index, line in enumerate(lines, 1) if pattern.search(line)), 1)
    depth = 0
    opened = False
    for index in range(start - 1, len(lines)):
        line = re.sub(r"(['\"]).*?\1", "", lines[index])
        depth += line.count("{") - line.count("}")
        opened = opened or "{" in line
        if opened and depth <= 0:
            return start, index + 1
    return start, len(lines)


def lines_for_symbol(path: Path, symbol: str) -> tuple[int, int]:
    return lines_for_symbol_text(path.read_text(encoding="utf-8"), symbol)


def candidate_symbol_range_text(text: str, qualified_symbol: str) -> tuple[int, int] | None:
    lines = text.splitlines()
    parts = qualified_symbol.split(".")
    leaf = parts[-1]
    search_start = 1
    search_end = len(lines)
    if len(parts) > 1:
        owner = parts[-2]
        owner_pattern = re.compile(rf"\bclass\s+{re.escape(owner)}\b")
        owner_start = next(
            (index for index, line in enumerate(lines, 1) if owner_pattern.search(line)),
            None,
        )
        if owner_start is None:
            return None
        search_start, search_end = lines_for_symbol_text(text, owner)
        definition = re.compile(
            rf"^\s*(?:(?:public|private|protected|static|readonly|override|abstract|async)\s+)*"
            rf"{re.escape(leaf)}(?:\s*<[^>]+>)?\s*\("
        )
    else:
        definition = re.compile(
            rf"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+{re.escape(leaf)}\b"
        )
    start = next(
        (index for index in range(search_start, search_end + 1) if definition.search(lines[index - 1])),
        None,
    )
    if start is None:
        return None
    body_start_line, body_start_column = function_body_open(lines, start, search_end)
    if body_start_line is None:
        return None
    depth = 0
    for index in range(body_start_line - 1, search_end):
        line = re.sub(r"(['\"]).*?\1", "", lines[index])
        if index == body_start_line - 1:
            line = line[body_start_column:]
        depth += line.count("{") - line.count("}")
        if depth <= 0:
            return start, index + 1
    return None


def function_body_open(
    lines: list[str],
    start: int,
    limit: int,
) -> tuple[int | None, int | None]:
    paren_depth = 0
    saw_parameters = False
    parameters_closed = False
    angle_depth = 0
    return_object_depth = 0
    after_parameters = ""
    for index in range(start - 1, limit):
        line = re.sub(r"(['\"]).*?\1", "", lines[index])
        for column, character in enumerate(line):
            if not parameters_closed:
                if character == "(":
                    saw_parameters = True
                    paren_depth += 1
                elif character == ")" and saw_parameters:
                    paren_depth -= 1
                    if paren_depth == 0:
                        parameters_closed = True
                        after_parameters = ""
                continue
            if return_object_depth:
                if character == "{":
                    return_object_depth += 1
                elif character == "}":
                    return_object_depth -= 1
                after_parameters += character
                continue
            if character == "<":
                angle_depth += 1
            elif character == ">" and angle_depth:
                angle_depth -= 1
            elif character == "{" and angle_depth == 0:
                if re.search(r":\s*$", after_parameters):
                    return_object_depth = 1
                    after_parameters += character
                    continue
                return index + 1, column
            after_parameters += character
        if parameters_closed:
            after_parameters += "\n"
    return None, None


def executable_typescript(value: str) -> bool:
    without_comments = re.sub(r"/\*.*?\*/|//[^\n]*", "", value, flags=re.S)
    return "{" in without_comments and bool(re.search(
        r"\b(?:await|return|if|for|while|try|catch|throw|const|let|var)\b|\.[A-Za-z_][A-Za-z0-9_]*\s*\(",
        without_comments,
    ))


def load_selected_sources() -> list[tuple[RecoverySelection, dict[str, Any]]]:
    cache: dict[str, dict[str, dict[str, Any]]] = {}
    selected: list[tuple[RecoverySelection, dict[str, Any]]] = []
    for selection in SELECTIONS:
        if selection.legacy_manifest not in cache:
            cache[selection.legacy_manifest] = {
                row["mapping_id"]: row for row in read_jsonl(MANIFEST_ROOT / selection.legacy_manifest)
            }
        row = cache[selection.legacy_manifest].get(selection.legacy_mapping_id)
        if row is None:
            raise ValueError(f"missing frozen source mapping: {selection.legacy_mapping_id}")
        if not row.get("accepted"):
            raise ValueError(f"selected source is not accepted: {selection.legacy_mapping_id}")
        selected.append((selection, row))
    return selected


def source_recovery_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen_ranges: set[tuple[str, str, int, int]] = set()
    for index, (selection, legacy) in enumerate(load_selected_sources(), 1):
        repo = legacy["source_repo"]
        snapshot = legacy["source_snapshot"]
        path = safe_relative(legacy["source_path"])
        blob = source_blob(repo, snapshot, path)
        actual_hash = sha256_bytes(blob)
        if actual_hash != legacy["source_sha256"]:
            raise ValueError(f"frozen source hash mismatch: {selection.legacy_mapping_id}")
        start, end = SOURCE_RANGE_OVERRIDES.get(
            selection.legacy_mapping_id,
            (int(legacy["start_line"]), int(legacy["end_line"])),
        )
        source_lines = blob.decode("utf-8", errors="strict").splitlines()
        if start < 1 or end < start or end > len(source_lines):
            raise ValueError(f"invalid source range: {selection.legacy_mapping_id}")
        range_key = (repo, path, start, end)
        if range_key in seen_ranges:
            raise ValueError(f"duplicate source range: {range_key}")
        seen_ranges.add(range_key)
        records.append({
            "schema_version": SCHEMA_VERSION,
            "execution_id": EXECUTION_ID,
            "record_type": "source_recovery_range",
            "record_id": f"e04-source-{index:03d}",
            "legacy_manifest": selection.legacy_manifest,
            "legacy_mapping_id": selection.legacy_mapping_id,
            "source_repo": repo,
            "source_snapshot": snapshot,
            "source_path": path,
            "source_sha256": actual_hash,
            "source_symbol": SOURCE_SYMBOL_OVERRIDES.get(
                selection.legacy_mapping_id,
                legacy["source_symbol"],
            ),
            "start_line": start,
            "end_line": end,
            "range_sha256": sha256_bytes(("\n".join(source_lines[start - 1:end]) + "\n").encode("utf-8")),
            "source_fingerprint": normalized_fingerprint("\n".join(source_lines[start - 1:end])),
            "source_role": "primary",
            "semantic_domain": selection.semantic_domain,
            "migration_category": "adapted_migration",
            "recovery_credit": True,
            "selection_reason": selection.retained_semantics,
            "crop_reason": selection.crop_reason,
            "exception_id": None,
        })
    return records


def target_provenance_records(source_records: list[dict[str, Any]], target_snapshot: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for index, ((selection, _legacy), source) in enumerate(zip(load_selected_sources(), source_records, strict=True), 1):
        target_path = safe_relative(CANDIDATE_TARGET_PATHS.get(selection.legacy_mapping_id, selection.target_path))
        target_symbol = TARGET_BASELINE_SYMBOLS.get(selection.legacy_mapping_id, selection.target_symbol)
        target_bytes = target_blob(target_snapshot, target_path)
        target_text = target_bytes.decode("utf-8", errors="strict")
        target_lines = target_text.splitlines()
        target_start, target_end = lines_for_symbol_text(target_text, target_symbol)
        source_bytes = source_blob(source["source_repo"], source["source_snapshot"], source["source_path"])
        source_lines = source_bytes.decode("utf-8", errors="strict").splitlines()
        success_id, failure_id, restore_id, disable_id = TEST_IDS[selection.semantic_domain]
        mutation_id = f"e04-mutation-domain-{DOMAINS.index(selection.semantic_domain) + 1:02d}"
        records.append({
            "schema_version": SCHEMA_VERSION,
            "execution_id": EXECUTION_ID,
            "record_type": "target_provenance",
            "record_id": f"e04-target-{index:03d}",
            "source_record_id": source["record_id"],
            "target_snapshot_commit": target_snapshot,
            "target_path": target_path,
            "target_symbol": target_symbol,
            "candidate_target_symbol": CANDIDATE_TARGET_SYMBOLS[selection.legacy_mapping_id],
            "target_start_line": target_start,
            "target_end_line": target_end,
            "target_sha256": sha256_bytes(target_bytes),
            "canonical_owner_id": f"typescript.{selection.semantic_domain}.{target_symbol}",
            "default_entry_id": selection.default_entry_id,
            "default_entry_edges": TARGET_EDGES[selection.semantic_domain],
            "state_store": selection.state_store,
            "state_effect_kind": selection.state_effect_kind,
            "state_effect_assertion": selection.state_effect_assertion,
            "retained_control_flow_anchors": [{
                "anchor_id": f"e04-anchor-{index:03d}",
                "source_path": source["source_path"],
                "source_symbol": source["source_symbol"],
                "source_start_line": source["start_line"],
                "source_end_line": source["end_line"],
                "target_path": target_path,
                "target_symbol": target_symbol,
                "candidate_target_symbol": CANDIDATE_TARGET_SYMBOLS[selection.legacy_mapping_id],
                "target_start_line": target_start,
                "target_end_line": target_end,
                "anchor_kind": selection.anchor_kind,
                "source_fingerprint": source["source_fingerprint"],
                "target_baseline_fingerprint": normalized_fingerprint("\n".join(target_lines[target_start - 1:target_end])),
                "retained_semantics": selection.retained_semantics,
                "candidate_target_fingerprint_required": True,
            }],
            "transformation_steps": [
                "extract the selected executable branch in its original TypeScript/TSX language",
                "remove Claude UI, telemetry, global cache and product-only dependencies",
                "adapt state and side effects to the declared Zyra owner/store/port",
                "connect the migrated branch to the declared default entry closure",
            ],
            "removed_source_branches": [selection.crop_reason],
            "zyra_boundary_adaptations": [
                f"state custody -> {selection.state_store}",
                f"default entry -> {selection.default_entry_id}",
                "events, permissions, checkpoints and effects use Zyra canonical contracts",
            ],
            "success_test_ids": [success_id],
            "failure_test_ids": [failure_id],
            "restore_test_ids": [restore_id],
            "disable_test_ids": [disable_id],
            "mutation_ids": [mutation_id],
            "disconnect_effect": f"Disconnecting {target_symbol} must fail or observably change {success_id}.",
            "baseline_inventory_only": True,
        })
    return records


def python_reference_index(target_snapshot: str) -> dict[str, list[dict[str, Any]]]:
    references: dict[str, list[dict[str, Any]]] = {}
    for relative in target_paths(target_snapshot, "apps", "packages", "tests"):
        if not relative.endswith(".py"):
            continue
        raw = target_blob(target_snapshot, relative)
        try:
            tree = ast.parse(raw.decode("utf-8"), filename=relative)
        except SyntaxError as error:
            raise ValueError(f"cannot parse Python callsite inventory {relative}: {error}") from error
        for node in ast.walk(tree):
            name = ""
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                name = node.id
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                name = node.attr
            if not name:
                continue
            references.setdefault(name, []).append({
                "path": relative,
                "line": int(node.lineno),
                "kind": "test" if relative.startswith("tests/") else "callsite",
            })
    return references


def python_owner_records(target_snapshot: str) -> list[dict[str, Any]]:
    roots = [
        "packages/workers/zyra_workers/code_worker_bridge.py",
        "packages/workers/zyra_workers/code_worker_runtime.py",
        "packages/workers/zyra_workers/typescript_claude_runtime.py",
        "packages/workers/zyra_workers/subagents",
        "packages/integrations/zyra_integrations/mcp",
        "packages/runtime/zyra_runtime/permission",
        "packages/runtime/zyra_runtime/e02_ports.py",
    ]
    paths: list[str] = []
    for raw in roots:
        candidates = target_paths(target_snapshot, raw)
        if raw.endswith(".py") and raw in candidates:
            paths.append(raw)
        else:
            paths.extend(path for path in candidates if path.endswith(".py"))
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    references = python_reference_index(target_snapshot)
    default_files = {
        "packages/workers/zyra_workers/code_worker_bridge.py",
        "packages/workers/zyra_workers/code_worker_runtime.py",
        "packages/workers/zyra_workers/typescript_claude_runtime.py",
    }
    for relative in paths:
        if relative in seen:
            continue
        seen.add(relative)
        raw = target_blob(target_snapshot, relative)
        tree = ast.parse(raw.decode("utf-8"), filename=relative)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            symbol = node.name
            responsibility = "durable_or_physical_port"
            if "typescript" in relative or "bridge" in relative:
                responsibility = "typescript_process_bridge"
            elif "/mcp/" in relative:
                responsibility = "mcp_durable_credential_event_or_transport_port"
            elif "/permission/" in relative or relative.endswith("e02_ports.py"):
                responsibility = "permission_durable_receipt_or_transport_port"
            elif "/subagents/" in relative:
                responsibility = "agent_durable_schema_digest_or_typescript_port"
            symbol_references = [
                reference
                for reference in references.get(symbol, [])
                if not (
                    reference["path"] == relative
                    and int(node.lineno) <= int(reference["line"]) <= int(getattr(node, "end_lineno", node.lineno))
                )
            ]
            callsites = [
                f"{reference['path']}:{reference['line']}"
                for reference in symbol_references
                if reference["kind"] == "callsite"
            ][:32]
            tests = [
                f"{reference['path']}:{reference['line']}"
                for reference in symbol_references
                if reference["kind"] == "test"
            ][:32]
            records.append({
                "schema_version": SCHEMA_VERSION,
                "execution_id": EXECUTION_ID,
                "record_type": "python_owner_symbol",
                "record_id": "",  # assigned after stable sorting
                "target_snapshot_commit": target_snapshot,
                "path": relative,
                "symbol": symbol,
                "start_line": int(node.lineno),
                "end_line": int(getattr(node, "end_lineno", node.lineno)),
                "sha256": sha256_bytes(raw),
                "responsibility_domain": responsibility,
                "disposition": "retain_port",
                "default_reachable": relative in default_files and bool(callsites),
                "can_advance_logical_state": False,
                "can_select_policy_or_route": False,
                "can_fallback_for_typescript": False,
                "allowed_physical_durable_responsibility": responsibility,
                "callsites": callsites,
                "tests": tests,
                "callsite_scan_complete": True,
                "retained_reason": "Typed durable/process/physical boundary; all Claude local-control decisions remain in TypeScript.",
                "deletion_or_block_reason": None,
            })
    records.sort(key=lambda row: (row["path"], row["start_line"], row["symbol"]))
    for index, record in enumerate(records, 1):
        record["record_id"] = f"e04-python-{index:04d}"
    return records


def mutation_records(target_records: list[dict[str, Any]], target_snapshot: str) -> list[dict[str, Any]]:
    first_by_domain: dict[str, dict[str, Any]] = {}
    source_by_id = {row["record_id"]: row for row in source_recovery_records()}
    for target in target_records:
        domain = source_by_id[target["source_record_id"]]["semantic_domain"]
        first_by_domain.setdefault(domain, target)

    records: list[dict[str, Any]] = []
    protocol_target = {
        "path": "packages/runtime/claude-runtime/src/stdio.ts",
        "symbol": "JsonlRuntimeHost",
    }

    specs: list[tuple[str, str, str, str, str, str]] = [
        ("e04-mutation-terminal-order", "terminal_protocol", protocol_target["path"], protocol_target["symbol"], "emit run.result before the final checkpoint/effect ACK", "e04-terminal-order"),
        ("e04-mutation-checkpoint-ack-loss", "terminal_protocol", protocol_target["path"], protocol_target["symbol"], "drop the final checkpoint ACK once", "e04-terminal-checkpoint-before-ack"),
        ("e04-mutation-checkpoint-ack-duplicate", "terminal_protocol", protocol_target["path"], protocol_target["symbol"], "deliver the final checkpoint ACK twice", "e04-terminal-duplicate-ack"),
        ("e04-mutation-python-host-disconnect", "terminal_protocol", protocol_target["path"], protocol_target["symbol"], "disconnect the Python host during terminal transition", "e04-terminal-host-disconnect"),
        ("e04-mutation-typescript-disconnect", "terminal_protocol", protocol_target["path"], protocol_target["symbol"], "kill the TypeScript owner during terminal transition", "e04-terminal-typescript-disconnect"),
        ("e04-mutation-python-fallback", "python_fallback", "packages/workers/zyra_workers/typescript_claude_runtime.py", "TypeScriptClaudeRuntime", "enable a local Python completion fallback after TypeScript failure", "e04-typescript-disable"),
        ("e04-mutation-root-dependency", "dependency", "packages/workers/zyra_workers/typescript_claude_runtime.py", "TypeScriptClaudeRuntime", "resolve runtime code from ../claude-code-best", "e04-root-dependency"),
    ]
    for index, domain in enumerate(DOMAINS, 1):
        target = first_by_domain[domain]
        specs.append((
            f"e04-mutation-domain-{index:02d}",
            {
                "permission": "permission",
                "mcp": "mcp",
                "skill_plugin_command": "skill_plugin_command",
                "agent_subagent": "agent_control",
            }.get(domain, "source_recovery"),
            target["target_path"],
            target["target_symbol"],
            f"disconnect the migrated {domain} owner from the default entry closure",
            TEST_IDS[domain][3],
        ))
    for index, (record_id, family, path, symbol, purpose, killer) in enumerate(specs, 1):
        raw = target_blob(target_snapshot, path)
        start, end = lines_for_symbol_text(raw.decode("utf-8", errors="strict"), symbol)
        operator = {"record_id": record_id, "path": path, "symbol": symbol, "purpose": purpose}
        records.append({
            "schema_version": SCHEMA_VERSION,
            "execution_id": EXECUTION_ID,
            "record_type": "mutation_point",
            "record_id": record_id,
            "target_snapshot_commit": target_snapshot,
            "target_path": path,
            "target_symbol": symbol,
            "target_start_line": start,
            "target_end_line": end,
            "target_sha256": sha256_bytes(raw),
            "mutation_patch_fingerprint": sha256_bytes(canonical_bytes(operator)),
            "family": family,
            "mutation_purpose": purpose,
            "exact_killer_test": killer,
            "observed_state_or_effect": purpose,
            "apply_command": f"python scripts/remediation/run_m1_r01_e04_mutations.py --id {record_id}",
            "restore_command": f"python scripts/remediation/run_m1_r01_e04_mutations.py --restore {record_id}",
            "compile_survival_expectation": "The mutation must compile unless the family is dependency or python_fallback; its killer test must still fail.",
        })
    return records


def capture(command: list[str], *, timeout: int = 240) -> dict[str, Any]:
    result = run(command, ZYRA_ROOT, check=False, timeout=timeout)
    stdout = result.stdout.decode("utf-8", errors="replace")
    stderr = result.stderr.decode("utf-8", errors="replace")
    combined = stdout + "\n" + stderr
    error_codes = sorted(set(re.findall(r"(?:code[=: ]+|['\"])(host_disconnected|worker_result_not_ok|runtime_[a-z_]+)(?:['\"])?", combined)))
    return {
        "command": command,
        "exit_code": result.returncode,
        "error_codes": error_codes,
        "stdout_sha256": sha256_bytes(result.stdout),
        "stderr_sha256": sha256_bytes(result.stderr),
        "combined_sha256": sha256_bytes((result.stdout + b"\n" + result.stderr)),
        "stdout_tail": stdout[-1600:],
        "stderr_tail": stderr[-1600:],
    }


def frozen_failure_results() -> list[dict[str, Any]]:
    python = str(ZYRA_ROOT / ".venv" / "Scripts" / "python.exe")
    tests = [
        (
            "default_code_worker_reaches_typescript_owner",
            [python, "-m", "pytest", "tests/integration/test_e01_typescript_runtime_cutover.py::test_default_code_worker_reaches_typescript_owner", "-vv", "-p", "no:cacheprovider", "--basetemp", ".tmp/e04-g0-owner"],
            "host_disconnected",
        ),
        (
            "clean_productized_default_runtime",
            [python, "-m", "pytest", "tests/integration/test_code_worker_clean_productized_runtime.py::CodeWorkerCleanProductizedRuntimeTests::test_default_runtime_runs_without_source_workspace_or_sidecar", "-vv", "-p", "no:cacheprovider", "--basetemp", ".tmp/e04-g0-clean"],
            "worker_result_not_ok",
        ),
    ]
    results: list[dict[str, Any]] = []
    for test_id, command, expected_error in tests:
        result = capture(command)
        if result["exit_code"] == 0:
            raise RuntimeError(f"frozen failure unexpectedly passed before E04 production edits: {test_id}")
        result.update({"test_id": test_id, "expected_error_code": expected_error})
        results.append(result)
    return results


def gate_profile(target_snapshot: str) -> dict[str, Any]:
    bun = ".\\node_modules\\.bin\\bun.exe"
    python = ".venv\\Scripts\\python.exe"
    return {
        "schema_version": SCHEMA_VERSION,
        "execution_id": EXECUTION_ID,
        "record_type": "gate_profile",
        "record_id": "e04-gate-profile",
        "profile_version": "1.0.0",
        "target_snapshot_commit": target_snapshot,
        "baseline_manifest_root": "provenance/authority/m1-r01-claude-source-custody/manifests",
        "candidate_output_root": "docs/reviews/evidence/M1-R01-v4/execution-04",
        "validators": [
            f"{python} scripts/remediation/verify_m1_r01_e04_g0.py",
            f"{python} scripts/remediation/verify_m1_r01_e04_candidate.py",
        ],
        "typecheck_build_test_commands": [
            f"{bun} run typecheck:e02",
            f"{bun} run build",
            f"{bun} test packages/runtime/claude-runtime/test packages/integrations/claude-mcp/test",
            f"{python} -m pytest tests/integration/test_e01_typescript_runtime_cutover.py tests/integration/test_code_worker_clean_productized_runtime.py -p no:cacheprovider --basetemp .tmp/e04-pytest",
        ],
        "default_path_commands": [
            f"{bun} apps/code-worker/src/main.ts --stdio-probe",
            f"{bun} dist/code-worker/main.js --stdio-probe",
            "node dist/code-worker-node/main.js --stdio-probe",
            f"{python} scripts/remediation/probe_m1_r01_e04.py python-bridge",
            f"{python} scripts/remediation/probe_m1_r01_e04.py api-route",
        ],
        "crash_restore_disable_commands": [
            f"{python} scripts/remediation/probe_m1_r01_e04.py checkpoint-before-ack",
            f"{python} scripts/remediation/probe_m1_r01_e04.py final-checkpoint-before-terminal",
            f"{python} scripts/remediation/probe_m1_r01_e04.py lost-ack",
            f"{python} scripts/remediation/probe_m1_r01_e04.py host-disconnect",
            f"{python} scripts/remediation/probe_m1_r01_e04.py typescript-disconnect",
            f"{python} scripts/remediation/probe_m1_r01_e04.py duplicate-ack",
            f"{python} scripts/remediation/probe_m1_r01_e04.py disable",
            f"{python} scripts/remediation/run_m1_r01_e04_mutations.py --all",
        ],
        "cleanroom_command": f"{python} scripts/remediation/cleanroom_m1_r01_e04.py",
        "forbidden_dependency_patterns": [
            "../claude-code-best", "../opencode", "../OpenClaw", "../openclaw",
            "vendor-runtimes", "runtime-sources", "source-pool", "npm link", "file:../", "-e ../",
        ],
        "line_bucket_command": f"{python} scripts/remediation/verify_m1_r01_e04_candidate.py --line-buckets",
        "similarity_diagnostic_command": f"{python} scripts/remediation/verify_m1_r01_e04_candidate.py --similarity",
        "reviewer_required_outputs": [
            "baseline-receipt.json", "source-recovery-report.json", "target-provenance-report.jsonl",
            "reimplementation-exceptions.json", "default-path-result.json", "terminal-protocol-crash-matrix.json",
            "python-owner-result.json", "dependency-audit.json", "build-and-test-result.json",
            "cleanroom-result.json", "mutation-results.json", "line-buckets.json", "candidate-gate-result.json",
        ],
        "thresholds": {
            "credited_domain_count": 8,
            "default_path_required_passes": 8,
            "mutation_kill_rate": 1.0,
            "python_logical_owner_count": 0,
            "forbidden_dependency_count": 0,
            "dirty_cleanroom_path_count": 0,
            "terminal_fault_points": 5,
            "minimum_restart_epochs": 2,
        },
    }


def verify_source_repos() -> list[dict[str, Any]]:
    if _EXTERNAL_SOURCE_WORKSPACE is None:
        raise ValueError("a new G0 freeze requires an explicit source workspace")
    snapshots: list[dict[str, Any]] = []
    for name, expected in SOURCE_REPOS.items():
        root = _EXTERNAL_SOURCE_WORKSPACE / name
        commit = git_text(root, "rev-parse", "HEAD")
        tree = git_text(root, "rev-parse", "HEAD^{tree}")
        dirty = git_text(root, "status", "--porcelain=v1", "--untracked-files=all").splitlines()
        if commit != expected["commit"] or tree != expected["tree"] or dirty:
            raise RuntimeError(f"source snapshot mismatch or dirty worktree: {name}")
        snapshots.append({"repo": name, "commit": commit, "tree": tree, "clean_worktree": True})
    return snapshots


def archive_abandoned_g0(
    output_names: list[str],
    *,
    reason: str,
    failed_candidate: str,
) -> Path:
    old_receipt_path = MANIFEST_ROOT / "execution-04-baseline-receipt.json"
    old_receipt = json.loads(old_receipt_path.read_text(encoding="utf-8"))
    old_tooling = str(
        old_receipt.get("g0_tooling_head")
        or old_receipt.get("verified_zyra_head")
        or "unknown"
    )
    archive_root = MANIFEST_ROOT / "abandoned" / f"execution-04-g0-{old_tooling[:12]}"
    if archive_root.exists():
        raise RuntimeError(f"refusing to overwrite abandoned G0 archive: {archive_root}")
    archive_root.mkdir(parents=True)
    archived_hashes: dict[str, str] = {}
    for name in output_names:
        source = MANIFEST_ROOT / name
        if not source.is_file():
            raise RuntimeError(f"cannot archive incomplete G0: {name}")
        destination = archive_root / name
        shutil.copy2(source, destination)
        archived_hashes[name] = sha256_file(destination)
    write_json(archive_root / "abandonment-receipt.json", {
        "schema_version": SCHEMA_VERSION,
        "execution_id": EXECUTION_ID,
        "record_type": "g0_abandonment_receipt",
        "record_id": f"e04-g0-abandoned-{old_tooling[:12]}",
        "abandoned_g0_tooling_commit": old_tooling,
        "failed_candidate": failed_candidate,
        "reason": reason,
        "archived_manifest_sha256": archived_hashes,
        "replacement_tooling_commit": git_text(ZYRA_ROOT, "rev-parse", "HEAD"),
        "archived_at": datetime.now(timezone.utc).isoformat(),
    })
    return archive_root


def freeze(
    *,
    refreeze_reason: str = "",
    failed_candidate: str = "",
    inherit_abandoned: str = "",
) -> None:
    MANIFEST_ROOT.mkdir(parents=True, exist_ok=True)
    output_names = [
        "execution-04-baseline-receipt.json",
        "execution-04-source-recovery-manifest.jsonl",
        "execution-04-target-provenance-map.jsonl",
        "execution-04-reimplementation-exceptions.jsonl",
        "execution-04-python-owner-census.jsonl",
        "execution-04-mutation-manifest.jsonl",
        "execution-04-gate-profile.json",
    ]
    existing = [name for name in output_names if (MANIFEST_ROOT / name).exists()]
    abandoned_archive: Path | None = None
    recovered_interrupted_refreeze = False
    if inherit_abandoned:
        if existing:
            raise RuntimeError("cannot inherit an abandoned G0 while active manifests exist")
        if not re.fullmatch(r"execution-04-g0-[0-9a-f]{12}", inherit_abandoned):
            raise RuntimeError("invalid abandoned G0 recovery directory")
        abandoned_archive = (MANIFEST_ROOT / "abandoned" / inherit_abandoned).resolve()
        abandoned_root = (MANIFEST_ROOT / "abandoned").resolve()
        if abandoned_archive.parent != abandoned_root or not abandoned_archive.is_dir():
            raise RuntimeError(f"abandoned G0 recovery directory was not found: {inherit_abandoned}")
        missing_recovery = [
            name for name in output_names if not (abandoned_archive / name).is_file()
        ]
        if missing_recovery:
            raise RuntimeError(f"abandoned G0 recovery set is incomplete: {missing_recovery}")
        recovered_interrupted_refreeze = True
    if existing and refreeze_reason and failed_candidate:
        abandoned_archive = archive_abandoned_g0(
            output_names,
            reason=refreeze_reason,
            failed_candidate=failed_candidate,
        )
        for name in output_names:
            (MANIFEST_ROOT / name).unlink()
        existing = []
    if existing:
        raise RuntimeError(f"refusing to overwrite immutable G0 files: {existing}")

    baseline_tree = git_text(ZYRA_ROOT, "rev-parse", f"{BASELINE_COMMIT}^{{tree}}")
    if baseline_tree != BASELINE_TREE:
        raise RuntimeError(f"baseline tree mismatch: {baseline_tree}")
    tooling_snapshot = git_text(ZYRA_ROOT, "rev-parse", "HEAD")
    target_snapshot = BASELINE_COMMIT
    dirty = git_text(ZYRA_ROOT, "status", "--porcelain=v1", "--untracked-files=all").splitlines()
    if dirty:
        raise RuntimeError(f"G0 requires a clean Zyra worktree: {dirty}")

    snapshots = verify_source_repos()
    sources = source_recovery_records()
    targets = target_provenance_records(sources, target_snapshot)
    python_owners = python_owner_records(target_snapshot)
    mutations = mutation_records(targets, target_snapshot)
    profile = gate_profile(target_snapshot)

    source_path = MANIFEST_ROOT / "execution-04-source-recovery-manifest.jsonl"
    target_path = MANIFEST_ROOT / "execution-04-target-provenance-map.jsonl"
    exception_path = MANIFEST_ROOT / "execution-04-reimplementation-exceptions.jsonl"
    python_path = MANIFEST_ROOT / "execution-04-python-owner-census.jsonl"
    mutation_path = MANIFEST_ROOT / "execution-04-mutation-manifest.jsonl"
    profile_path = MANIFEST_ROOT / "execution-04-gate-profile.json"
    write_jsonl(source_path, sources)
    write_jsonl(target_path, targets)
    write_jsonl(exception_path, [])
    write_jsonl(python_path, python_owners)
    write_jsonl(mutation_path, mutations)
    write_json(profile_path, profile)

    local_bun = Path(os.environ.get(
        "ZYRA_BUN_EXECUTABLE",
        str(ZYRA_ROOT / "node_modules" / ".bin" / "bun.exe"),
    )).resolve()
    if not local_bun.is_file():
        raise RuntimeError(f"Bun executable is required: {local_bun}")
    lockfile = ZYRA_ROOT / "bun.lock"
    if not lockfile.is_file():
        raise RuntimeError("bun.lock is required")
    inherited_baseline_receipt: dict[str, Any] | None = None
    if abandoned_archive is not None:
        inherited_baseline_receipt = json.loads(
            (abandoned_archive / "execution-04-baseline-receipt.json").read_text(encoding="utf-8")
        )
        frozen_failures = list(inherited_baseline_receipt["frozen_default_path_failures"])
        entry_baseline = dict(inherited_baseline_receipt["entry_baseline"])
    else:
        frozen_failures = frozen_failure_results()
        entry_baseline = {
            "source_health": capture([str(local_bun), "apps/code-worker/src/main.ts", "--health"], timeout=60),
            "built_bun_health": capture([str(local_bun), "dist/code-worker/main.js", "--health"], timeout=60),
            "built_node_health": capture(["node", "dist/code-worker-node/main.js", "--health"], timeout=60),
            "observation": "Health probes can pass while the real Python-to-TypeScript stdio terminal handshake fails; E04 gates real stdio runs separately.",
        }

    other_manifests = {
        path.name: sha256_file(path)
        for path in (source_path, target_path, exception_path, python_path, mutation_path, profile_path)
    }
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "execution_id": EXECUTION_ID,
        "record_type": "baseline_receipt",
        "record_id": "e04-baseline-receipt",
        "zyra_baseline_commit": BASELINE_COMMIT,
        "zyra_baseline_tree": BASELINE_TREE,
        "verified_zyra_head": BASELINE_COMMIT,
        "verified_zyra_tree": BASELINE_TREE,
        "g0_tooling_head": tooling_snapshot,
        "g0_tooling_tree": git_text(ZYRA_ROOT, "rev-parse", "HEAD^{tree}"),
        "target_inventory_commit": target_snapshot,
        "target_inventory_tree": BASELINE_TREE,
        "refreeze": {
            "performed": bool(refreeze_reason),
            "reason": refreeze_reason or None,
            "failed_candidate": failed_candidate or None,
            "abandoned_archive": str(abandoned_archive.relative_to(ZYRA_ROOT)).replace("\\", "/") if abandoned_archive else None,
            "inherited_baseline_receipt_sha256": (
                sha256_file(abandoned_archive / "execution-04-baseline-receipt.json")
                if abandoned_archive
                else None
            ),
            "recovered_interrupted_refreeze": recovered_interrupted_refreeze,
        },
        "clean_worktree": True,
        "dirty_paths": [],
        "source_snapshots": snapshots,
        "toolchain": {
            "bun": command_version([str(local_bun), "--version"]),
            "typescript": command_version(["node", str(ZYRA_ROOT / "node_modules" / "typescript" / "bin" / "tsc"), "--version"]),
            "node": command_version(["node", "--version"]),
            "python": command_version([str(ZYRA_ROOT / ".venv" / "Scripts" / "python.exe"), "--version"]),
        },
        "lockfile_path": "bun.lock",
        "lockfile_sha256": sha256_file(lockfile),
        "manifest_sha256": other_manifests,
        "frozen_default_path_failures": frozen_failures,
        "entry_baseline": entry_baseline,
        "generator": {
            "command": [str(ZYRA_ROOT / ".venv" / "Scripts" / "python.exe"), "scripts/remediation/m1_r01_e04_g0.py", "freeze"],
            "version": GENERATOR_VERSION,
        },
        "verifier": {
            "command": [str(ZYRA_ROOT / ".venv" / "Scripts" / "python.exe"), "scripts/remediation/verify_m1_r01_e04_g0.py"],
            "version": GENERATOR_VERSION,
        },
        "capture_timestamp": datetime.now(timezone.utc).isoformat(),
        "platform": {"system": platform.system(), "release": platform.release(), "machine": platform.machine()},
        "workspace_boundary": {
            "workspace_root": "G:/agent-zoo",
            "zyra_root": "G:/agent-zoo/zyra",
            "authority_docs_outside_zyra_git": True,
            "runtime_forbidden_roots": ["claude-code-best", "opencode", "openclaw", "OpenClaw"],
        },
        "inventory": {
            "source_recovery_ranges": len(sources),
            "target_provenance_records": len(targets),
            "python_owner_symbols": len(python_owners),
            "mutation_points": len(mutations),
            "semantic_domains": list(DOMAINS),
            "reimplementation_exceptions": 0,
        },
    }
    write_json(MANIFEST_ROOT / "execution-04-baseline-receipt.json", receipt)


def verify() -> dict[str, Any]:
    names = [
        "execution-04-baseline-receipt.json",
        "execution-04-source-recovery-manifest.jsonl",
        "execution-04-target-provenance-map.jsonl",
        "execution-04-reimplementation-exceptions.jsonl",
        "execution-04-python-owner-census.jsonl",
        "execution-04-mutation-manifest.jsonl",
        "execution-04-gate-profile.json",
    ]
    missing = [name for name in names if not (MANIFEST_ROOT / name).is_file()]
    if missing:
        raise RuntimeError(f"missing E04 G0 manifests: {missing}")
    receipt = json.loads((MANIFEST_ROOT / names[0]).read_text(encoding="utf-8"))
    sources = read_jsonl(MANIFEST_ROOT / names[1])
    targets = read_jsonl(MANIFEST_ROOT / names[2])
    exceptions = read_jsonl(MANIFEST_ROOT / names[3])
    owners = read_jsonl(MANIFEST_ROOT / names[4])
    mutations = read_jsonl(MANIFEST_ROOT / names[5])
    profile = json.loads((MANIFEST_ROOT / names[6]).read_text(encoding="utf-8"))
    all_records = [receipt, *sources, *targets, *exceptions, *owners, *mutations, profile]
    ids: set[str] = set()
    for record in all_records:
        if record.get("schema_version") != SCHEMA_VERSION or record.get("execution_id") != EXECUTION_ID:
            raise ValueError(f"schema/execution mismatch: {record.get('record_id')}")
        record_id = record.get("record_id")
        if not isinstance(record_id, str) or not record_id or record_id in ids:
            raise ValueError(f"missing or duplicate record ID: {record_id}")
        ids.add(record_id)
    if receipt["zyra_baseline_commit"] != BASELINE_COMMIT or receipt["zyra_baseline_tree"] != BASELINE_TREE:
        raise ValueError("baseline identity mismatch")
    if receipt.get("verified_zyra_head") != BASELINE_COMMIT or receipt.get("verified_zyra_tree") != BASELINE_TREE:
        raise ValueError("verified baseline identity mismatch")
    tooling_head = str(receipt.get("g0_tooling_head") or "")
    tooling_tree = str(receipt.get("g0_tooling_tree") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", tooling_head) or not re.fullmatch(r"[0-9a-f]{40}", tooling_tree):
        raise ValueError("G0 tooling identity is missing or malformed")
    for name, expected in receipt["manifest_sha256"].items():
        if sha256_file(MANIFEST_ROOT / name) != expected:
            raise ValueError(f"immutable G0 manifest changed: {name}")
    source_ids = {row["record_id"] for row in sources}
    target_source_ids = {row["source_record_id"] for row in targets}
    if source_ids != target_source_ids:
        raise ValueError("credited source/provenance coverage mismatch")
    seen_ranges: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for row in sources:
        if not row["recovery_credit"] or row["migration_category"] not in {"direct_migration", "cropped_migration", "adapted_migration", "supplementary_migration"}:
            raise ValueError(f"invalid recovery credit: {row['record_id']}")
        blob = source_blob(row["source_repo"], row["source_snapshot"], row["source_path"])
        if sha256_bytes(blob) != row["source_sha256"]:
            raise ValueError(f"source blob mismatch: {row['record_id']}")
        key = (row["source_repo"], row["source_path"])
        current = (row["start_line"], row["end_line"])
        selected_text = "\n".join(blob.decode("utf-8", errors="strict").splitlines()[current[0] - 1:current[1]])
        executable_tokens = re.sub(r"/\*.*?\*/|//[^\n]*", "", selected_text, flags=re.S)
        if "{" not in executable_tokens or not re.search(
            r"\b(?:await|return|if|for|while|try|catch|throw|const|let|var)\b|\.[A-Za-z_][A-Za-z0-9_]*\s*\(",
            executable_tokens,
        ):
            raise ValueError(f"credited source range is not executable: {row['record_id']}")
        for start, end in seen_ranges.setdefault(key, []):
            if current[0] <= end and start <= current[1]:
                raise ValueError(f"overlapping credited source ranges: {row['record_id']}")
        seen_ranges[key].append(current)
    domains = {row["semantic_domain"] for row in sources}
    if domains != set(DOMAINS):
        raise ValueError(f"semantic-domain coverage mismatch: {domains}")
    mutation_ids = {row["record_id"] for row in mutations}
    candidate_symbols = [str(target.get("candidate_target_symbol") or "") for target in targets]
    if any(not symbol for symbol in candidate_symbols) or len(candidate_symbols) != len(set(candidate_symbols)):
        raise ValueError("candidate target symbols must be present and unique")
    for target in targets:
        target_bytes = run(["git", "show", f"{target['target_snapshot_commit']}:{target['target_path']}"], ZYRA_ROOT).stdout
        if sha256_bytes(target_bytes) != target["target_sha256"]:
            raise ValueError(f"target baseline hash mismatch: {target['record_id']}")
        if not target["retained_control_flow_anchors"]:
            raise ValueError(f"missing retained control-flow anchors: {target['record_id']}")
        candidate_bytes = target_blob(tooling_head, target["target_path"])
        candidate_text = candidate_bytes.decode("utf-8", errors="strict")
        candidate_range = candidate_symbol_range_text(
            candidate_text,
            target["candidate_target_symbol"],
        )
        if candidate_range is None:
            raise ValueError(
                f"candidate target symbol is absent from its declared path: {target['record_id']}"
            )
        candidate_lines = candidate_text.splitlines()
        candidate_selected = "\n".join(
            candidate_lines[candidate_range[0] - 1:candidate_range[1]]
        )
        if not executable_typescript(candidate_selected):
            raise ValueError(f"candidate target symbol is not executable: {target['record_id']}")
        if not set(target["mutation_ids"]).issubset(mutation_ids):
            raise ValueError(f"unknown mutation ID: {target['record_id']}")
    for owner in owners:
        required_owner_fields = {
            "path", "symbol", "start_line", "end_line", "sha256",
            "responsibility_domain", "disposition", "default_reachable",
            "can_advance_logical_state", "can_select_policy_or_route",
            "can_fallback_for_typescript", "allowed_physical_durable_responsibility",
            "tests", "callsites", "callsite_scan_complete",
        }
        missing_owner_fields = sorted(required_owner_fields - set(owner))
        if missing_owner_fields:
            raise ValueError(
                f"Python owner record misses schema-v4 fields: {owner['record_id']}: {missing_owner_fields}"
            )
        if not isinstance(owner["tests"], list) or not isinstance(owner["callsites"], list):
            raise ValueError(f"Python owner tests/callsites are not arrays: {owner['record_id']}")
        if owner["callsite_scan_complete"] is not True:
            raise ValueError(f"Python owner callsite scan is incomplete: {owner['record_id']}")
        if owner["disposition"] == "retain_port" and (
            owner["can_advance_logical_state"] or owner["can_select_policy_or_route"] or owner["can_fallback_for_typescript"]
        ):
            raise ValueError(f"Python retained owner exceeds port boundary: {owner['record_id']}")
        blob = run(["git", "show", f"{owner['target_snapshot_commit']}:{owner['path']}"], ZYRA_ROOT).stdout
        if sha256_bytes(blob) != owner["sha256"]:
            raise ValueError(f"Python census hash mismatch: {owner['record_id']}")
        for field in ("tests", "callsites"):
            for reference in owner[field]:
                match = re.fullmatch(r"([^:]+):(\d+)", str(reference))
                if match is None:
                    raise ValueError(f"invalid Python owner {field} reference: {owner['record_id']}: {reference}")
                reference_path, reference_line = match.group(1), int(match.group(2))
                reference_blob = target_blob(owner["target_snapshot_commit"], reference_path)
                if reference_line < 1 or reference_line > len(reference_blob.decode("utf-8").splitlines()):
                    raise ValueError(f"out-of-range Python owner {field} reference: {owner['record_id']}: {reference}")
    required_mutations = {
        "e04-mutation-terminal-order", "e04-mutation-checkpoint-ack-loss",
        "e04-mutation-checkpoint-ack-duplicate", "e04-mutation-python-fallback",
        "e04-mutation-python-host-disconnect", "e04-mutation-typescript-disconnect",
        "e04-mutation-root-dependency", *{f"e04-mutation-domain-{i:02d}" for i in range(1, 9)},
    }
    if mutation_ids != required_mutations:
        raise ValueError(f"mutation corpus mismatch: {mutation_ids ^ required_mutations}")
    if (
        profile["thresholds"]["credited_domain_count"] != 8
        or profile["thresholds"]["mutation_kill_rate"] != 1.0
        or profile["thresholds"].get("terminal_fault_points") != 5
        or profile["thresholds"].get("minimum_restart_epochs") != 2
    ):
        raise ValueError("gate thresholds were weakened")
    return {
        "ok": True,
        "baseline_commit": BASELINE_COMMIT,
        "verified_zyra_head": receipt["verified_zyra_head"],
        "source_ranges": len(sources),
        "target_records": len(targets),
        "python_owner_symbols": len(owners),
        "mutation_points": len(mutations),
        "domains": sorted(domains),
    }


def main() -> int:
    global _EXTERNAL_SOURCE_WORKSPACE
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("freeze", "refreeze", "verify"), nargs="?", default="verify")
    parser.add_argument("--abandon-reason", default="")
    parser.add_argument("--failed-candidate", default="")
    parser.add_argument("--inherit-abandoned", default="")
    parser.add_argument(
        "--source-workspace",
        help=(
            "Explicit upstream workspace required only for freeze/refreeze. "
            "Ordinary verification uses bundled immutable provenance."
        ),
    )
    args = parser.parse_args()
    if args.mode in {"freeze", "refreeze"}:
        if not args.source_workspace:
            parser.error("freeze/refreeze requires --source-workspace")
        _EXTERNAL_SOURCE_WORKSPACE = Path(args.source_workspace).resolve()
        if args.mode == "refreeze" and (not args.abandon_reason or not re.fullmatch(r"[0-9a-f]{40}", args.failed_candidate)):
            parser.error("refreeze requires --abandon-reason and a 40-hex --failed-candidate")
        freeze(
            refreeze_reason=args.abandon_reason if args.mode == "refreeze" else "",
            failed_candidate=args.failed_candidate if args.mode == "refreeze" else "",
            inherit_abandoned=args.inherit_abandoned if args.mode == "refreeze" else "",
        )
    result = verify()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
