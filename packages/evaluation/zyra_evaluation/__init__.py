from .trace import evaluate_task_trace
from .scenario_runner import (
    CallbackScenarioExecutionPort,
    EffectiveStepClassifier,
    EvidenceCollector,
    OwnerExecutionResult,
    ScenarioRegistry,
    ScenarioRunStore,
    ScenarioRunnerApi,
    ScenarioRunnerService,
    SealedPolicyRuntime,
    SourceRoleAuditor,
)


def run_m2_scenarios(*args, **kwargs):
    from .m2_scenarios import run_m2_scenarios as _run_m2_scenarios

    return _run_m2_scenarios(*args, **kwargs)


__all__ = [
    "CallbackScenarioExecutionPort",
    "EffectiveStepClassifier",
    "EvidenceCollector",
    "OwnerExecutionResult",
    "ScenarioRegistry",
    "ScenarioRunStore",
    "ScenarioRunnerApi",
    "ScenarioRunnerService",
    "SealedPolicyRuntime",
    "SourceRoleAuditor",
    "evaluate_task_trace",
    "run_m2_scenarios",
]
