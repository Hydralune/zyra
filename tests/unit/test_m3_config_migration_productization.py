from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from zyra_runtime.artifacts import LocalArtifactStore
from zyra_runtime.productization import (
    ArtifactManifestMigrationAdapter,
    BootstrapError,
    ConfigurationError,
    CredentialError,
    CredentialRequirement,
    EnvironmentSecretProvisioner,
    LifecyclePhase,
    LifecycleRecorder,
    MigrationAdapterError,
    MigrationJournal,
    MigrationJournalError,
    MigrationRuntime,
    MigrationRuntimeError,
    ProcessIdentity,
    ProductBootstrapRuntime,
    SqliteOwnerMigrationAdapter,
    load_runtime_configuration,
)


CONFIGURATION_DIGEST = "sha256:" + ("7" * 64)


def _sqlite_version(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()


def _seed_legacy_sqlite(path: Path, payload: str = "preserve-me") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE durable_payload(id INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO durable_payload(id, payload) VALUES (1, ?)",
            (payload,),
        )
        connection.execute("PRAGMA user_version=0")
        connection.commit()
    finally:
        connection.close()


def _read_payload(path: Path) -> str:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT payload FROM durable_payload WHERE id=1"
        ).fetchone()
        assert row is not None
        return str(row[0])
    finally:
        connection.close()


def _sqlite_adapter(path: Path) -> SqliteOwnerMigrationAdapter:
    return SqliteOwnerMigrationAdapter(
        adapter_id="durable-owner",
        owner="tests.DurableOwner",
        path=path,
        target_version=1,
        required_tables=("durable_payload",),
        allow_missing=False,
    )


def _migration_runtime(
    root: Path,
    *,
    crash_hook=None,
) -> tuple[MigrationJournal, MigrationRuntime]:
    journal = MigrationJournal(root / "migration-journal.sqlite3")
    runtime = MigrationRuntime(
        journal,
        (_sqlite_adapter(root / "owner.sqlite3"),),
        backup_root=root / "backups",
        process_generation="test-generation",
        configuration_digest=CONFIGURATION_DIGEST,
        target_version=1,
        lease_seconds=10,
        crash_hook=crash_hook,
    )
    return journal, runtime


def test_configuration_precedence_derivation_and_public_projection(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "zyra.toml"
    config_path.write_text(
        """
schema_version = 1
profile = "competition"

[state]
root = "file-state"

[api]
port = 8123
""".strip(),
        encoding="utf-8",
    )

    configuration = load_runtime_configuration(
        tmp_path,
        config_path=config_path,
        environ={
            "ZYRA_STATE_ROOT": "environment-state",
            "ZYRA_API_PORT": "8234",
        },
        explicit_overrides={
            "state.root": "explicit-state",
            "api.port": 8345,
        },
    )

    assert configuration.path("state.root") == tmp_path / "explicit-state"
    assert configuration.path("state.database") == (
        tmp_path / "explicit-state" / "zyra.sqlite3"
    )
    assert configuration.path("state.provider") == (
        tmp_path
        / "explicit-state"
        / "artifacts"
        / ".provider-control-plane"
        / "provider.sqlite3"
    )
    assert configuration.require("api.port") == 8345
    assert configuration.origin("api.port").source == "explicit"
    projection = configuration.public_projection()
    assert projection["schema"] == "zyra.runtime-config/v1"
    assert projection["digest"] == configuration.digest
    assert projection["values"] if "values" in projection else True
    assert "credential material" not in json.dumps(projection)

    environment_only = load_runtime_configuration(
        tmp_path,
        environ={"ZYRA_STATE_ROOT": "environment-only"},
    )
    assert environment_only.path("state.database") == (
        tmp_path / "environment-only" / "zyra.sqlite3"
    )


def test_equal_precedence_leaf_paths_override_environment_derivation(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    graph_path = tmp_path / "explicit" / "graph.sqlite3"
    artifact_root = tmp_path / "explicit" / "artifacts"
    permission_path = tmp_path / "explicit" / "permission.json"
    configuration = load_runtime_configuration(
        tmp_path,
        environ={
            "ZYRA_STATE_ROOT": str(state_root),
            "ZYRA_GRAPH_STATE_STORE": str(graph_path),
            "ZYRA_ARTIFACT_ROOT": str(artifact_root),
            "ZYRA_PERMISSION_STATE": str(permission_path),
        },
    )

    assert configuration.path("state.database") == (
        state_root / "zyra.sqlite3"
    )
    assert configuration.path("state.graph") == graph_path
    assert configuration.path("state.artifacts") == artifact_root
    assert configuration.path("state.permission") == permission_path
    assert configuration.path("state.mcp") == (
        artifact_root / ".mcp" / "state.json"
    )
    assert configuration.origin("state.graph").source == "environment"
    assert configuration.origin("state.permission").source == "environment"


def test_legacy_database_path_derives_an_isolated_state_root(
    tmp_path: Path,
) -> None:
    database = tmp_path / "isolated" / "api.sqlite3"
    configuration = load_runtime_configuration(
        tmp_path,
        environ={"ZYRA_SQLITE_PATH": str(database)},
    )

    assert configuration.path("state.root") == database.parent
    assert configuration.path("state.database") == database
    assert configuration.path("state.worker_pool") == (
        database.parent / "zyra.worker-pool.sqlite3"
    )
    assert configuration.path("state.graph") == (
        database.parent / "zyra.graph-state.sqlite3"
    )
    assert configuration.path("state.migration_journal") == (
        database.parent / ".productization" / "migrations.sqlite3"
    )
    assert configuration.origin("state.root").source == "derived:environment"


@pytest.mark.parametrize(
    ("document", "code"),
    [
        (
            'schema_version = 2\nprofile = "default"\n',
            "config_schema_version_future",
        ),
        (
            'schema_version = 1\nunknown = true\n',
            "config_unknown_key",
        ),
        (
            'schema_version = 1\n[state]\nroot = "../escape"\n',
            "config_relative_path_escape",
        ),
        (
            'schema_version = 1\n[state]\nroot = "sk-abcdefghijklmnop"\n',
            "config_secret_material_forbidden",
        ),
    ],
)
def test_configuration_rejects_unsafe_or_unknown_documents(
    tmp_path: Path,
    document: str,
    code: str,
) -> None:
    config_path = tmp_path / "unsafe.toml"
    config_path.write_text(document, encoding="utf-8")
    with pytest.raises(ConfigurationError) as raised:
        load_runtime_configuration(tmp_path, config_path=config_path, environ={})
    assert raised.value.code == code
    assert "sk-abcdefghijklmnop" not in json.dumps(raised.value.to_dict())


def test_credentials_are_leased_rotated_and_never_serialized() -> None:
    requirements = (
        CredentialRequirement(
            credential_id="provider",
            environment_name="ZYRA_TEST_PROVIDER_SECRET",
            required=True,
            feature="provider_dispatch",
        ),
    )
    secret = "provider-secret-material-123"
    runtime = EnvironmentSecretProvisioner(
        requirements,
        environ={"ZYRA_TEST_PROVIDER_SECRET": secret},
    )
    presence = runtime.provision("provider")
    lease = runtime.issue_lease("provider", ttl_seconds=30)

    assert presence.available is True
    assert secret not in json.dumps(presence.to_dict())
    assert runtime.use(lease, lambda value: bytes(value).decode("utf-8")) == secret

    rotated = runtime.rotate("provider", "rotated-secret-material-456")
    assert rotated.fingerprint != presence.fingerprint
    with pytest.raises(CredentialError) as raised:
        runtime.use(lease, lambda value: bytes(value))
    assert raised.value.code == "credential_lease_unknown"
    assert runtime.revoke("provider") is True
    assert runtime.status()[0]["revoked"] is True
    assert "rotated-secret-material-456" not in json.dumps(runtime.status())
    runtime.close()


def test_lifecycle_redacts_logs_and_closes_dependents_first(
    tmp_path: Path,
) -> None:
    closed: list[str] = []
    process = ProcessIdentity.create(configuration_digest=CONFIGURATION_DIGEST)
    recorder = LifecycleRecorder(
        process,
        component="tests.productization",
        log_path=tmp_path / "lifecycle.jsonl",
    )
    recorder.register_resource(
        "database",
        "database",
        close=lambda value: closed.append(value),
    )
    recorder.register_resource(
        "api",
        "api",
        close=lambda value: closed.append(value),
        dependencies=("database",),
    )
    recorder.transition(LifecyclePhase.CONFIGURING)
    recorder.emit(
        "configuration.observed",
        attributes={
            "api_token": "sk-sensitive-material-123456",
            "safe": "visible",
        },
    )
    recorder.transition(LifecyclePhase.STARTING)
    recorder.transition(LifecyclePhase.READY)

    assert recorder.readiness()["ready"] is True
    report = recorder.shutdown(timeout_seconds=5)
    assert report.clean is True
    assert closed == ["api", "database"]
    payload = (tmp_path / "lifecycle.jsonl").read_text(encoding="utf-8")
    assert "sk-sensitive-material-123456" not in payload
    assert "[REDACTED]" in payload


def test_sqlite_migration_commits_preserves_data_and_is_restart_safe(
    tmp_path: Path,
) -> None:
    owner = tmp_path / "owner.sqlite3"
    _seed_legacy_sqlite(owner)
    journal, runtime = _migration_runtime(tmp_path)
    try:
        plan = runtime.plan()
        assert plan.migration_required is True
        receipt = runtime.execute(plan)
        assert receipt.migrated is True
        assert receipt.transaction.state.value == "committed"
        assert runtime.readiness()["ready"] is True
        assert _sqlite_version(owner) == 1
        assert _read_payload(owner) == "preserve-me"
    finally:
        journal.close()

    restart_journal, restart = _migration_runtime(tmp_path)
    try:
        assert restart.recover_incomplete() is None
        restart_receipt = restart.execute(restart.plan())
        assert restart_receipt.migrated is False
        assert restart.readiness()["ready"] is True
        assert _read_payload(owner) == "preserve-me"
    finally:
        restart_journal.close()


def test_migration_lease_rejects_live_owner_and_recovers_dead_owner(
    tmp_path: Path,
) -> None:
    journal = MigrationJournal(tmp_path / "lease-journal.sqlite3")
    try:
        live = journal.acquire_lease(
            f"pid:{os.getpid()}:live-generation",
            ttl_seconds=60,
        )
        with pytest.raises(MigrationJournalError) as raised:
            journal.acquire_lease(
                "pid:999999999:new-generation",
                ttl_seconds=60,
            )
        assert getattr(raised.value, "code", "") == "migration_lease_busy"
        assert journal.release_lease(live) is True

        journal.acquire_lease(
            "pid:999999999:dead-generation",
            ttl_seconds=60,
        )
        replacement = journal.acquire_lease(
            f"pid:{os.getpid()}:replacement-generation",
            ttl_seconds=60,
        )
        assert replacement.generation >= 2
        assert journal.release_lease(replacement) is True
    finally:
        journal.close()


def test_interrupted_migration_is_rolled_back_before_restart(
    tmp_path: Path,
) -> None:
    owner = tmp_path / "owner.sqlite3"
    _seed_legacy_sqlite(owner, "restart-custody")

    def interrupt(phase: str, _transaction: str, _adapter: str) -> None:
        if phase == "after_apply":
            raise SystemExit("simulated process termination")

    journal, runtime = _migration_runtime(tmp_path, crash_hook=interrupt)
    with pytest.raises(SystemExit):
        runtime.execute(runtime.plan())
    active = journal.active_transaction()
    assert active is not None
    assert active.state.value == "applying"
    assert _sqlite_version(owner) == 1
    assert len(journal.backups(active.transaction_id)) == 1
    journal.close()

    restart_journal, restart = _migration_runtime(tmp_path)
    try:
        recovery = restart.recover_incomplete()
        assert recovery is not None
        assert recovery.clean is True
        assert recovery.final_state == "rolled_back"
        assert _sqlite_version(owner) == 0
        assert _read_payload(owner) == "restart-custody"
        committed = restart.execute(restart.plan())
        assert committed.migrated is True
        assert _sqlite_version(owner) == 1
    finally:
        restart_journal.close()


def test_corrupt_backup_blocks_restart_recovery(
    tmp_path: Path,
) -> None:
    owner = tmp_path / "owner.sqlite3"
    _seed_legacy_sqlite(owner, "must-not-disappear")

    def interrupt(phase: str, _transaction: str, _adapter: str) -> None:
        if phase == "after_apply":
            raise KeyboardInterrupt("simulated abrupt termination")

    journal, runtime = _migration_runtime(tmp_path, crash_hook=interrupt)
    with pytest.raises(KeyboardInterrupt):
        runtime.execute(runtime.plan())
    active = journal.active_transaction()
    assert active is not None
    backup = journal.backups(active.transaction_id)[0]
    Path(backup.backup_path).write_bytes(b"corrupted-backup")
    journal.close()

    restart_journal, restart = _migration_runtime(tmp_path)
    try:
        with pytest.raises(MigrationRuntimeError) as raised:
            restart.recover_incomplete()
        assert raised.value.code == "migration_recovery_failed"
        assert restart_journal.active_transaction() is not None
        assert _read_payload(owner) == "must-not-disappear"
    finally:
        restart_journal.close()


def test_product_bootstrap_migrates_all_default_owner_shapes(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    configuration = load_runtime_configuration(
        tmp_path,
        environ={},
        explicit_overrides={"state.root": str(state_root)},
    )
    _seed_legacy_sqlite(configuration.path("state.database"), "session")
    _seed_legacy_sqlite(configuration.path("state.graph"), "graph")
    artifact_root = configuration.path("state.artifacts")
    artifact_root.mkdir(parents=True)
    (artifact_root / "result.txt").write_text("artifact-payload", encoding="utf-8")
    permission_path = configuration.path("state.permission")
    permission_path.parent.mkdir(parents=True, exist_ok=True)
    permission_path.write_text(
        json.dumps({"rules": [], "requests": {}}),
        encoding="utf-8",
    )

    runtime = ProductBootstrapRuntime(
        configuration,
        environ={},
        owner_probe=lambda: {"ready": True, "blockers": []},
        process_id="test-bootstrap",
    )
    receipt = runtime.start()
    assert receipt.migration is not None
    assert receipt.migration.migrated is True
    assert runtime.readiness()["ready"] is True
    assert _sqlite_version(configuration.path("state.database")) == 1
    assert _sqlite_version(configuration.path("state.graph")) == 1
    assert (artifact_root / "result.txt").read_text(encoding="utf-8") == (
        "artifact-payload"
    )
    manifest = json.loads(
        (artifact_root / ".zyra-artifact-store.json").read_text(encoding="utf-8")
    )
    assert manifest["schema"] == "zyra.artifact-store-manifest/v1"
    permission = json.loads(permission_path.read_text(encoding="utf-8"))
    assert permission["schema"] == "zyra.permission-state"
    assert runtime.shutdown().clean is True
    LocalArtifactStore(artifact_root).write_text(
        run_id="run_restart",
        task_id="task_restart",
        content="artifact committed after bootstrap",
        title="restart evidence",
    )

    restarted = ProductBootstrapRuntime(
        configuration,
        environ={},
        owner_probe=lambda: {"ready": True, "blockers": []},
        process_id="test-bootstrap-restart",
    )
    restart_receipt = restarted.start()
    assert restart_receipt.recovery is None
    assert restart_receipt.migration is not None
    assert restart_receipt.migration.migrated is False
    assert restarted.readiness()["ready"] is True
    assert restarted.shutdown().clean is True


def test_artifact_manifest_allows_append_only_owner_growth(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    original = artifact_root / "legacy.txt"
    original.write_text("preserved", encoding="utf-8")
    adapter = ArtifactManifestMigrationAdapter(artifact_root)

    probe = adapter.probe()
    mutation = adapter.apply(probe)
    assert adapter.verify(mutation)["verified"] is True

    (artifact_root / "run-new" / "task-new").mkdir(parents=True)
    (artifact_root / "run-new" / "task-new" / "artifact.txt").write_text(
        "new immutable artifact",
        encoding="utf-8",
    )
    restart_probe = adapter.probe()

    assert restart_probe.migration_required is False
    assert restart_probe.details["appended_artifact_count"] == 1


@pytest.mark.parametrize("operation", ["change", "delete"])
def test_artifact_manifest_rejects_drift_of_recorded_bytes(
    tmp_path: Path,
    operation: str,
) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    original = artifact_root / "legacy.txt"
    original.write_text("preserved", encoding="utf-8")
    adapter = ArtifactManifestMigrationAdapter(artifact_root)
    adapter.apply(adapter.probe())

    if operation == "change":
        original.write_text("tampered", encoding="utf-8")
    else:
        original.unlink()

    with pytest.raises(MigrationAdapterError) as raised:
        adapter.probe()

    assert raised.value.code == "migration_artifact_inventory_drift"


def test_disabling_required_migration_fails_closed_without_owner_fallback(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    configuration = load_runtime_configuration(
        tmp_path,
        environ={},
        explicit_overrides={
            "state.root": str(state_root),
            "runtime.migration_enabled": False,
        },
    )
    session_store = configuration.path("state.database")
    _seed_legacy_sqlite(session_store, "migration-must-run")
    probes: list[str] = []
    runtime = ProductBootstrapRuntime(
        configuration,
        environ={},
        owner_probe=lambda: (
            probes.append("called") or {"ready": True, "blockers": []}
        ),
        process_id="test-migration-disabled",
    )

    with pytest.raises(BootstrapError) as raised:
        runtime.start()

    assert raised.value.code == "bootstrap_migration_disabled"
    assert probes == []
    assert _sqlite_version(session_store) == 0
    assert _read_payload(session_store) == "migration-must-run"
    readiness = runtime.readiness()
    assert readiness["ready"] is False
    assert "lifecycle" in readiness["blockers"]
    assert readiness["source_store_fallback"] is False
    assert runtime.shutdown().clean is True


def test_missing_required_credential_blocks_bootstrap_before_owner_start(
    tmp_path: Path,
) -> None:
    configuration = load_runtime_configuration(
        tmp_path,
        environ={},
        explicit_overrides={
            "state.root": str(tmp_path / "state"),
            "features.provider_dispatch": True,
            "credentials.required.provider": "ZYRA_TEST_MISSING_PROVIDER_KEY",
        },
    )
    probes: list[str] = []
    runtime = ProductBootstrapRuntime(
        configuration,
        environ={},
        owner_probe=lambda: (
            probes.append("called") or {"ready": True, "blockers": []}
        ),
        process_id="test-missing-required-credential",
    )

    with pytest.raises(CredentialError) as raised:
        runtime.start()

    assert raised.value.code == "required_credentials_missing"
    assert probes == []
    readiness = runtime.readiness()
    assert readiness["ready"] is False
    assert "credential:provider" in readiness["blockers"]
    assert readiness["source_store_fallback"] is False
    assert runtime.shutdown().clean is True
