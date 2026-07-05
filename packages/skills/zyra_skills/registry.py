from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class SkillSpec:
    name: str
    purpose: str
    source: str
    preferred_runtime: str
    vendor_paths: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)


class SkillRegistry:
    def __init__(self, skills: list[SkillSpec]) -> None:
        self._skills = {skill.name: skill for skill in skills}

    def get(self, name: str) -> SkillSpec | None:
        return self._skills.get(name)

    def list(self) -> list[SkillSpec]:
        return list(self._skills.values())


def default_skill_registry() -> SkillRegistry:
    return SkillRegistry(
        [
            SkillSpec(
                name="codebase-analysis",
                purpose="Understand repository structure, dependencies, and change surface.",
                source="claude-code-best context + OpenHands patterns",
                preferred_runtime="CodeWorkerRuntime",
                vendor_paths=("vendor/claude-code-best/src/context.ts", "vendor/claude-code-best/src/tools/AgentTool"),
                allowed_tools=("file_read", "grep", "glob"),
            ),
            SkillSpec(
                name="code-change",
                purpose="Modify code, update tests, and produce a change report.",
                source="claude-code-best Tool/AgentTool",
                preferred_runtime="CodeWorkerRuntime",
                vendor_paths=("vendor/claude-code-best/src/tools/FileEditTool", "vendor/claude-code-best/src/tools/BashTool"),
                allowed_tools=("file_read", "file_edit", "shell", "test"),
            ),
            SkillSpec(
                name="verification",
                purpose="Run independent verification and failure analysis.",
                source="claude-code-best verifier agent",
                preferred_runtime="SubagentRuntime",
                vendor_paths=("vendor/claude-code-best/src/tools/AgentTool/built-in/verificationAgent.ts",),
                allowed_tools=("shell", "file_read", "trace"),
            ),
            SkillSpec(
                name="web-research",
                purpose="Research web sources and preserve evidence refs.",
                source="browser-use + claude-code-best Web tools",
                preferred_runtime="BrowserWorker",
                vendor_paths=("vendor/browser-use/browser_use/agent", "vendor/claude-code-best/src/tools/WebSearchTool"),
                allowed_tools=("browser", "web_search", "artifact_write"),
            ),
            SkillSpec(
                name="pdf-analysis",
                purpose="Extract and summarize PDF requirements and evidence.",
                source="zyra",
                preferred_runtime="ResearchWorker",
                allowed_tools=("file_read", "artifact_write"),
            ),
            SkillSpec(
                name="report-writing",
                purpose="Assemble final traceable reports.",
                source="zyra",
                preferred_runtime="ChiefPlanner",
                allowed_tools=("artifact_read", "artifact_write"),
            ),
            SkillSpec(
                name="trace-summary",
                purpose="Summarize event traces and compact long trajectories.",
                source="claude-code-best compact",
                preferred_runtime="MemoryCurator",
                vendor_paths=("vendor/claude-code-best/src/services/compact",),
                allowed_tools=("trace", "artifact_write"),
            ),
            SkillSpec(
                name="failure-recovery",
                purpose="Recover from node failure, timeout, or failed verification.",
                source="zyra harness + claude-code-best verifier",
                preferred_runtime="Verifier",
                vendor_paths=("vendor/claude-code-best/src/tools/AgentTool/built-in/verificationAgent.ts",),
                allowed_tools=("trace", "checkpoint", "shell"),
            ),
            SkillSpec(
                name="requirement-change",
                purpose="Handle running requirement changes and local graph replanning.",
                source="zyra harness",
                preferred_runtime="ChiefPlanner",
                allowed_tools=("task_graph", "trace", "artifact_read"),
            ),
            SkillSpec(
                name="competition-demo",
                purpose="Run competition-aligned scenario demonstrations.",
                source="zyra",
                preferred_runtime="EvaluationHarness",
                allowed_tools=("scenario", "trace", "artifact_write"),
            ),
        ]
    )
