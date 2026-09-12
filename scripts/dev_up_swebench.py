"""One-click local launch for the Zyra workbench bound to a SWE-bench container.

The sealed demo scenarios proved fault recovery and causal archiving, but their
"effective steps" are dominated by per-fragment bookkeeping and they never run
the task graph's stage nodes.  The competition asks for *thousands of steps* of
autonomous work, which is what the SWE-bench path produces: the agent reads a
real repository, edits production code, runs the project's own test suite and
iterates under a turn and token budget.

This launcher starts the same API and Web workbench as ``dev_up.py``, but with
the benchmark container exported to the API so ``POST /tasks`` runs against it.
Tasks created here appear in the workbench like any other and can be started
from the UI.

Environment:
    ZYRA_SWEBENCH_CONTAINER   container name (default zyra-flask-5014-bench)
    ZYRA_SWEBENCH_WORKDIR     repository path inside it (default /testbed)
    ZYRA_SWEBENCH_DEADLINE_MINUTES / _MAX_TURNS / _MAX_TOTAL_TOKENS
                              whole-run budget (default 45 / 80 / 600000, the
                              same limits the formal SWE-bench harness records)
    ZYRA_API_HOST / ZYRA_API_PORT / ZYRA_WEB_HOST / ZYRA_WEB_PORT

Run from the repository root:
    python scripts/dev_up_swebench.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

CONTAINER = os.environ.get(
    "ZYRA_SWEBENCH_CONTAINER", "zyra-flask-5014-bench"
)
WORKDIR = os.environ.get("ZYRA_SWEBENCH_WORKDIR", "/testbed")
BRIDGE = ROOT / "scripts" / "docker_wsl_bridge.py"

# The formal harness records one non-renewable deadline per sample.  Launching
# from the workbench has no external harness, so this launcher supplies the
# same limits; without them the run is unbounded and can outlive the demo.
DEADLINE_MINUTES = int(os.environ.get("ZYRA_SWEBENCH_DEADLINE_MINUTES", "45"))
MAX_TURNS = int(os.environ.get("ZYRA_SWEBENCH_MAX_TURNS", "80"))
MAX_TOTAL_TOKENS = int(os.environ.get("ZYRA_SWEBENCH_MAX_TOTAL_TOKENS", "600000"))
MAX_OUTPUT_TOKENS = int(os.environ.get("ZYRA_SWEBENCH_MAX_OUTPUT_TOKENS", "16384"))


def _load_credentials() -> None:
    """Load the git-ignored provider credentials into this process."""

    env_file = ROOT / ".env.deepseek.local"
    if not env_file.is_file():
        raise SystemExit(
            f"Provider credentials are missing: {env_file}\n"
            "The benchmark path fails closed rather than degrading silently."
        )
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip())


def _probe(*command: str) -> str:
    """Run a command inside the container and return its trimmed stdout.

    Every element of ``command`` is forwarded, so the program and its
    arguments must both be listed.  A failed ``docker exec`` returns a
    non-zero status and prints the runtime error on *stdout*; treating that
    text as command output is how an unreachable probe once masqueraded as a
    dirty working tree, so a non-zero status is raised instead of returned.
    """

    import subprocess

    # The bridge rewrites absolute host drive paths to WSL mounts, so keep
    # arguments verbatim and let it decide.
    completed = subprocess.run(
        [sys.executable, str(BRIDGE), "exec", CONTAINER, *command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stdout.strip() or completed.stderr.strip()
        raise SystemExit(
            "The container probe failed "
            f"({completed.returncode}): {' '.join(command)}\n{detail}"
        )
    return completed.stdout.strip()


def _restore_clean_container() -> None:
    """Start from the benchmark baseline, discarding a previous run's edits.

    A benchmark container is a disposable execution sandbox: its git working
    tree holds nothing but the SWE-bench baseline plus whatever the last run
    wrote.  Leaving those edits in place would make the new run inherit a
    partially-solved repository, so an unclean tree is reset rather than
    accepted.  (Two things other than a real edit reach ``status`` here: the
    Windows checkout uses ``core.autocrlf=true``, so ``git checkout -- .``
    restores the first line of every file; and a run killed mid-execution
    leaves its patch behind.  Both are cleared by the same reset.)

    Set ``ZYRA_SWEBENCH_KEEP_DIRTY=1`` to inspect a container instead.
    """

    if not BRIDGE.is_file():
        raise SystemExit(f"The benchmark Docker bridge is missing: {BRIDGE}")
    head = _probe("git", "-C", WORKDIR, "log", "--oneline", "-1")
    if not head:
        raise SystemExit(
            f"Container {CONTAINER} is not reachable. Start it first; a "
            "SWE-bench container should be running `sleep infinity`."
        )
    dirty = _probe("git", "-C", WORKDIR, "status", "--porcelain")
    if not dirty:
        print(f"[dev_up_swebench] container {CONTAINER} at {head} (clean)")
        return
    if os.environ.get("ZYRA_SWEBENCH_KEEP_DIRTY", "").strip().casefold() in {
        "1",
        "true",
        "yes",
    }:
        raise SystemExit(
            f"Container {CONTAINER} is not a clean baseline "
            f"({len(dirty.splitlines())} modified paths) and "
            "ZYRA_SWEBENCH_KEEP_DIRTY is set."
        )
    print(
        f"[dev_up_swebench] resetting {CONTAINER} "
        f"({len(dirty.splitlines())} paths from a previous run)"
    )
    _probe("git", "-C", WORKDIR, "checkout", "--", ".")
    remaining = _probe("git", "-C", WORKDIR, "status", "--porcelain")
    if remaining:
        raise SystemExit(
            f"Container {CONTAINER} still reports "
            f"{len(remaining.splitlines())} modified paths after a reset; "
            "inspect it before running."
        )
    print(f"[dev_up_swebench] container {CONTAINER} restored to {head}")


def main() -> int:
    _load_credentials()
    _restore_clean_container()

    os.environ["ZYRA_BENCHMARK_DOCKER_CONTAINER"] = CONTAINER
    os.environ["ZYRA_BENCHMARK_DOCKER_WORKDIR"] = WORKDIR
    os.environ["ZYRA_BENCHMARK_DOCKER_BRIDGE_SCRIPT"] = str(BRIDGE)
    os.environ["ZYRA_BENCHMARK_LONG_HORIZON"] = "true"
    os.environ.setdefault("ZYRA_MODEL_PROVIDER", "deepseek")
    os.environ.setdefault("ZYRA_MODEL", "deepseek-flash")
    # One non-renewable deadline for the whole session, exactly as the formal
    # harness supplies it.  Without it the long-horizon path has no end and the
    # workbench cannot show a bounded run.
    deadline_ms = int(time.time() * 1000) + DEADLINE_MINUTES * 60_000
    os.environ.setdefault("ZYRA_EXTERNAL_DEADLINE_EPOCH_MS", str(deadline_ms))
    os.environ.setdefault("ZYRA_REASONING_MAX_TURNS", str(MAX_TURNS))
    os.environ.setdefault("ZYRA_MAX_TOTAL_TOKENS", str(MAX_TOTAL_TOKENS))
    os.environ.setdefault("ZYRA_MAX_OUTPUT_TOKENS", str(MAX_OUTPUT_TOKENS))
    # The formal harness occupies 8420+ so its deployment workers never collide
    # with stale development workers on the default 8310--8312 profile block.
    os.environ.setdefault("ZYRA_DEPLOYMENT_PROFILE_BASE_PORT", "8420")
    print(
        f"[dev_up_swebench] budget: deadline {DEADLINE_MINUTES} min, "
        f"max turns {MAX_TURNS}, max total tokens {MAX_TOTAL_TOKENS}"
    )

    # The web API runs plain HTTP on loopback and its outbound provider calls
    # must reach the internet.  If a proxy is configured it is dropped for the
    # loopback control plane only, so a local tunnel cannot intercept the
    # workbench's own API traffic while provider egress still works.
    proxy = (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
        or ""
    ).strip().casefold()
    if proxy.startswith(("http://127.0.0.1", "http://localhost")):
        for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            os.environ.pop(name, None)
        print("[dev_up_swebench] dropped a loopback proxy for the API process")

    interpreter = _probe(
        "sh",
        "-c",
        "if [ -x /opt/miniconda3/envs/testbed/bin/python ]; then "
        "echo /opt/miniconda3/envs/testbed/bin/python; "
        "elif [ -x /opt/miniconda3/bin/python ]; then "
        "echo /opt/miniconda3/bin/python; else echo python3; fi",
    )
    if interpreter:
        os.environ["ZYRA_BENCHMARK_TEST_INTERPRETER"] = interpreter
        print(f"[dev_up_swebench] test interpreter {interpreter}")

    # A localhost proxy must never see the CLI's ephemeral loopback traffic.
    os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost,::1")
    os.environ.setdefault("no_proxy", os.environ["NO_PROXY"])

    import dev_up

    print(
        "[dev_up_swebench] SWE-bench long-horizon tasks are enabled; "
        "tool calls run inside the container."
    )
    return dev_up.main()


if __name__ == "__main__":
    raise SystemExit(main())
