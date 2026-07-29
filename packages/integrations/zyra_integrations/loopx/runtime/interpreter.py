from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .errors import LoopXRuntimeError


_VERSION_PROBE = (
    "import json,platform,sys;"
    "print(json.dumps({"
    "'implementation':platform.python_implementation(),"
    "'version':[sys.version_info.major,sys.version_info.minor,"
    "sys.version_info.micro]"
    "},sort_keys=True))"
)


def probe_python_interpreter(
    python_executable: Path,
    *,
    minimum: Sequence[int],
    requirement: str,
) -> dict[str, Any]:
    """Read the version from the selected executable, never from the host process."""

    python = python_executable.resolve()
    command = [str(python), "-I", "-c", _VERSION_PROBE]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LoopXRuntimeError(
            "The selected Python interpreter could not be inspected.",
            code="loopx_python_probe_failed",
            details={
                "python": str(python),
                "command": command,
                "error": str(error),
            },
        ) from error
    if completed.returncode != 0:
        raise LoopXRuntimeError(
            "The selected Python interpreter version probe failed.",
            code="loopx_python_probe_failed",
            details={
                "python": str(python),
                "command": command,
                "returncode": completed.returncode,
                "stdout": completed.stdout.strip(),
                "stderr": completed.stderr.strip(),
            },
        )
    try:
        payload = json.loads(completed.stdout.splitlines()[-1])
        raw_version = payload["version"]
        version = tuple(int(item) for item in raw_version)
        implementation = str(payload["implementation"])
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise LoopXRuntimeError(
            "The selected Python interpreter returned an invalid version receipt.",
            code="loopx_python_probe_failed",
            details={
                "python": str(python),
                "command": command,
                "stdout": completed.stdout.strip(),
                "stderr": completed.stderr.strip(),
            },
        ) from error
    if len(version) < len(minimum) or version[: len(minimum)] < tuple(minimum):
        minimum_text = ".".join(str(item) for item in minimum)
        raise LoopXRuntimeError(
            f"{requirement} requires Python {minimum_text} or newer.",
            code="loopx_python_incompatible",
            details={
                "python": str(python),
                "version": ".".join(str(item) for item in version),
                "minimum": minimum_text,
                "implementation": implementation,
            },
        )
    return {
        "python": str(python),
        "version": ".".join(str(item) for item in version),
        "implementation": implementation,
        "minimum": ".".join(str(item) for item in minimum),
        "compatible": True,
        "probed_selected_executable": True,
    }


__all__ = ["probe_python_interpreter"]
