from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from zyra_productization.release import BenchmarkEvidenceLinker
from zyra_productization.release.cleanroom import CleanInstallRunner


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CURRENT_BENCHMARK_COMMIT = "09e99cdc5ed9cf3a935ccc327f7261110e6c7d1b"


def test_release_benchmark_link_follows_the_authoritative_current_pointer() -> None:
    link = BenchmarkEvidenceLinker(PROJECT_ROOT).link(
        expected_commit=CURRENT_BENCHMARK_COMMIT,
    )
    assert link["ready"] is True
    assert link["source_commit"] == CURRENT_BENCHMARK_COMMIT
    assert (
        link["report"]
        == "docs/reviews/evidence/M3-S02A-02/"
        "formal-current-09e99cdc/benchmark-report.json"
    )
    assert set(link["providers"]) >= {"deepseek", "kimi-platform", "zhipu"}


def test_clean_install_uses_workspace_local_empty_caches(tmp_path: Path) -> None:
    host_uv_cache = tmp_path / "host-uv-cache"
    environment = CleanInstallRunner._environment(
        workspace=tmp_path / "cleanroom",
        plan=SimpleNamespace(
            environment={"UV_CACHE_DIR": str(host_uv_cache)}
        ),
        ports=(31001, 31002, 31003, 31004, 31005),
    )
    cleanroom_cache = (tmp_path / "cleanroom" / "cache").resolve()
    assert Path(environment["PIP_CACHE_DIR"]).is_relative_to(cleanroom_cache)
    assert Path(environment["UV_CACHE_DIR"]).is_relative_to(cleanroom_cache)
    assert Path(environment["BUN_INSTALL_CACHE_DIR"]).is_relative_to(
        cleanroom_cache
    )
    assert environment["UV_CACHE_DIR"] != str(host_uv_cache)
