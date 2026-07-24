from __future__ import annotations

import hashlib
import importlib
import json
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping

import pytest


ROOT = Path(__file__).resolve().parents[2]
for package in (
    ROOT / "packages" / "core",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "workspace",
):
    if str(package) not in sys.path:
        sys.path.insert(0, str(package))

from apps.api.zyra_api.artifact_api import ArtifactCatalogService  # noqa: E402
from apps.api.zyra_api.diff_review_api import (  # noqa: E402
    DIFF_MANIFEST_SCHEMA,
    DIFF_PAGE_SCHEMA,
    PATCH_RECEIPT_SCHEMA,
    DiffReviewApiService,
    DiffReviewApiError,
    DiffReviewRegistry,
)
from zyra_core import ArtifactKind  # noqa: E402
from zyra_runtime.artifacts import LocalArtifactStore  # noqa: E402
from zyra_workspace import (  # noqa: E402
    WorkspaceEditPort,
    WorkspaceError,
    WorkspaceErrorCode,
    WorkspaceKind,
    WorkspaceManagerConfig,
    WorkspaceManagerRuntime,
)


TASK_ID = "task-diff-review"
RUN_ID = "run-diff-review"
SESSION_ID = "session-diff-review"


class PermissionOwner:
    def __init__(self, effect: str = "allow") -> None:
        self.effect = effect
        self.claims: list[dict[str, Any]] = []
        self.enforcements: list[dict[str, Any]] = []

    def permission_claim(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self.claims.append(dict(payload))
        permit_id = str(payload.get("permit_id") or "")
        if permit_id == "permit-exact":
            return {
                "canonical_owner": "typescript.PermissionCoordinator",
                "claimed": True,
                "permit_id": permit_id,
                "decision": {
                    "effect": "allow",
                    "decision_id": "decision-claimed",
                    "reason_code": "permission.permit_claimed",
                    "policy_revision": 3,
                    "mode_revision": 2,
                },
            }
        return {
            "canonical_owner": "typescript.PermissionCoordinator",
            "claimed": False,
        }

    def permission_enforce(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self.enforcements.append(dict(payload))
        return {
            "canonical_owner": "typescript.PermissionCoordinator",
            "allowed": self.effect == "allow",
            "request_fingerprint": "permission-fingerprint-diff-review",
            "decision": {
                "effect": self.effect,
                "decision_id": f"decision-{self.effect}",
                "request_id": "request-diff-review" if self.effect == "ask" else "",
                "reason_code": f"permission.{self.effect}",
                "policy_revision": 3,
                "mode_revision": 2,
            },
        }


@pytest.fixture()
def integration(tmp_path: Path):
    manager = WorkspaceManagerRuntime(
        WorkspaceManagerConfig(
            state_root=tmp_path / "workspace-state",
            data_root=tmp_path / "workspace-data",
            lease_ttl_seconds=300,
        )
    )
    created = manager.create_for_task(
        run_id=RUN_ID,
        task_id=TASK_ID,
        session_id=SESSION_ID,
        worker_id="setup",
        idempotency_key="create-diff-review-workspace",
    )
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    setup = WorkspaceEditPort(
        manager,
        created.access,
        worker_id="setup",
        run_id=RUN_ID,
        task_id=TASK_ID,
        node_id="setup",
        artifact_store=artifacts,
    )
    setup.write_text(
        "src/example.txt",
        "alpha\nbeta\ngamma\n",
        idempotency_key="seed-example",
    )
    patch = (
        "diff --git a/src/example.txt b/src/example.txt\n"
        "index 83db48f..f735c2d 100644\n"
        "--- a/src/example.txt\n"
        "+++ b/src/example.txt\n"
        "@@ -1,3 +1,4 @@\n"
        " alpha\n"
        "-beta\n"
        "+beta reviewed\n"
        " gamma\n"
        "+delta\n"
    )
    artifact = artifacts.write_text(
        run_id=RUN_ID,
        task_id=TASK_ID,
        content=patch,
        title="review.patch",
        kind=ArtifactKind.TEXT,
        extension=".patch",
        producer_node_id="node-patch",
        metadata={
            "media_type": "text/x-diff",
            "content_family": "text",
            "encoding": "utf-8",
            "security_label": "internal",
            "trust_disposition": "trusted",
            "download_policy": "allow",
        },
    )
    permission = PermissionOwner("allow")
    service = DiffReviewApiService(
        artifact_service=ArtifactCatalogService(store=artifacts),
        workspace_manager=manager,
        permission_port=permission,
        artifact_store=artifacts,
    )
    return manager, artifacts, artifact, permission, service


def _manifest(service: DiffReviewApiService, artifact) -> dict[str, Any]:
    return dict(
        service.manifest(
            task_id=TASK_ID,
            run_id=RUN_ID,
            artifact=artifact,
            revision=str(artifact.metadata["revision"]),
        ).body
    )


def _apply_payload(manifest: Mapping[str, Any]) -> dict[str, Any]:
    source = dict(manifest["source"])
    file = dict(manifest["files"][0])
    return {
        "schema": "zyra.patch-review-apply.v1",
        "task_id": TASK_ID,
        "run_id": RUN_ID,
        "diff_id": manifest["diff_id"],
        "artifact_id": source["artifact_id"],
        "artifact_revision": source["artifact_revision"],
        "workspace_id": source["workspace_id"],
        "idempotency_key": "diff-apply-idempotency-001",
        "causation_id": "diff-apply-cause-001",
        "actor_id": "reviewer",
        "session_id": SESSION_ID,
        "session_revision": 0,
        "worker_request_id": "diff-review-worker-request",
        "tool_call_id": "diff-review-tool-call",
        "expected_owner_epoch": source["owner_epoch"],
        "expected_binding_revision": source["binding_revision"],
        "expected_lease_id": source["lease_id"],
        "selected_file_ids": [file["file_id"]],
        "preconditions": [
            {
                "file_id": file["file_id"],
                "path": file["path"],
                "previous_path": file.get("previous_path"),
                "kind": file["kind"],
                "base_sha256": file["old_sha256"],
                "current_sha256": file["current_sha256"],
                "proposed_sha256": file["new_sha256"],
                "base_mtime_ns": file["old_mtime_ns"],
                "current_mtime_ns": file["current_mtime_ns"],
                "base_mode": file["old_mode"],
                "current_mode": file["current_mode"],
                "encoding": file["encoding"],
                "line_ending": file["line_ending"],
                "binary": False,
            }
        ],
        "sealed": False,
        "review_revision": 0,
    }


def _review_payload(
    manifest: Mapping[str, Any],
    *,
    action: str = "comment",
    sealed: bool = False,
) -> dict[str, Any]:
    file = dict(manifest["files"][0])
    page = file["_test_page"]
    hunk = dict(page["hunks"][0])
    line = next(item for item in hunk["lines"] if item["new_line"] is not None)
    return {
        "schema": "zyra.diff-review-comment-request.v1",
        "artifact_revision": manifest["source"]["artifact_revision"],
        "diff_id": manifest["diff_id"],
        "action": action,
        "selection": {
            "file_id": file["file_id"],
            "hunk_id": hunk["hunk_id"],
            "side": "new",
            "start_line": line["new_line"],
            "end_line": line["new_line"],
            "anchor_line_ids": [line["line_id"]],
        },
        "body": "Please preserve this reviewed behavior.",
        "expected_review_revision": 0,
        "actor_id": "reviewer",
        "causation_id": f"review-cause-{action}-{sealed}",
        "session_id": SESSION_ID,
        "session_revision": 0,
        "worker_request_id": "diff-review-worker-request",
        "tool_call_id": f"diff-review-tool-call-{action}-{sealed}",
        "sealed": sealed,
    }


def test_manifest_pages_and_content_are_bound_and_path_redacted(integration) -> None:
    manager, _artifacts, artifact, _permission, service = integration
    manifest = _manifest(service, artifact)
    assert manifest["schema"] == DIFF_MANIFEST_SCHEMA
    assert manifest["physical_path_disclosed"] is False
    assert manifest["read_only"] is True
    assert manifest["totals"] == {
        "files": 1,
        "hunks": 1,
        "lines": 5,
        "additions": 2,
        "deletions": 1,
        "binary_files": 0,
        "renamed_files": 0,
        "oversized_files": 0,
        "patch_bytes": len(
            (
                "diff --git a/src/example.txt b/src/example.txt\n"
                "index 83db48f..f735c2d 100644\n"
                "--- a/src/example.txt\n"
                "+++ b/src/example.txt\n"
                "@@ -1,3 +1,4 @@\n alpha\n-beta\n+beta reviewed\n gamma\n+delta\n"
            ).encode()
        ),
    }
    file = manifest["files"][0]
    page = service.page(
        task_id=TASK_ID,
        run_id=RUN_ID,
        artifact=artifact,
        file_id=file["file_id"],
        revision=manifest["source"]["artifact_revision"],
        page_index=0,
        maximum_bytes=8 * 1024 * 1024,
        maximum_lines=100_000,
    ).body
    assert page["schema"] == DIFF_PAGE_SCHEMA
    assert page["complete"] is True
    assert page["hunk_start"] == 0
    assert page["hunk_end"] == 1
    assert page["content_digest"].startswith("sha256:")
    assert page["physical_path_disclosed"] is False
    base = service.file_content(
        task_id=TASK_ID,
        run_id=RUN_ID,
        artifact=artifact,
        file_id=file["file_id"],
        revision=manifest["source"]["artifact_revision"],
        version="base",
    ).body
    assert base["text"] == "alpha\nbeta\ngamma\n"
    assert base["sha256"] == f"sha256:{hashlib.sha256(base['text'].encode()).hexdigest()}"
    serialized = str(manifest) + str(page) + str(base)
    assert str(manager.config.data_root) not in serialized
    assert str(manager.config.state_root) not in serialized


def test_manifest_uses_real_git_boundary_for_dirty_risk(integration) -> None:
    manager, _artifacts, artifact, _permission, service = integration
    access = manager.acquire_for_worker(
        task_id=TASK_ID,
        session_id="",
        worker_id="git-risk-observer",
    )
    binding = manager.store.require_binding(access.workspace_id)
    task_root = manager.backend.mount_root(binding, WorkspaceKind.TASK)
    initialized = subprocess.run(
        ["git", "init", "--quiet"],
        cwd=task_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert initialized.returncode == 0, initialized.stderr
    manifest = _manifest(service, artifact)
    risk = manifest["files"][0]["risk"]
    assert risk["dirty"] is True
    assert risk["untracked_paths"] >= 1
    assert risk["repository_root"] == "."
    assert "git_boundary.dirty_worktree" in risk["reason_codes"]


def test_multifile_manifest_covers_add_delete_rename_and_binary(integration) -> None:
    manager, artifacts, _artifact, _permission, service = integration
    access = manager.acquire_for_worker(
        task_id=TASK_ID,
        session_id="",
        worker_id="multifile-setup",
    )
    setup = WorkspaceEditPort(
        manager,
        access,
        worker_id="multifile-setup",
        run_id=RUN_ID,
        task_id=TASK_ID,
    )
    setup.write_text("src/old.txt", "old\n", idempotency_key="seed-old")
    setup.write_text("src/delete.txt", "delete\n", idempotency_key="seed-delete")
    setup.write_bytes(
        "src/logo.bin",
        b"\x00\x01\x02\xff",
        idempotency_key="seed-binary",
    )
    patch = (
        "diff --git a/src/old.txt b/src/renamed.txt\n"
        "similarity index 50%\n"
        "rename from src/old.txt\n"
        "rename to src/renamed.txt\n"
        "--- a/src/old.txt\n"
        "+++ b/src/renamed.txt\n"
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "+renamed\n"
        "diff --git a/src/added.txt b/src/added.txt\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/src/added.txt\n"
        "@@ -0,0 +1,2 @@\n"
        "+added\n"
        "+line\n"
        "diff --git a/src/delete.txt b/src/delete.txt\n"
        "deleted file mode 100644\n"
        "--- a/src/delete.txt\n"
        "+++ /dev/null\n"
        "@@ -1,1 +0,0 @@\n"
        "-delete\n"
        "diff --git a/src/logo.bin b/src/logo.bin\n"
        "Binary files a/src/logo.bin and b/src/logo.bin differ\n"
    )
    artifact = artifacts.write_text(
        run_id=RUN_ID,
        task_id=TASK_ID,
        content=patch,
        title="multifile.patch",
        kind=ArtifactKind.TEXT,
        extension=".patch",
        producer_node_id="node-multifile",
        metadata={
            "media_type": "text/x-diff",
            "content_family": "text",
            "encoding": "utf-8",
            "security_label": "internal",
            "trust_disposition": "trusted",
            "download_policy": "allow",
        },
    )
    manifest = _manifest(service, artifact)
    files = {item["path"]: item for item in manifest["files"]}
    assert files["src/renamed.txt"]["kind"] == "renamed"
    assert files["src/renamed.txt"]["previous_path"] == "src/old.txt"
    assert files["src/added.txt"]["kind"] == "added"
    assert files["src/delete.txt"]["kind"] == "deleted"
    assert files["src/logo.bin"]["kind"] == "binary"
    assert files["src/logo.bin"]["binary"] is True
    assert manifest["totals"]["files"] == 4
    assert manifest["totals"]["binary_files"] == 1
    assert manifest["totals"]["renamed_files"] == 1


def test_crlf_patch_apply_preserves_line_endings(integration) -> None:
    manager, artifacts, _artifact, _permission, service = integration
    access = manager.acquire_for_worker(
        task_id=TASK_ID,
        session_id="",
        worker_id="crlf-setup",
    )
    setup = WorkspaceEditPort(
        manager,
        access,
        worker_id="crlf-setup",
        run_id=RUN_ID,
        task_id=TASK_ID,
    )
    setup.write_text(
        "src/crlf.txt",
        "first\r\nsecond\r\n",
        idempotency_key="seed-crlf",
    )
    artifact = artifacts.write_text(
        run_id=RUN_ID,
        task_id=TASK_ID,
        content=(
            "diff --git a/src/crlf.txt b/src/crlf.txt\n"
            "--- a/src/crlf.txt\n"
            "+++ b/src/crlf.txt\n"
            "@@ -1,2 +1,2 @@\n"
            " first\n"
            "-second\n"
            "+reviewed\n"
        ),
        title="crlf.patch",
        kind=ArtifactKind.TEXT,
        extension=".patch",
        producer_node_id="node-crlf",
        metadata={
            "media_type": "text/x-diff",
            "content_family": "text",
            "encoding": "utf-8",
            "security_label": "internal",
            "trust_disposition": "trusted",
            "download_policy": "allow",
        },
    )
    manifest = _manifest(service, artifact)
    assert manifest["files"][0]["line_ending"] == "crlf"
    payload = {
        **_apply_payload(manifest),
        "idempotency_key": "diff-apply-crlf",
        "causation_id": "diff-apply-crlf-cause",
    }
    receipt = service.apply(
        task_id=TASK_ID,
        run_id=RUN_ID,
        artifact=artifact,
        payload=payload,
    )
    assert receipt.body["phase"] == "committed"
    verify_access = manager.acquire_for_worker(
        task_id=TASK_ID,
        session_id="",
        worker_id="crlf-verification",
    )
    verify = WorkspaceEditPort(
        manager,
        verify_access,
        worker_id="crlf-verification",
        run_id=RUN_ID,
        task_id=TASK_ID,
    )
    assert verify.read_bytes("src/crlf.txt").content == b"first\r\nreviewed\r\n"


def test_non_utf8_text_is_reviewable_but_cannot_enter_utf8_patch_apply(integration) -> None:
    manager, artifacts, _artifact, _permission, service = integration
    access = manager.acquire_for_worker(
        task_id=TASK_ID,
        session_id="",
        worker_id="cp1252-setup",
    )
    setup = WorkspaceEditPort(
        manager,
        access,
        worker_id="cp1252-setup",
        run_id=RUN_ID,
        task_id=TASK_ID,
    )
    setup.write_bytes(
        "src/cp1252.txt",
        "café\r\n".encode("cp1252"),
        idempotency_key="seed-cp1252",
    )
    artifact = artifacts.write_text(
        run_id=RUN_ID,
        task_id=TASK_ID,
        content=(
            "diff --git a/src/cp1252.txt b/src/cp1252.txt\n"
            "--- a/src/cp1252.txt\n"
            "+++ b/src/cp1252.txt\n"
            "@@ -1,1 +1,1 @@\n"
            "-café\n"
            "+cafe\n"
        ),
        title="cp1252.patch",
        kind=ArtifactKind.TEXT,
        extension=".patch",
        producer_node_id="node-cp1252",
        metadata={
            "media_type": "text/x-diff",
            "content_family": "text",
            "encoding": "utf-8",
            "security_label": "internal",
            "trust_disposition": "trusted",
            "download_policy": "allow",
        },
    )
    manifest = _manifest(service, artifact)
    file = manifest["files"][0]
    assert file["encoding"] == "cp1252"
    assert file["line_ending"] == "crlf"
    content = service.file_content(
        task_id=TASK_ID,
        run_id=RUN_ID,
        artifact=artifact,
        file_id=file["file_id"],
        revision=manifest["source"]["artifact_revision"],
        version="base",
    )
    assert content.body["text"] == "café\r\n"
    assert content.body["encoding"] == "cp1252"
    with pytest.raises(DiffReviewApiError) as captured:
        service.apply(
            task_id=TASK_ID,
            run_id=RUN_ID,
            artifact=artifact,
            payload={
                **_apply_payload(manifest),
                "idempotency_key": "diff-apply-cp1252",
                "causation_id": "diff-apply-cp1252-cause",
            },
        )
    assert captured.value.status == 415
    assert captured.value.code == "diff_apply_encoding_unsupported"
    verify_access = manager.acquire_for_worker(
        task_id=TASK_ID,
        session_id="",
        worker_id="cp1252-verification",
    )
    verify = WorkspaceEditPort(
        manager,
        verify_access,
        worker_id="cp1252-verification",
        run_id=RUN_ID,
        task_id=TASK_ID,
    )
    assert verify.read_bytes("src/cp1252.txt").content == "café\r\n".encode("cp1252")


def test_disabled_diff_review_fails_closed_before_projection(integration) -> None:
    manager, artifacts, artifact, permission, _service = integration
    disabled = DiffReviewApiService(
        artifact_service=ArtifactCatalogService(store=artifacts),
        workspace_manager=manager,
        permission_port=permission,
        artifact_store=artifacts,
        enabled=False,
    )
    with pytest.raises(DiffReviewApiError) as captured:
        _manifest(disabled, artifact)
    assert captured.value.status == 503
    assert captured.value.code == "diff_review_disabled"


def test_review_comment_is_permission_gated_and_emits_causal_receipt(integration) -> None:
    _manager, _artifacts, artifact, permission, service = integration
    manifest = _manifest(service, artifact)
    file = manifest["files"][0]
    page = service.page(
        task_id=TASK_ID,
        run_id=RUN_ID,
        artifact=artifact,
        file_id=file["file_id"],
        revision=manifest["source"]["artifact_revision"],
        page_index=0,
        maximum_bytes=8 * 1024 * 1024,
        maximum_lines=100_000,
    ).body
    manifest["files"][0]["_test_page"] = page
    response = service.review(
        task_id=TASK_ID,
        run_id=RUN_ID,
        artifact=artifact,
        payload=_review_payload(manifest),
    )
    assert response.status == 201
    assert response.body["accepted"] is True
    assert response.body["revision"] == 1
    assert response.body["comment"]["state"] == "submitted"
    assert response.body["permission"]["canonical_owner"] == "typescript.PermissionCoordinator"
    assert response.body["human_intervention_count"] == 0
    assert permission.enforcements[-1]["arguments"]["action"] == "comment"
    event = response.events[0]
    assert event.payload["causation_id"] == "review-cause-comment-False"
    assert event.payload["diff_review"]["comment"]["body"].startswith("Please preserve")
    assert event.payload["diff_review"]["state_owner"] == "canonical event log"


def test_review_ask_and_sealed_allow_are_read_only_rejections(integration) -> None:
    _manager, _artifacts, artifact, permission, service = integration
    manifest = _manifest(service, artifact)
    file = manifest["files"][0]
    page = service.page(
        task_id=TASK_ID,
        run_id=RUN_ID,
        artifact=artifact,
        file_id=file["file_id"],
        revision=manifest["source"]["artifact_revision"],
        page_index=0,
        maximum_bytes=8 * 1024 * 1024,
        maximum_lines=100_000,
    ).body
    manifest["files"][0]["_test_page"] = page
    permission.effect = "ask"
    pending = service.review(
        task_id=TASK_ID,
        run_id=RUN_ID,
        artifact=artifact,
        payload=_review_payload(manifest),
    )
    assert pending.status == 202
    assert pending.body["accepted"] is False
    assert pending.body["revision"] == 0
    assert pending.body["permission"]["effect"] == "ask"
    permission.effect = "allow"
    sealed = service.review(
        task_id=TASK_ID,
        run_id=RUN_ID,
        artifact=artifact,
        payload=_review_payload(manifest, sealed=True),
    )
    assert sealed.status == 403
    assert sealed.body["accepted"] is False
    assert sealed.body["revision"] == 0
    assert sealed.body["permission"]["effect"] == "deny"
    assert sealed.body["human_intervention_count"] == 0
    assert sealed.events[0].payload["diff_review"]["accepted"] is False


def test_allow_commits_through_workspace_owner_and_idempotently_replays(integration) -> None:
    manager, _artifacts, artifact, permission, service = integration
    manifest = _manifest(service, artifact)
    payload = _apply_payload(manifest)
    response = service.apply(
        task_id=TASK_ID,
        run_id=RUN_ID,
        artifact=artifact,
        payload=payload,
    )
    receipt = response.body
    assert receipt["schema"] == PATCH_RECEIPT_SCHEMA
    assert receipt["phase"] == "committed"
    assert receipt["committed"] is True
    assert receipt["human_intervention_count"] == 0
    assert receipt["permission"]["canonical_owner"] == "typescript.PermissionCoordinator"
    assert receipt["path_results"][0]["verified"] is True
    assert receipt["snapshot_id"]
    assert permission.enforcements[0]["arguments"]["diff_id"] == manifest["diff_id"]
    access = manager.acquire_for_worker(
        task_id=TASK_ID,
        session_id="",
        worker_id="verification",
    )
    verify = WorkspaceEditPort(
        manager,
        access,
        worker_id="verification",
        run_id=RUN_ID,
        task_id=TASK_ID,
    )
    assert verify.read_text("src/example.txt").text() == "alpha\nbeta reviewed\ngamma\ndelta\n"
    replay = service.apply(
        task_id=TASK_ID,
        run_id=RUN_ID,
        artifact=artifact,
        payload=payload,
    )
    assert replay.body["transaction_id"] == receipt["transaction_id"]
    assert replay.body["idempotent_replay"] is True


def test_ask_is_pending_without_write_and_exact_permit_can_retry(integration) -> None:
    manager, _artifacts, artifact, permission, service = integration
    permission.effect = "ask"
    manifest = _manifest(service, artifact)
    payload = _apply_payload(manifest)
    pending = service.apply(
        task_id=TASK_ID,
        run_id=RUN_ID,
        artifact=artifact,
        payload=payload,
    )
    assert pending.status == 202
    assert pending.body["phase"] == "permission_pending"
    assert pending.body["denied_manual_mutation_count"] == 1
    access = manager.acquire_for_worker(
        task_id=TASK_ID,
        session_id="",
        worker_id="verification",
    )
    verify = WorkspaceEditPort(
        manager,
        access,
        worker_id="verification",
        run_id=RUN_ID,
        task_id=TASK_ID,
    )
    assert verify.read_text("src/example.txt").text() == "alpha\nbeta\ngamma\n"
    retry = {
        **payload,
        "idempotency_key": "diff-apply-idempotency-permit",
        "causation_id": "diff-apply-cause-permit",
        "permission_permit_id": "permit-exact",
    }
    committed = service.apply(
        task_id=TASK_ID,
        run_id=RUN_ID,
        artifact=artifact,
        payload=retry,
    )
    assert committed.body["phase"] == "committed"
    assert committed.body["permission"]["permit_id"] == "permit-exact"


def test_sealed_ask_is_deterministic_denial_with_zero_human_intervention(integration) -> None:
    _manager, _artifacts, artifact, permission, service = integration
    permission.effect = "ask"
    manifest = _manifest(service, artifact)
    payload = {
        **_apply_payload(manifest),
        "sealed": True,
        "idempotency_key": "diff-apply-idempotency-sealed",
        "causation_id": "diff-apply-cause-sealed",
    }
    denied = service.apply(
        task_id=TASK_ID,
        run_id=RUN_ID,
        artifact=artifact,
        payload=payload,
    )
    assert denied.status == 403
    assert denied.body["phase"] == "permission_denied"
    assert denied.body["human_intervention_count"] == 0
    assert denied.body["permission"]["effect"] == "deny"
    assert denied.body["permission"]["recovery_input"]["sealed"] is True


def test_sealed_allow_is_still_deterministic_read_only(integration) -> None:
    manager, _artifacts, artifact, permission, service = integration
    permission.effect = "allow"
    manifest = _manifest(service, artifact)
    payload = {
        **_apply_payload(manifest),
        "sealed": True,
        "idempotency_key": "diff-apply-idempotency-sealed-allow",
        "causation_id": "diff-apply-cause-sealed-allow",
    }
    denied = service.apply(
        task_id=TASK_ID,
        run_id=RUN_ID,
        artifact=artifact,
        payload=payload,
    )
    assert denied.status == 403
    assert denied.body["phase"] == "permission_denied"
    assert denied.body["permission"]["effect"] == "deny"
    access = manager.acquire_for_worker(
        task_id=TASK_ID,
        session_id="",
        worker_id="sealed-verification",
    )
    verify = WorkspaceEditPort(
        manager,
        access,
        worker_id="sealed-verification",
        run_id=RUN_ID,
        task_id=TASK_ID,
    )
    assert verify.read_text("src/example.txt").text() == "alpha\nbeta\ngamma\n"


def test_stale_hash_blocks_transaction_before_workspace_mutation(integration) -> None:
    manager, _artifacts, artifact, _permission, service = integration
    manifest = _manifest(service, artifact)
    payload = _apply_payload(manifest)
    access = manager.acquire_for_worker(
        task_id=TASK_ID,
        session_id="",
        worker_id="external-change",
    )
    external = WorkspaceEditPort(
        manager,
        access,
        worker_id="external-change",
        run_id=RUN_ID,
        task_id=TASK_ID,
    )
    external.write_text(
        "src/example.txt",
        "alpha\nconcurrent\ngamma\n",
        idempotency_key="external-change",
    )
    stale = service.apply(
        task_id=TASK_ID,
        run_id=RUN_ID,
        artifact=artifact,
        payload=payload,
    )
    assert stale.status == 409
    assert stale.body["phase"] == "stale"
    assert stale.body["committed"] is False
    assert stale.body["path_results"][0]["verified"] is False


def test_committed_transaction_rollback_uses_snapshot_owner(integration) -> None:
    manager, _artifacts, artifact, _permission, service = integration
    manifest = _manifest(service, artifact)
    applied = service.apply(
        task_id=TASK_ID,
        run_id=RUN_ID,
        artifact=artifact,
        payload=_apply_payload(manifest),
    ).body
    rollback_payload = {
        "schema": "zyra.patch-review-rollback.v1",
        "task_id": TASK_ID,
        "run_id": RUN_ID,
        "diff_id": manifest["diff_id"],
        "transaction_id": applied["transaction_id"],
        "snapshot_id": applied["snapshot_id"],
        "idempotency_key": "diff-rollback-idempotency",
        "causation_id": "diff-rollback-cause",
        "actor_id": "reviewer",
        "session_id": SESSION_ID,
        "session_revision": 0,
        "worker_request_id": "diff-rollback-worker-request",
        "tool_call_id": "diff-rollback-tool-call",
        "sealed": False,
    }
    rolled_back = service.rollback(
        task_id=TASK_ID,
        run_id=RUN_ID,
        transaction_id=applied["transaction_id"],
        payload=rollback_payload,
    )
    assert rolled_back.body["phase"] == "rolled_back"
    assert rolled_back.body["rolled_back"] is True
    access = manager.acquire_for_worker(
        task_id=TASK_ID,
        session_id="",
        worker_id="rollback-verification",
    )
    verify = WorkspaceEditPort(
        manager,
        access,
        worker_id="rollback-verification",
        run_id=RUN_ID,
        task_id=TASK_ID,
    )
    assert verify.read_text("src/example.txt").text() == "alpha\nbeta\ngamma\n"


def test_rollback_failure_returns_recovery_receipt(
    integration,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _artifacts, artifact, _permission, service = integration
    manifest = _manifest(service, artifact)
    applied = service.apply(
        task_id=TASK_ID,
        run_id=RUN_ID,
        artifact=artifact,
        payload=_apply_payload(manifest),
    ).body

    def fail_restore(*_args: Any, **_kwargs: Any) -> None:
        raise WorkspaceError(
            WorkspaceErrorCode.RESTORE_FAILED,
            "Injected restore failure.",
            workspace_id=manifest["source"]["workspace_id"],
            operation="restore_workspace",
        )

    monkeypatch.setattr(manager, "restore", fail_restore)
    response = service.rollback(
        task_id=TASK_ID,
        run_id=RUN_ID,
        transaction_id=applied["transaction_id"],
        payload={
            "schema": "zyra.patch-review-rollback.v1",
            "task_id": TASK_ID,
            "run_id": RUN_ID,
            "diff_id": manifest["diff_id"],
            "transaction_id": applied["transaction_id"],
            "snapshot_id": applied["snapshot_id"],
            "idempotency_key": "diff-rollback-failure",
            "causation_id": "diff-rollback-failure-cause",
            "actor_id": "reviewer",
            "session_id": SESSION_ID,
            "session_revision": 0,
            "worker_request_id": "diff-rollback-failure-request",
            "tool_call_id": "diff-rollback-failure-tool",
            "sealed": False,
        },
    )
    assert response.status == 409
    assert response.body["phase"] == "rollback_failed"
    assert response.body["rollback_failed"] is True
    assert response.body["reason_code"] == "workspace.workspace_restore_failed"
    assert response.events[0].payload["diff_patch_transaction"]["phase"] == "rollback_failed"


def test_sealed_rollback_is_read_only_even_when_permission_allows(integration) -> None:
    manager, _artifacts, artifact, permission, service = integration
    permission.effect = "allow"
    manifest = _manifest(service, artifact)
    applied = service.apply(
        task_id=TASK_ID,
        run_id=RUN_ID,
        artifact=artifact,
        payload=_apply_payload(manifest),
    ).body
    denied = service.rollback(
        task_id=TASK_ID,
        run_id=RUN_ID,
        transaction_id=applied["transaction_id"],
        payload={
            "schema": "zyra.patch-review-rollback.v1",
            "task_id": TASK_ID,
            "run_id": RUN_ID,
            "diff_id": manifest["diff_id"],
            "transaction_id": applied["transaction_id"],
            "snapshot_id": applied["snapshot_id"],
            "idempotency_key": "diff-rollback-sealed",
            "causation_id": "diff-rollback-sealed-cause",
            "actor_id": "reviewer",
            "session_id": SESSION_ID,
            "session_revision": 0,
            "worker_request_id": "diff-rollback-sealed-request",
            "tool_call_id": "diff-rollback-sealed-tool",
            "sealed": True,
        },
    )
    assert denied.status == 403
    assert denied.body["phase"] == "permission_denied"
    assert denied.body["human_intervention_count"] == 0
    access = manager.acquire_for_worker(
        task_id=TASK_ID,
        session_id="",
        worker_id="sealed-rollback-verification",
    )
    verify = WorkspaceEditPort(
        manager,
        access,
        worker_id="sealed-rollback-verification",
        run_id=RUN_ID,
        task_id=TASK_ID,
    )
    assert verify.read_text("src/example.txt").text() == "alpha\nbeta reviewed\ngamma\ndelta\n"


def test_true_http_routes_reach_manifest_page_and_patch_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZYRA_SQLITE_PATH", str(tmp_path / "api.sqlite3"))
    monkeypatch.setenv("ZYRA_EVENT_LOG", str(tmp_path / "events.jsonl"))
    monkeypatch.setenv("ZYRA_TOOL_WORKSPACE", str(tmp_path / "tool-workspace"))
    monkeypatch.setenv("ZYRA_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("ZYRA_WORKSPACE_STATE_ROOT", str(tmp_path / "workspace-state"))
    monkeypatch.setenv("ZYRA_WORKSPACE_DATA_ROOT", str(tmp_path / "workspace-data"))
    monkeypatch.setenv("ZYRA_PERMISSION_STATE", str(tmp_path / "permissions.json"))
    module = importlib.import_module("apps.api.zyra_api.main")
    module = importlib.reload(module)
    permission = PermissionOwner("allow")
    server = ThreadingHTTPServer(("127.0.0.1", 0), module.ZyraRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    observed_headers: dict[str, str] = {}

    def request(
        path: str,
        *,
        method: str = "GET",
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = None
        headers: dict[str, str] = {}
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        selected = urllib.request.Request(
            base + path,
            data=data,
            method=method,
            headers=headers,
        )
        with urllib.request.urlopen(selected, timeout=15) as response:
            observed_headers.clear()
            observed_headers.update(dict(response.headers.items()))
            return json.loads(response.read().decode())

    try:
        created = request(
            "/tasks",
            method="POST",
            payload={"goal": "Exercise HTTP diff review.", "auto_run": False},
        )
        task = created["task"]
        state = module.get_store().load_task(task["task_id"])
        assert state is not None
        manager = module.get_workspace_manager()
        artifacts = module.LocalArtifactStore(module.artifact_root_path())
        module._DIFF_REVIEW_API_INSTANCE = DiffReviewApiService(
            artifact_service=ArtifactCatalogService(
                store=artifacts,
                audit=module._ARTIFACT_READ_AUDIT,
            ),
            workspace_manager=manager,
            permission_port=permission,
            registry=DiffReviewRegistry(maximum_sessions=8),
            artifact_store=artifacts,
        )
        module._DIFF_REVIEW_API_KEY = (
            id(manager),
            str(module.artifact_root_path().resolve()),
            False,
        )
        access = manager.acquire_for_worker(
            task_id=state.task_id,
            session_id="",
            worker_id="http-diff-setup",
        )
        setup = module.WorkspaceEditPort(
            manager,
            access,
            worker_id="http-diff-setup",
            run_id=state.run_id,
            task_id=state.task_id,
            artifact_store=artifacts,
        )
        setup.write_text(
            "src/http.txt",
            "before\n",
            idempotency_key="http-diff-seed",
        )
        artifact = artifacts.write_text(
            run_id=state.run_id,
            task_id=state.task_id,
            content=(
                "diff --git a/src/http.txt b/src/http.txt\n"
                "--- a/src/http.txt\n"
                "+++ b/src/http.txt\n"
                "@@ -1,1 +1,1 @@\n"
                "-before\n"
                "+after\n"
            ),
            title="http.patch",
            kind=module.ArtifactKind.TEXT,
            extension=".patch",
            producer_node_id=state.root_node_id,
            metadata={
                "media_type": "text/x-diff",
                "content_family": "text",
                "encoding": "utf-8",
                "security_label": "internal",
                "trust_disposition": "trusted",
                "download_policy": "allow",
            },
        )
        state.artifacts.append(artifact)
        module.get_store().save_checkpoint(state)
        revision = urllib.parse.quote(str(artifact.metadata["revision"]), safe="")
        manifest = request(
            f"/tasks/{state.task_id}/diff-reviews/{artifact.artifact_id}"
            f"?revision={revision}"
        )
        assert manifest["schema"] == DIFF_MANIFEST_SCHEMA
        file = manifest["files"][0]
        page = request(
            f"/tasks/{state.task_id}/diff-reviews/{artifact.artifact_id}"
            f"/files/{file['file_id']}/hunks?revision={revision}"
            "&page=0&maximum_bytes=8388608&maximum_lines=100000"
        )
        assert page["schema"] == DIFF_PAGE_SCHEMA
        apply_payload = {
            "schema": "zyra.patch-review-apply.v1",
            "task_id": state.task_id,
            "run_id": state.run_id,
            "diff_id": manifest["diff_id"],
            "artifact_id": artifact.artifact_id,
            "artifact_revision": manifest["source"]["artifact_revision"],
            "workspace_id": manifest["source"]["workspace_id"],
            "idempotency_key": "http-diff-apply-idempotency",
            "causation_id": "http-diff-apply-cause",
            "actor_id": "http-reviewer",
            "session_id": str(state.metadata["query_session_id"]),
            "session_revision": 0,
            "worker_request_id": "http-diff-worker-request",
            "tool_call_id": "http-diff-tool-call",
            "expected_owner_epoch": manifest["source"]["owner_epoch"],
            "expected_binding_revision": manifest["source"]["binding_revision"],
            "expected_lease_id": manifest["source"]["lease_id"],
            "selected_file_ids": [file["file_id"]],
            "preconditions": [{
                "file_id": file["file_id"],
                "path": file["path"],
                "kind": file["kind"],
                "base_sha256": file["old_sha256"],
                "current_sha256": file["current_sha256"],
                "proposed_sha256": file["new_sha256"],
                "base_mtime_ns": file["old_mtime_ns"],
                "current_mtime_ns": file["current_mtime_ns"],
                "base_mode": file["old_mode"],
                "current_mode": file["current_mode"],
                "encoding": file["encoding"],
                "line_ending": file["line_ending"],
                "binary": False,
            }],
            "sealed": False,
            "review_revision": 0,
        }
        committed = request(
            f"/tasks/{state.task_id}/diff-reviews/{artifact.artifact_id}/apply",
            method="POST",
            payload=apply_payload,
        )
        assert committed["phase"] == "committed"
        assert committed["human_intervention_count"] == 0
        assert observed_headers["X-Zyra-Receipt-Id"] == committed["receipt_id"]
        verify_access = manager.acquire_for_worker(
            task_id=state.task_id,
            session_id="",
            worker_id="http-diff-verify",
        )
        verify = module.WorkspaceEditPort(
            manager,
            verify_access,
            worker_id="http-diff-verify",
            run_id=state.run_id,
            task_id=state.task_id,
        )
        assert verify.read_text("src/http.txt").text() == "after\n"
        events = module.get_store().task_events(state.task_id)
        assert any("diff_patch_transaction" in event["payload"] for event in events)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        module.reset_diff_review_api()
        module.reset_workspace_manager()
