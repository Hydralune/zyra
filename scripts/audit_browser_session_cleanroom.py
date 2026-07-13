from __future__ import annotations

import importlib
import importlib.metadata
import json
import os
import site
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for package_path in sorted((ROOT / "packages").iterdir()):
    if package_path.is_dir() and str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

MODULES = (
    "zyra_integrations.browser_use.cdp_transport",
    "zyra_workers.browser_worker",
    "zyra_workers.browser_session.application",
    "zyra_workers.browser_session.runtime_registry",
    "zyra_workers.browser_session.action_runtime",
    "zyra_workers.browser_session.session_lease",
    "zyra_workers.browser_session.control_runtime",
    "zyra_workers.browser_session.canonical_ports",
    "zyra_workers.browser_session.integration_audit",
    "zyra_workers.browser_session.lifecycle_transactions",
    "zyra_workers.browser_session.resume_runtime",
    "zyra_workers.browser_session.session_projection",
    "zyra_workers.browser_context.application",
    "zyra_workers.browser_context.api_projection",
    "zyra_workers.browser_context.task_integration",
    "zyra_workers.browser_state.runtime",
    "zyra_workers.browser_state.selector_store",
    "zyra_workers.browser_action.application",
    "zyra_workers.browser_action.continuation_runtime",
    "zyra_workers.browser_action.gateway",
    "zyra_workers.browser_observability.application",
    "zyra_workers.browser_observability.history_store",
    "zyra_workers.browser_observability.restart_projection",
    "zyra_workers.browser_observability.integration.application",
    "zyra_workers.browser_observability.integration.commit_fence",
)


def main() -> int:
    forbidden = tuple(
        Path(value).resolve()
        for value in os.environ.get("ZYRA_BROWSER_FORBIDDEN_SOURCE_ROOTS", "").split(os.pathsep)
        if value.strip()
    )
    origins: dict[str, str] = {}
    violations: list[str] = []
    for name in MODULES:
        module = importlib.import_module(name)
        origin = Path(str(module.__file__)).resolve()
        origins[name] = str(origin)
        if not _inside(origin, ROOT):
            violations.append(f"productized module is outside clean root: {name}={origin}")
        if any(_inside(origin, path) for path in forbidden):
            violations.append(f"productized module resolves to forbidden source root: {name}={origin}")

    path_entries = [Path(value).resolve() for value in sys.path if value and Path(value).exists()]
    for entry in path_entries:
        if any(_inside(entry, path) for path in forbidden):
            violations.append(f"sys.path contains forbidden source root: {entry}")

    inactive_debts: list[str] = []
    pth_files: list[str] = []
    inactive_site_packages = tuple(
        Path(value).resolve()
        for value in os.environ.get("ZYRA_BROWSER_INACTIVE_SITE_PACKAGES", "").split(os.pathsep)
        if value.strip()
    )
    site_directories = tuple(dict.fromkeys(
        [Path(directory).resolve() for directory in site.getsitepackages()]
        + list(inactive_site_packages)
    ))
    for root in site_directories:
        if not root.is_dir():
            continue
        for path in root.glob("*.pth"):
            text = path.read_text(encoding="utf-8", errors="replace")
            if any(str(item).casefold() in text.casefold() for item in forbidden):
                inactive_debts.append(f"inactive editable path file references forbidden source root: {path}")
                pth_files.append(str(path))

    distribution_origins: dict[str, str] = {}
    for distribution in importlib.metadata.distributions():
        name = str(distribution.metadata.get("Name") or "").strip()
        origin = Path(distribution.locate_file("")).resolve()
        if name:
            distribution_origins[name] = str(origin)
        direct_url = distribution.read_text("direct_url.json") or ""
        if any(_inside(origin, path) for path in forbidden):
            violations.append(f"installed distribution is active from forbidden source: {name}={origin}")
        elif any(str(path).casefold() in direct_url.casefold() for path in forbidden):
            inactive_debts.append(f"installed editable distribution references forbidden source: {name}")

    for key, value in os.environ.items():
        if key == "ZYRA_BROWSER_FORBIDDEN_SOURCE_ROOTS":
            continue
        if any(str(path).casefold() in str(value).casefold() for path in forbidden):
            violations.append(f"environment exposes forbidden source root: {key}")

    if "browser_use" in sys.modules:
        origin = Path(str(getattr(sys.modules["browser_use"], "__file__", ""))).resolve()
        if any(_inside(origin, path) for path in forbidden):
            violations.append(f"browser_use imported from forbidden source root: {origin}")

    payload = {
        "ok": not violations,
        "root": str(ROOT),
        "origins": origins,
        "forbidden_roots": [str(path) for path in forbidden],
        "editable_path_files": pth_files,
        "inactive_editable_debts": inactive_debts,
        "distribution_origins": distribution_origins,
        "violations": violations,
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if not violations else 2


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
