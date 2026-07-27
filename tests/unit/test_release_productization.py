from __future__ import annotations

import json
import os
import tarfile
import tomllib
from pathlib import Path

import pytest

from zyra_productization.release import (
    BoundaryScanner,
    BoundaryViolation,
    BunLock,
    ChecksumBuilder,
    ChecksumVerifier,
    CleanInstallReceiptVerifier,
    ConfigurationProvisioner,
    DeterministicWheelBuilder,
    EvidenceIndex,
    EvidenceReference,
    GateExecutor,
    GateFailure,
    GateRegistry,
    GateSpec,
    InstallReceiptStore,
    IntegrityViolation,
    LockViolation,
    MigrationExecutor,
    MigrationFailure,
    MigrationRegistry,
    MigrationStep,
    OfflineArtifactResolver,
    PlatformPlanner,
    PortAvailabilityProbe,
    PythonLock,
    ReleaseAdmission,
    ReleaseInstaller,
    ReproducibilityVerifier,
    SecretRedactor,
    SubmissionAssembler,
    TransactionConflict,
)
from zyra_productization.release.bundle import (
    DeterministicArchiveWriter,
    ReleaseManifest,
)
from zyra_productization.release.errors import InstallationFailure
from zyra_productization.release.integrity import (
    CanonicalTreeWalker,
    normalize_relative_path,
    sha256_file,
    stable_digest,
)
from zyra_productization.release.models import GateState
from zyra_productization.release.policy import ReleasePolicy
from zyra_productization.release.transactions import default_migration_registry


def write_project(root: Path) -> None:
    (root / "packages" / "demo").mkdir(parents=True)
    (root / "apps" / "web").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "tests").mkdir()
    (root / "README.md").write_text("demo\n", encoding="utf-8")
    (root / "requirements.txt").write_text(
        "demo==1.2.3 \\\n"
        "    --hash=sha256:" + "a" * 64 + "\n",
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        "[project]\n"
        'name = "demo"\n'
        'version = "1.0.0"\n'
        'requires-python = ">=3.12"\n'
        'dependencies = ["demo==1.2.3"]\n',
        encoding="utf-8",
    )
    (root / "package.json").write_text(
        json.dumps(
            {
                "name": "demo",
                "version": "1.0.0",
                "packageManager": "bun@1.2.15",
                "workspaces": ["apps/web"],
                "devDependencies": {"typescript": "5.8.3"},
            }
        ),
        encoding="utf-8",
    )
    (root / "apps" / "web" / "package.json").write_text(
        json.dumps(
            {
                "name": "@demo/web",
                "version": "1.0.0",
                "dependencies": {"react": "19.1.0"},
            }
        ),
        encoding="utf-8",
    )
    (root / "bun.lock").write_text(
        '{\n'
        '  "lockfileVersion": 1,\n'
        '  "workspaces": {\n'
        '    "": {"name": "demo",},\n'
        '  },\n'
        '}\n',
        encoding="utf-8",
    )
    (root / "packages" / "demo" / "runtime.py").write_text(
        "def execute(value):\n"
        "    return value + 1\n",
        encoding="utf-8",
    )


def test_normalize_relative_path_rejects_cross_platform_escapes() -> None:
    assert normalize_relative_path("a/./b/../c") == "a/c"
    for unsafe in (
        "../outside",
        "/absolute",
        r"C:\absolute",
        "a/con.txt",
        "a/name:stream",
        "a/trailing. ",
        "a/\x00bad",
    ):
        with pytest.raises(BoundaryViolation):
            normalize_relative_path(unsafe)


def test_boundary_scanner_excludes_cache_secret_and_source_pool(
    tmp_path: Path,
) -> None:
    write_project(tmp_path)
    (tmp_path / ".env.local").write_text("TOKEN=value\n", encoding="utf-8")
    (tmp_path / ".cache").mkdir()
    (tmp_path / ".cache" / "cached.txt").write_text("cached", encoding="utf-8")
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "upstream.py").write_text("pass\n", encoding="utf-8")
    report = BoundaryScanner(tmp_path).enforce()
    assert report["ready"] is True
    assert report["included_count"] >= 7
    assert report["blocker_count"] == 0


def test_release_policy_keeps_product_artifact_feature_but_not_output_root(
    tmp_path: Path,
) -> None:
    write_project(tmp_path)
    feature = tmp_path / "apps" / "web" / "src" / "features" / "artifacts"
    feature.mkdir(parents=True)
    (feature / "contracts.ts").write_text(
        "export type Artifact = { id: string };\n",
        encoding="utf-8",
    )
    generated = tmp_path / "artifacts"
    generated.mkdir()
    (generated / "result.json").write_text("{}\n", encoding="utf-8")
    included = {
        item.relative_path
        for item in CanonicalTreeWalker(tmp_path).walk()
    }
    assert "apps/web/src/features/artifacts/contracts.ts" in included
    assert "artifacts/result.json" not in included


def test_boundary_scanner_rejects_external_manifest_dependency(
    tmp_path: Path,
) -> None:
    write_project(tmp_path)
    manifest = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))
    manifest["dependencies"] = {"bad": "file:../browser-use"}
    (tmp_path / "package.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    with pytest.raises(BoundaryViolation) as captured:
        BoundaryScanner(tmp_path).enforce()
    assert captured.value.code == "release_boundary_blocked"
    assert any(
        finding["code"] == "root_source_runtime_reference"
        for finding in captured.value.details["findings"]
    )


def test_python_lock_parses_hashes_markers_and_project_constraints(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "requirements.txt"
    lock_path.write_text(
        "alpha==1.2.3 ; python_version >= '3.12' \\\n"
        "  --hash=sha256:" + "a" * 64 + " \\\n"
        "  --hash=sha256:" + "b" * 64 + "\n"
        "bravo[fast]==4.5.6 \\\n"
        "  --hash=sha256:" + "c" * 64 + "\n",
        encoding="utf-8",
    )
    lock = PythonLock.load(lock_path)
    assert len(lock.records) == 2
    assert lock.by_name["alpha"].marker == "python_version >= '3.12'"
    assert lock.by_name["bravo"].extras == ("fast",)
    receipt = lock.verify_project_requirements(
        {"project": {"dependencies": ["alpha>=1.0", "bravo[fast]==4.5.6"]}}
    )
    assert receipt["ready"] is True
    assert receipt["hashed_requirement_count"] == 2


@pytest.mark.parametrize(
    "line,code",
    [
        ("alpha>=1.0\n", "python_lock_unpinned"),
        ("-e ../alpha\n", "python_lock_editable"),
        ("git+https://example.invalid/a.git\n", "python_lock_external_source"),
        ("alpha==1.0\n", "python_lock_hash_missing"),
        (
            "alpha==1.0 --hash=md5:" + "a" * 32 + "\n",
            "python_lock_hash_algorithm",
        ),
    ],
)
def test_python_lock_rejects_nonreproducible_inputs(
    tmp_path: Path,
    line: str,
    code: str,
) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text(line, encoding="utf-8")
    with pytest.raises(LockViolation) as captured:
        PythonLock.load(path)
    assert captured.value.code == code


def test_bun_lock_accepts_jsonc_trailing_commas_and_binds_workspace(
    tmp_path: Path,
) -> None:
    write_project(tmp_path)
    receipt = BunLock.load(tmp_path / "bun.lock").verify_workspace(tmp_path)
    assert receipt["ready"] is True
    assert receipt["package_manager"] == "bun@1.2.15"
    assert receipt["workspace_count"] == 1


def test_bun_lock_rejects_floating_and_external_dependencies(
    tmp_path: Path,
) -> None:
    write_project(tmp_path)
    manifest = json.loads(
        (tmp_path / "apps" / "web" / "package.json").read_text(encoding="utf-8")
    )
    manifest["dependencies"] = {"floating": "latest", "linked": "link:../../other"}
    (tmp_path / "apps" / "web" / "package.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    with pytest.raises(LockViolation) as captured:
        BunLock.load(tmp_path / "bun.lock").verify_workspace(tmp_path)
    assert captured.value.code == "javascript_workspace_unfrozen"
    assert captured.value.details["floating_dependencies"]
    assert captured.value.details["external_dependencies"]


def test_checksum_manifest_detects_missing_changed_and_extra(
    tmp_path: Path,
) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "one.txt").write_text("one", encoding="utf-8")
    (tmp_path / "two.txt").write_text("two", encoding="utf-8")
    manifest = ChecksumBuilder(tmp_path).build(
        generated_at="2026-07-27T00:00:00+00:00"
    )
    assert ChecksumVerifier(tmp_path).verify(manifest)["ready"] is True
    (tmp_path / "a" / "one.txt").write_text("changed", encoding="utf-8")
    (tmp_path / "two.txt").unlink()
    (tmp_path / "extra.txt").write_text("extra", encoding="utf-8")
    with pytest.raises(IntegrityViolation) as captured:
        ChecksumVerifier(tmp_path).verify(manifest)
    assert captured.value.code == "checksum_verification_failed"
    assert captured.value.details["missing"] == ["two.txt"]
    assert captured.value.details["changed"][0]["path"] == "a/one.txt"
    assert captured.value.details["extra"] == ["extra.txt"]


def test_deterministic_tar_and_zip_are_byte_reproducible(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    write_project(source)
    writer = DeterministicArchiveWriter(source)
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"
    first_zip = tmp_path / "first.zip"
    second_zip = tmp_path / "second.zip"
    writer.write_tar_gz(first, archive_root="release")
    writer.write_tar_gz(second, archive_root="release")
    writer.write_zip(first_zip, archive_root="release")
    writer.write_zip(second_zip, archive_root="release")
    assert first.read_bytes() == second.read_bytes()
    assert first_zip.read_bytes() == second_zip.read_bytes()
    assert ReproducibilityVerifier().compare(
        first,
        second,
        expected_commit="a" * 40,
    )["ready"] is True
    second.write_bytes(second.read_bytes() + b"x")
    with pytest.raises(IntegrityViolation):
        ReproducibilityVerifier().compare(
            first,
            second,
            expected_commit="a" * 40,
        )


def test_deterministic_wheel_is_installable_and_record_bound(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    package = source / "packages" / "product" / "zyra_demo"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        'VALUE = "ready"\n',
        encoding="utf-8",
    )
    (package / "data.json").write_text('{"ready":true}\n', encoding="utf-8")
    (source / "pyproject.toml").write_text(
        "[project]\n"
        'name = "zyra-demo"\n'
        'version = "1.2.3"\n'
        'requires-python = ">=3.12"\n'
        'dependencies = ["demo==1.0"]\n'
        "[project.scripts]\n"
        'zyra-demo = "zyra_demo:VALUE"\n'
        "[tool.setuptools.packages.find]\n"
        'where = ["packages/product"]\n'
        'include = ["zyra_*"]\n',
        encoding="utf-8",
    )
    builder = DeterministicWheelBuilder(source)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_receipt = builder.build(first)
    second_receipt = builder.build(second)
    first_wheel = first / str(first_receipt["path"])
    second_wheel = second / str(second_receipt["path"])
    assert first_wheel.read_bytes() == second_wheel.read_bytes()
    verification = builder.verify(first_wheel)
    assert verification["ready"] is True
    assert verification["entry_count"] >= 6
    import zipfile

    with zipfile.ZipFile(first_wheel, "a") as archive:
        archive.writestr("zyra_demo/extra.py", "MUTATED = True\n")
    with pytest.raises(IntegrityViolation) as captured:
        builder.verify(first_wheel)
    assert captured.value.code == "wheel_record_coverage"


def test_configuration_provisioning_persists_references_not_secrets(
    tmp_path: Path,
) -> None:
    template = {
        "schema": "zyra.release-configuration/v1",
        "public": {"bind_host": "127.0.0.1", "nested": {"value": 1}},
        "secrets": {"api_key": "${OPENAI_API_KEY}"},
        "profiles": {
            "device": {"network_mode": "offline"},
            "edge": {"network_mode": "limited"},
            "cloud": {"network_mode": "public"},
        },
    }
    destination = tmp_path / "config" / "installed.json"
    receipt = ConfigurationProvisioner().materialize(
        template,
        environment={"OPENAI_API_KEY": "super-secret-value"},
        destination=destination,
        require_secrets=("api_key",),
    )
    assert receipt["ready"] is True
    content = destination.read_text(encoding="utf-8")
    assert "super-secret-value" not in content
    assert "OPENAI_API_KEY" in content
    assert json.loads(content)["secret_presence"] == {"api_key": True}
    assert SecretRedactor(("super-secret-value",)).redact(
        {"token": "super-secret-value", "message": "super-secret-value"}
    ) == {"token": "<redacted>", "message": "<redacted>"}


def test_configuration_rejects_secret_literal_and_nonloopback_endpoint() -> None:
    base = {
        "schema": "zyra.release-configuration/v1",
        "public": {},
        "secrets": {"api_key": "literal"},
        "profiles": {
            "device": {},
            "edge": {},
            "cloud": {},
        },
    }
    with pytest.raises(Exception) as literal:
        ConfigurationProvisioner().validate_template(base)
    assert getattr(literal.value, "code") == "configuration_secret_literal"
    base["secrets"] = {"api_key": "${OPENAI_API_KEY}"}
    base["profiles"]["cloud"] = {"endpoint": "https://remote.example"}
    with pytest.raises(Exception) as endpoint:
        ConfigurationProvisioner().validate_template(base)
    assert getattr(endpoint.value, "code") == "configuration_profile_endpoint_unsafe"


def test_install_store_fences_revision_idempotency_and_lease(
    tmp_path: Path,
) -> None:
    with InstallReceiptStore(tmp_path / "state" / "install.sqlite3") as store:
        receipt, created = store.create(
            idempotency_key="request-1",
            request_digest="a" * 64,
            release_id="release-1",
            install_root=tmp_path / "install",
            active_path=tmp_path / "install" / "current",
            manifest_digest="b" * 64,
        )
        assert created is True
        duplicate, duplicate_created = store.create(
            idempotency_key="request-1",
            request_digest="a" * 64,
            release_id="release-1",
            install_root=tmp_path / "install",
            active_path=tmp_path / "install" / "current",
            manifest_digest="b" * 64,
        )
        assert duplicate_created is False
        assert duplicate.transaction_id == receipt.transaction_id
        with pytest.raises(TransactionConflict) as conflict:
            store.create(
                idempotency_key="request-1",
                request_digest="c" * 64,
                release_id="release-2",
                install_root=tmp_path / "install",
                active_path=tmp_path / "install" / "current",
                manifest_digest="d" * 64,
            )
        assert conflict.value.code == "install_idempotency_conflict"
        token = store.acquire_lease(
            receipt.transaction_id,
            tmp_path / "install",
        )
        second, _ = store.create(
            idempotency_key="request-2",
            request_digest="e" * 64,
            release_id="release-2",
            install_root=tmp_path / "install",
            active_path=tmp_path / "install" / "current",
            manifest_digest="f" * 64,
        )
        with pytest.raises(TransactionConflict) as leased:
            store.acquire_lease(second.transaction_id, tmp_path / "install")
        assert leased.value.code == "install_root_leased"
        store.release_lease(
            tmp_path / "install",
            transaction_id=receipt.transaction_id,
            token=token,
        )


def test_migration_executor_rolls_back_applied_steps_on_failure(
    tmp_path: Path,
) -> None:
    operations: list[str] = []

    class Context:
        transaction_id = ""
        release_root = tmp_path / "release"
        state_root = tmp_path / "state"

    def apply_one(_: object) -> dict[str, object]:
        operations.append("apply-one")
        return {"value": 1}

    def rollback_one(_: object, __: object) -> None:
        operations.append("rollback-one")

    def verify_one(_: object) -> dict[str, object]:
        return {"ready": True}

    def apply_two(_: object) -> dict[str, object]:
        operations.append("apply-two")
        raise RuntimeError("boom")

    registry = MigrationRegistry(
        (
            MigrationStep(
                migration_id="one",
                version=1,
                checksum="one",
                apply=apply_one,
                rollback=rollback_one,
                verify=verify_one,
            ),
            MigrationStep(
                migration_id="two",
                version=2,
                checksum="two",
                dependencies=("one",),
                apply=apply_two,
                rollback=lambda *_: None,
                verify=lambda _: {"ready": True},
            ),
        )
    )
    with InstallReceiptStore(tmp_path / "install.sqlite3") as store:
        receipt, _ = store.create(
            idempotency_key="migration",
            request_digest="a",
            release_id="release",
            install_root=tmp_path / "install",
            active_path=tmp_path / "install" / "current",
            manifest_digest="b",
        )
        Context.transaction_id = receipt.transaction_id
        with pytest.raises(MigrationFailure) as captured:
            MigrationExecutor(registry, store).apply(
                Context(),
                current_version=0,
                target_version=2,
            )
        assert captured.value.code == "migration_apply_failed"
        assert operations == ["apply-one", "apply-two", "rollback-one"]
        assert store.migration_journal(receipt.transaction_id)[0]["state"] == "rolled_back"


def make_install_archive(root: Path) -> Path:
    source = root / "source"
    source.mkdir()
    write_project(source)
    archive = root / "release.tar.gz"
    DeterministicArchiveWriter(source).write_tar_gz(
        archive,
        archive_root="release",
    )
    return archive


def test_release_installer_commits_replays_and_uninstalls(
    tmp_path: Path,
) -> None:
    archive = make_install_archive(tmp_path)
    install_root = tmp_path / "installed"
    with InstallReceiptStore(tmp_path / "receipts.sqlite3") as store:
        installer = ReleaseInstaller(
            store,
            migrations=MigrationExecutor(
                default_migration_registry(),
                store,
            ),
        )
        receipt = installer.install(
            archive,
            install_root=install_root,
            idempotency_key="install-1",
            release_id="v1",
            manifest_digest="a" * 64,
            target_migration_version=1,
        )
        assert receipt.state.value == "committed"
        active = Path((install_root / "current").read_text(encoding="utf-8").strip())
        assert active == install_root / "releases" / "v1"
        replay = installer.install(
            archive,
            install_root=install_root,
            idempotency_key="install-1",
            release_id="v1",
            manifest_digest="a" * 64,
            target_migration_version=1,
        )
        assert replay.transaction_id == receipt.transaction_id
        uninstalled = installer.uninstall(receipt.transaction_id)
        assert uninstalled.state.value == "uninstalled"
        assert not active.exists()


def test_release_installer_restores_prior_active_pointer_on_doctor_failure(
    tmp_path: Path,
) -> None:
    archive = make_install_archive(tmp_path)
    install_root = tmp_path / "installed"
    prior = install_root / "releases" / "prior"
    prior.mkdir(parents=True)
    (prior / "marker").write_text("prior", encoding="utf-8")
    (install_root / "current").write_text(str(prior), encoding="utf-8")
    with InstallReceiptStore(tmp_path / "receipts.sqlite3") as store:
        installer = ReleaseInstaller(
            store,
            doctor=lambda _: {"ready": False, "reason": "mutated"},
        )
        with pytest.raises(InstallationFailure) as captured:
            installer.install(
                archive,
                install_root=install_root,
                idempotency_key="install-fail",
                release_id="bad",
                manifest_digest="a" * 64,
            )
        assert captured.value.code == "install_doctor_failed"
        assert Path(
            (install_root / "current").read_text(encoding="utf-8").strip()
        ) == prior
        failed = store.by_idempotency_key("install-fail")
        assert failed is not None
        assert failed.state.value == "rolled_back"


def test_release_installer_rolls_back_committed_activation(
    tmp_path: Path,
) -> None:
    archive = make_install_archive(tmp_path)
    install_root = tmp_path / "installed"
    prior = install_root / "releases" / "prior"
    prior.mkdir(parents=True)
    (prior / "marker").write_text("prior", encoding="utf-8")
    (install_root / "current").write_text(str(prior), encoding="utf-8")
    with InstallReceiptStore(tmp_path / "receipts.sqlite3") as store:
        installer = ReleaseInstaller(
            store,
            migrations=MigrationExecutor(
                default_migration_registry(),
                store,
            ),
        )
        receipt = installer.install(
            archive,
            install_root=install_root,
            idempotency_key="install-rollback",
            release_id="v2",
            manifest_digest="a" * 64,
            target_migration_version=1,
        )
        release_path = install_root / "releases" / "v2"
        assert release_path.is_dir()
        rolled_back = installer.rollback_committed(
            receipt.transaction_id,
            target_migration_version=0,
        )
        assert rolled_back.state.value == "rolled_back"
        assert rolled_back.migration_version == 0
        assert not release_path.exists()
        assert Path(
            (install_root / "current").read_text(encoding="utf-8").strip()
        ) == prior
        assert any(
            item.kind == "release.rollback"
            for item in rolled_back.operations
        )


def test_platform_plans_cover_windows_linux_and_offline() -> None:
    planner = PlatformPlanner(Path("."))
    windows = planner.plan("windows", offline=True)
    linux = planner.plan("linux", offline=False)
    assert windows.platform == "windows"
    assert windows.environment["PIP_NO_INDEX"] == "1"
    assert any("--require-hashes" in command for command in windows.install_commands)
    assert linux.platform == "linux"
    assert linux.install_commands[0][0] == "python3"
    assert "\\" in windows.lifecycle_commands["doctor"][0]
    assert "/" in linux.lifecycle_commands["doctor"][0]


def test_port_probe_fails_when_required_port_is_occupied() -> None:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.bind(("127.0.0.1", 0))
        port = occupied.getsockname()[1]
        with pytest.raises(Exception) as captured:
            PortAvailabilityProbe().probe("127.0.0.1", (port,))
        assert getattr(captured.value, "code") == "cleanroom_port_unavailable"


def test_offline_resolver_detects_missing_and_hash_mismatch(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "requirements.txt"
    lock_path.write_text(
        "demo==1.0 --hash=sha256:" + "a" * 64 + "\n",
        encoding="utf-8",
    )
    lock = PythonLock.load(lock_path)
    wheelhouse = tmp_path / "wheels"
    wheelhouse.mkdir()
    with pytest.raises(LockViolation) as missing:
        OfflineArtifactResolver(wheelhouse).resolve(lock)
    assert missing.value.code == "offline_dependency_incomplete"
    wheel = wheelhouse / "demo-1.0-py3-none-any.whl"
    wheel.write_bytes(b"not-a-wheel")
    with pytest.raises(LockViolation) as mismatch:
        OfflineArtifactResolver(wheelhouse).resolve(lock)
    assert mismatch.value.details["hash_mismatch"]


def test_gate_registry_dependency_failure_blocks_downstream(
    tmp_path: Path,
) -> None:
    registry = GateRegistry(
        (
            GateSpec(
                gate_id="first",
                callable=lambda _: {"ready": False, "reason": "mutation"},
            ),
            GateSpec(
                gate_id="second",
                dependencies=("first",),
                callable=lambda _: {"ready": True},
            ),
        )
    )
    report = GateExecutor(
        registry,
        project_root=tmp_path,
        output_root=tmp_path / "out",
        source_commit="a" * 40,
        environment=dict(os.environ),
    ).execute()
    receipts = {item["gate_id"]: item for item in report["receipts"]}
    assert report["ready"] is False
    assert receipts["first"]["state"] == GateState.FAILED.value
    assert receipts["second"]["state"] == GateState.BLOCKED.value


def test_failed_gate_preserves_primary_error_when_success_artifact_is_absent(
    tmp_path: Path,
) -> None:
    registry = GateRegistry(
        (
            GateSpec(
                gate_id="failure",
                callable=lambda _: {
                    "ready": False,
                    "error": "primary gate failure",
                },
                artifacts=("success.json",),
            ),
        )
    )
    report = GateExecutor(
        registry,
        project_root=tmp_path,
        output_root=tmp_path / "out",
        source_commit="a" * 40,
        environment=dict(os.environ),
    ).execute()
    receipt = report["receipts"][0]
    assert receipt["state"] == GateState.FAILED.value
    assert receipt["reason"] == "primary gate failure"
    assert report["details"]["failure"]["error"] == "primary gate failure"


def test_release_environment_allowlist_is_case_insensitive_on_windows() -> None:
    policy = ReleasePolicy()
    assert policy.environment_allowed("SystemRoot") is True
    assert policy.environment_allowed("SYSTEMROOT") is True
    assert policy.environment_allowed("zyra_release_ci") is True
    assert policy.environment_allowed("UNRELATED_SECRET") is False


def test_gate_registry_rejects_cycles() -> None:
    registry = GateRegistry(
        (
            GateSpec(
                gate_id="a",
                dependencies=("b",),
                callable=lambda _: {"ready": True},
            ),
            GateSpec(
                gate_id="b",
                dependencies=("a",),
                callable=lambda _: {"ready": True},
            ),
        )
    )
    with pytest.raises(GateFailure) as captured:
        registry.validate()
    assert captured.value.code == "ci_gate_dependency_cycle"


def test_release_admission_cannot_disable_mandatory_gate() -> None:
    policy = ReleasePolicy(mandatory_gates=frozenset({"a", "b"}))
    report = {
        "schema": "zyra.release-ci-report/v1",
        "source_commit": "a" * 40,
        "ready": True,
        "receipts": [
            {
                "gate_id": "a",
                "state": "passed",
                "required": True,
                "artifacts": [],
            }
        ],
    }
    with pytest.raises(GateFailure) as captured:
        ReleaseAdmission(policy=policy).verify(
            report,
            expected_commit="a" * 40,
        )
    assert captured.value.code == "release_admission_failed"
    assert captured.value.details["missing"] == ["b"]


def test_evidence_index_verifies_digest_commit_dependencies_and_cycles(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.json").write_text("{}", encoding="utf-8")
    (tmp_path / "b.json").write_text("{}", encoding="utf-8")
    index = EvidenceIndex(tmp_path)
    index.add(
        EvidenceReference(
            evidence_id="a-evidence",
            kind="a",
            path="a.json",
            sha256=sha256_file(tmp_path / "a.json"),
            source_commit="a" * 40,
        )
    )
    index.add(
        EvidenceReference(
            evidence_id="b-evidence",
            kind="b",
            path="b.json",
            sha256=sha256_file(tmp_path / "b.json"),
            source_commit="b" * 40,
            dependencies=("a-evidence",),
        )
    )
    receipt = index.validate(
        release_commit="b" * 40,
        allowed_historical_commits=("a" * 40,),
    )
    assert receipt["ready"] is True
    assert receipt["order"] == ["a-evidence", "b-evidence"]


def test_clean_install_receipt_admission_detects_missing_lifecycle() -> None:
    receipt = {
        "schema": "zyra.clean-install-receipt/v1",
        "ready": True,
        "source_commit": "a" * 40,
        "workspace_isolated": True,
        "parent_source_repositories_present": False,
        "product_lifecycle_exercised": False,
        "commands": [],
        "receipts": {
            "install": {"state": "committed"},
            "uninstall": {"state": "uninstalled"},
        },
    }
    with pytest.raises(GateFailure) as captured:
        CleanInstallReceiptVerifier().verify(
            receipt,
            expected_commit="a" * 40,
        )
    assert "product_lifecycle_not_exercised" in captured.value.details["failures"]


def test_release_manifest_requires_all_delivery_artifacts() -> None:
    value = {
        "schema": "zyra.release-manifest/v1",
        "release_id": "release-1",
        "source_commit": "a" * 40,
        "payload": {"root_digest": "b" * 64},
        "locks": {
            "python": {"digest": "c" * 64},
            "javascript": {"digest": "d" * 64},
        },
        "artifacts": {
            name: {"sha256": "e" * 64}
            for name in (
                "checksums",
                "sbom",
                "notice",
                "runtime_inventory",
                "configuration",
                "benchmark",
                "python_wheel",
            )
        },
        "platforms": [
            {"platform": "windows"},
            {"platform": "linux"},
        ],
    }
    manifest = ReleaseManifest(value)
    assert manifest.release_id == "release-1"
    del value["artifacts"]["sbom"]
    with pytest.raises(IntegrityViolation) as captured:
        ReleaseManifest(value)
    assert captured.value.code == "release_manifest_artifact_missing"


def test_deployment_resolves_commit_from_installed_release_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zyra_orchestration.deployment.orchestrator import DeploymentOrchestrator

    release = tmp_path / "release"
    release.mkdir()
    (release / "manifest.json").write_text(
        json.dumps({"source_commit": "a" * 40}),
        encoding="utf-8",
    )

    def unavailable(*_: object, **__: object) -> object:
        raise OSError("git unavailable in installed release")

    monkeypatch.setattr(
        "zyra_orchestration.deployment.orchestrator.subprocess.run",
        unavailable,
    )
    orchestrator = DeploymentOrchestrator(
        tmp_path,
        state_root=tmp_path / "state",
    )
    assert orchestrator.target_commit() == "a" * 40
