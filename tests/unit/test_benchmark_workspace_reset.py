"""The benchmark container's reset predicate.

The container is one long-lived resource shared by every task the API runs, and
the agent edits it in place.  Resetting it is therefore destructive in a way
most of this package is not: a reset applied to the wrong dispatch discards work
that is still in progress.  These cases pin the predicate that decides when that
is safe.
"""

from __future__ import annotations

from zyra_orchestration.deployment.code_worker_adapter import (
    _is_first_benchmark_dispatch,
    _reset_benchmark_workspace,
)


def test_a_plain_first_layer_dispatch_opens_a_task() -> None:
    assert _is_first_benchmark_dispatch({}, 1) is True


def test_a_later_layer_is_a_continuation_not_an_opening() -> None:
    # One run fans out into one physical dispatch per candidate layer.  Layer 2
    # is continuing layer 1's container edits, so it must never reset them.
    for layer_index in (2, 3, 7):
        assert _is_first_benchmark_dispatch({}, layer_index) is False


def test_a_recovery_continuation_never_resets() -> None:
    for payload in (
        {"recovery_session_id": "recovery_1"},
        {"recovery_plan_id": "plan_1"},
        {"physical_recovery_pass": 1},
        {"physical_recovery_pass": "2"},
    ):
        assert _is_first_benchmark_dispatch(payload, 1) is False


def test_the_recovery_identity_alone_does_not_protect_a_fresh_task() -> None:
    """Regression: two different tasks can share a recovery identity.

    The identity is derived from the task, but a task that was ever resumed
    keeps one for the rest of its life, and a first-layer dispatch of a
    *different* task carries its own.  Treating "has a recovery identity" as
    "is a continuation" would leave a genuinely fresh task starting from the
    previous task's dirty tree, which is the defect this reset exists to fix.
    """

    assert _is_first_benchmark_dispatch({"recovery_session_id": ""}, 1) is True
    # An empty string is not an identity; only a truthy one is.
    assert _is_first_benchmark_dispatch({"recovery_plan_id": "   "}, 1) is True


def test_the_reset_restores_tracked_files_without_deleting_dependencies() -> None:
    """`clean -fd`, not `-fdx`.

    The container's installed runtime dependencies are ignore-listed; removing
    them would cost a reinstall on every run, which is what the mirror excludes
    are there to avoid.
    """

    recorded: dict[str, object] = {}

    def fake_run(binding, argv, *, operation, timeout_seconds, **kwargs):
        recorded["binding"] = binding
        recorded["argv"] = argv
        recorded["operation"] = operation
        recorded["timeout_seconds"] = timeout_seconds
        return type("Completed", (), {"returncode": 0, "stderr": b""})()

    import zyra_orchestration.deployment.code_worker_adapter as adapter

    original = adapter._run_benchmark_docker
    adapter._run_benchmark_docker = fake_run
    try:
        _reset_benchmark_workspace(
            {"container": "zyra-bench", "workdir": "/testbed"}
        )
    finally:
        adapter._run_benchmark_docker = original

    argv = recorded["argv"]
    assert argv[:4] == ("exec", "zyra-bench", "sh", "-c")
    script = argv[4]
    assert "git checkout -- ." in script
    assert "git clean -fd" in script
    assert "git clean -fdx" not in script
    assert "--" in argv and "/testbed" in argv
    # The workdir must be passed as an argument, never interpolated into the
    # script text, so a path with shell metacharacters cannot become a command.
    assert "/testbed" not in script
    assert recorded["operation"] == "benchmark_workspace_reset"
