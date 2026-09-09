"""Run Docker through WSL without corrupting nested shell arguments.

The Windows Docker client on this host drops ``docker exec`` stdout, while
passing a shell wrapper directly through ``wsl.exe`` rewrites its quotes.  The
argument vector is therefore carried as JSON in WSLENV and executed by a small
static Python program inside Ubuntu.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys


def main() -> int:
    # ``docker cp`` receives host paths from the Windows deployment process.
    # Docker inside WSL would parse ``C:\\...`` as a second container reference;
    # map only absolute drive paths to their WSL mount equivalents while leaving
    # container paths and ordinary command arguments untouched.
    windows_drive_path = re.compile(r"^([A-Za-z]):[\\/](.*)$")
    docker_args: list[str] = []
    for value in sys.argv[1:]:
        match = windows_drive_path.match(value)
        if match:
            docker_args.append(
                f"/mnt/{match.group(1).lower()}/{match.group(2).replace('\\\\', '/')}"
            )
        else:
            docker_args.append(value)
    args = json.dumps(docker_args)
    environment = dict(os.environ)
    environment["ZYRA_WSL_DOCKER_ARGS"] = args
    environment["DOCKER_HOST"] = "tcp://127.0.0.1:2375"
    existing = [item for item in environment.get("WSLENV", "").split(":") if item]
    for name in ("ZYRA_WSL_DOCKER_ARGS/u", "DOCKER_HOST/u"):
        if name not in existing:
            existing.append(name)
    environment["WSLENV"] = ":".join(existing)
    program = (
        "import json,os; "
        "args=json.loads(os.environ['ZYRA_WSL_DOCKER_ARGS']); "
        "os.execvp('docker', ['docker', *args])"
    )
    completed = subprocess.run(
        ["C:\\Windows\\System32\\wsl.exe", "-d", "Ubuntu", "--", "python3", "-c", program],
        env=environment,
        check=False,
    )
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
