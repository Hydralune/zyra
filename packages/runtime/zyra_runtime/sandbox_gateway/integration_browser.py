from __future__ import annotations

import ipaddress
import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence, TYPE_CHECKING
from urllib.parse import unquote, urlparse

from .artifact_port import FileArtifactRequest, FileArtifactReceipt
from .redaction import redact_for_event
from .models import OperationKind, ProvenanceKind, TrustLevel
from .integration_models import (
    FailureClass,
    GatewayAction,
    GatewayExecutionReceipt,
    GatewayOutcome,
    GatewaySurface,
    RecoveryAction,
    TrustDisposition,
    WorkerGatewayIdentity,
    content_digest,
    invocation_for,
    stable_identifier,
)

if TYPE_CHECKING:
    from .integration_factory import GatewayRuntimeBundle


@dataclass(frozen=True, slots=True)
class BrowserFetchResult:
    request_id: str
    url_digest: str
    final_url_digest: str
    status: int
    content: bytes
    content_type: str
    charset: str
    provenance_ref: str
    gateway_receipt: GatewayExecutionReceipt
    headers: Mapping[str, str] = field(default_factory=dict)

    def text(self) -> str:
        return self.content.decode(self.charset or "utf-8", errors="replace")

    def safe_dict(self, *, include_content: bool = False) -> dict[str, Any]:
        value = {
            "request_id": self.request_id,
            "url_digest": self.url_digest,
            "final_url_digest": self.final_url_digest,
            "status": self.status,
            "content_digest": content_digest(self.content),
            "content_bytes": len(self.content),
            "content_type": self.content_type,
            "charset": self.charset,
            "provenance_ref": self.provenance_ref,
            "headers": dict(self.headers),
            "gateway_receipt": self.gateway_receipt.safe_dict(),
        }
        if include_content:
            value["content"] = self.text()
        return value


@dataclass(frozen=True, slots=True)
class BrowserPlanReceipt:
    receipt_id: str
    identity: WorkerGatewayIdentity
    plan_digest: str
    action_count: int
    network_targets: tuple[str, ...]
    upload_paths: tuple[str, ...]
    allowed: bool
    policy_digest: str
    findings: tuple[Mapping[str, Any], ...] = ()
    created_at: float = field(default_factory=time.time)

    def safe_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "identity": self.identity.safe_dict(),
            "plan_digest": self.plan_digest,
            "action_count": self.action_count,
            "network_target_digests": [content_digest(item) for item in self.network_targets],
            "upload_paths": list(self.upload_paths),
            "allowed": self.allowed,
            "policy_digest": self.policy_digest,
            "findings": [dict(item) for item in self.findings],
            "created_at": self.created_at,
        }


class BrowserGatewayBoundary:
    def __init__(self, bundle: "GatewayRuntimeBundle") -> None:
        self.bundle = bundle
        self._plan_receipts: dict[str, BrowserPlanReceipt] = {}

    def identity_for_request(self, request: Any) -> WorkerGatewayIdentity:
        run_id = str(getattr(request, "run_id", "") or "")
        task_id = str(getattr(request, "task_id", "") or "")
        node_id = str(getattr(request, "node_id", "") or "")
        request_id = str(getattr(request, "request_id", "") or "")
        constraints = dict(getattr(request, "constraints", {}) or {})
        access = _current_access(self.bundle.workspace_edit_port)
        workspace_id = str(getattr(access, "workspace_id", "") or "")
        owner_epoch = int(getattr(access, "owner_epoch", 0) or 0)
        session_id = str(constraints.get("browser_session_id") or constraints.get("session_id") or "")
        if not session_id:
            session_id = stable_identifier(
                "gateway-session",
                run_id,
                task_id,
                "BrowserWorker",
                workspace_id,
            )
        return WorkerGatewayIdentity(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            worker_id="BrowserWorker",
            session_id=session_id,
            request_id=request_id,
            workspace_id=workspace_id,
            owner_epoch=owner_epoch,
            backend_id="browser-runtime",
        )

    def preflight_plan(
        self,
        request: Any,
        plan: Sequence[Mapping[str, Any]],
    ) -> BrowserPlanReceipt:
        identity = self.identity_for_request(request)
        constraints = dict(getattr(request, "constraints", {}) or {})
        findings: list[Mapping[str, Any]] = []
        targets: list[str] = []
        uploads: list[str] = []
        for index, step in enumerate(plan):
            action = str(step.get("action") or step.get("name") or "").casefold()
            arguments = dict(step.get("arguments") or {})
            url = str(arguments.get("url") or arguments.get("href") or "")
            if url:
                decision = self.bundle.policy_runtime.evaluate_url(
                    url,
                    allowed_schemes=constraints.get("allowed_schemes") or (),
                    allowed_hosts=constraints.get("allowed_hosts") or (),
                )
                targets.append(url)
                if not decision.allowed:
                    findings.append(
                        {
                            "index": index,
                            "action": action,
                            "code": "browser_network_target_denied",
                            "reason": decision.reason,
                            "policy": decision.safe_dict(),
                        }
                    )
            if action in {"upload", "set_input_files", "upload_file"}:
                values = arguments.get("paths") or arguments.get("files") or arguments.get("path") or ()
                if isinstance(values, str):
                    values = (values,)
                for value in values:
                    try:
                        uploads.append(self.bundle.policy_runtime.assert_path(str(value)))
                    except ValueError as error:
                        findings.append(
                            {
                                "index": index,
                                "action": action,
                                "code": "browser_upload_path_denied",
                                "reason": str(error),
                            }
                        )
            if action in {"execute_script", "javascript", "eval", "install_extension"}:
                findings.append(
                    {
                        "index": index,
                        "action": action,
                        "code": "browser_code_execution_requires_dedicated_approval",
                        "reason": "browser plan cannot execute generated code through the generic action path",
                    }
                )
        receipt = BrowserPlanReceipt(
            receipt_id=stable_identifier(
                "gateway-browser-plan",
                identity.binding_digest,
                content_digest(plan),
            ),
            identity=identity,
            plan_digest=content_digest(plan),
            action_count=len(plan),
            network_targets=tuple(targets),
            upload_paths=tuple(uploads),
            allowed=not findings,
            policy_digest=self.bundle.policy_runtime.policy_digest,
            findings=tuple(findings),
        )
        self._plan_receipts[receipt.receipt_id] = receipt
        if findings:
            self.bundle.signal_emitter.emit(
                identity,
                invocation_id=receipt.receipt_id,
                failure_class=FailureClass.POLICY,
                code="browser_plan_denied",
                reason=str(findings[0]["reason"]),
                retryable=False,
                recovery_actions=(RecoveryAction.REDUCE_SCOPE, RecoveryAction.REPLAN),
                causation_id=str(getattr(request, "request_id", "") or ""),
                metadata={"plan_digest": receipt.plan_digest},
            )
        return receipt

    def load_url(self, url: str, request: Any) -> str:
        parsed = urlparse(str(url))
        if parsed.scheme in {"workspace", "file"}:
            return self.read_workspace_url(url, request)
        result = self.fetch_url(url, request)
        return result.text()

    def read_workspace_url(self, url: str, request: Any) -> str:
        parsed = urlparse(str(url))
        if parsed.scheme == "workspace":
            if parsed.netloc not in {"", "task"}:
                raise ValueError("workspace URL authority must be empty or task")
            logical_path = self.bundle.policy_runtime.assert_path(
                unquote(parsed.path).replace("\\", "/").lstrip("/")
            )
        elif parsed.scheme == "file":
            raw = Path(urllib.request.url2pathname(unquote(parsed.path)))
            if parsed.netloc:
                raise ValueError("UNC file URLs are denied")
            resolved = raw.resolve()
            try:
                relative = resolved.relative_to(self.bundle.workspace_root)
            except ValueError as error:
                raise ValueError("file URL is outside workspace custody") from error
            logical_path = self.bundle.policy_runtime.assert_path(relative.as_posix())
        else:
            raise ValueError("URL is not a workspace resource")
        if self.bundle.artifact_port is None:
            raise RuntimeError("sandbox_gateway_artifact_port_unavailable")
        content = self.bundle.artifact_port.read(logical_path)
        identity = self.identity_for_request(request)
        invocation = invocation_for(
            identity,
            surface=GatewaySurface.BROWSER_WORKER,
            action=GatewayAction.FILE_READ,
            tool_call_id=stable_identifier("gateway-browser-read", identity.request_id or identity.session_id, logical_path),
            logical_name=logical_path,
            arguments={"logical_path": logical_path, "scheme": parsed.scheme},
            policy_digest=self.bundle.policy_runtime.policy_digest,
            causation_id=identity.request_id,
        )
        receipt = GatewayExecutionReceipt(
            receipt_id=stable_identifier("gateway-execution", invocation.binding_digest, content_digest(content)),
            invocation=invocation,
            outcome=GatewayOutcome.ALLOWED,
            result_digest=content_digest(content),
            owner_epoch_before=identity.owner_epoch,
            owner_epoch_after=identity.owner_epoch,
            metadata={"browser_workspace_read": True},
        )
        self.bundle.receipt_journal.append(receipt)
        return content.decode("utf-8", errors="replace")

    def fetch_url(self, url: str, request: Any) -> BrowserFetchResult:
        identity = self.identity_for_request(request)
        constraints = dict(getattr(request, "constraints", {}) or {})
        decision = self.bundle.policy_runtime.evaluate_url(
            url,
            allowed_schemes=constraints.get("allowed_schemes") or (),
            allowed_hosts=constraints.get("allowed_hosts") or (),
        )
        if not decision.allowed:
            signal = self.bundle.signal_emitter.emit(
                identity,
                invocation_id=stable_identifier("gateway-browser-fetch", identity.binding_digest, decision.subject_digest),
                failure_class=FailureClass.POLICY,
                code="browser_network_denied",
                reason=decision.reason,
                retryable=False,
                recovery_actions=(RecoveryAction.REDUCE_SCOPE, RecoveryAction.REPLAN),
                causation_id=identity.request_id,
            )
            raise RuntimeError(f"browser_network_denied:{signal.signal_id}:{decision.reason}")
        self._assert_public_resolution(url)
        timeout = _bounded_float(constraints.get("browser_fetch_timeout_seconds"), 15.0, 0.5, 120.0)
        limit = _bounded_int(
            constraints.get("browser_fetch_max_bytes"),
            min(self.bundle.policy_runtime.config.maximum_browser_result_bytes, 8 * 1024 * 1024),
            1024,
            self.bundle.policy_runtime.config.maximum_browser_result_bytes,
        )
        request_id = stable_identifier("gateway-browser-fetch", identity.binding_digest, decision.subject_digest)
        request_obj = urllib.request.Request(
            str(url),
            headers={
                "User-Agent": "ZyraSandboxGateway/1.0",
                "Accept": str(constraints.get("browser_accept") or "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1"),
            },
            method="GET",
        )
        started = time.time()
        try:
            with urllib.request.urlopen(request_obj, timeout=timeout) as response:
                final_url = response.geturl()
                redirect_decision = self.bundle.policy_runtime.evaluate_url(
                    final_url,
                    allowed_schemes=constraints.get("allowed_schemes") or (),
                    allowed_hosts=constraints.get("allowed_hosts") or (),
                )
                if not redirect_decision.allowed:
                    raise RuntimeError(f"browser_redirect_denied:{redirect_decision.reason}")
                self._assert_public_resolution(final_url)
                declared_length = response.headers.get("Content-Length")
                if declared_length and int(declared_length) > limit:
                    raise RuntimeError("browser_response_budget_exceeded")
                content = response.read(limit + 1)
                if len(content) > limit:
                    raise RuntimeError("browser_response_budget_exceeded")
                content_type = response.headers.get_content_type() or "application/octet-stream"
                charset = response.headers.get_content_charset() or "utf-8"
                status = int(getattr(response, "status", 200) or 200)
                safe_headers = _safe_response_headers(response.headers)
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"browser_http_error:{error.code}") from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"browser_transport_error:{error.reason}") from error
        provenance = self.bundle.provenance_registry.derive(
            kind=ProvenanceKind.WEB,
            source_id=urlparse(str(url)).hostname or "web",
            parent_refs=(),
            content=content,
            trust=TrustLevel.UNTRUSTED,
            untrusted_instructions=True,
            metadata={
                "url_digest": content_digest(str(url)),
                "final_url_digest": content_digest(final_url),
                "status": status,
                "content_type": content_type,
            },
        )
        invocation = invocation_for(
            identity,
            surface=GatewaySurface.BROWSER_WORKER,
            action=GatewayAction.BROWSER_FETCH,
            tool_call_id=request_id,
            logical_name=urlparse(str(url)).hostname or "web",
            arguments={
                "url_digest": content_digest(str(url)),
                "final_url_digest": content_digest(final_url),
                "status": status,
            },
            trust=TrustDisposition.UNTRUSTED,
            provenance_ref=provenance.provenance_id,
            policy_digest=decision.policy_digest,
            causation_id=identity.request_id,
        )
        finished = time.time()
        receipt = GatewayExecutionReceipt(
            receipt_id=stable_identifier("gateway-execution", invocation.binding_digest, content_digest(content)),
            invocation=invocation,
            outcome=GatewayOutcome.ALLOWED,
            result_digest=content_digest(
                {
                    "status": status,
                    "content_digest": content_digest(content),
                    "content_type": content_type,
                }
            ),
            owner_epoch_before=identity.owner_epoch,
            owner_epoch_after=identity.owner_epoch,
            started_at=started,
            finished_at=finished,
            metadata={
                "network_policy_enforced": True,
                "redirect_revalidated": True,
                "bounded_content": True,
                "untrusted_instructions": True,
            },
        )
        self.bundle.receipt_journal.append(receipt)
        return BrowserFetchResult(
            request_id=request_id,
            url_digest=content_digest(str(url)),
            final_url_digest=content_digest(final_url),
            status=status,
            content=content,
            content_type=content_type,
            charset=charset,
            provenance_ref=provenance.provenance_id,
            gateway_receipt=receipt,
            headers=safe_headers,
        )

    def ingest_download(
        self,
        request: Any,
        *,
        logical_path: str,
        content: bytes,
        source_url: str,
        content_type: str = "application/octet-stream",
        idempotency_key: str = "",
    ) -> FileArtifactReceipt:
        if self.bundle.artifact_port is None:
            raise RuntimeError("sandbox_gateway_artifact_port_unavailable")
        if len(content) > self.bundle.policy_runtime.config.maximum_download_bytes:
            raise RuntimeError("browser_download_budget_exceeded")
        identity = self.identity_for_request(request)
        path = self.bundle.policy_runtime.assert_path(logical_path)
        provenance = self.bundle.provenance_registry.derive(
            kind=ProvenanceKind.DOWNLOAD,
            source_id=urlparse(source_url).hostname or "browser-download",
            parent_refs=(),
            content=content,
            trust=TrustLevel.UNTRUSTED,
            untrusted_instructions=True,
            metadata={
                "source_url_digest": content_digest(source_url),
                "browser_request_id": identity.request_id,
            },
        )
        file_request = FileArtifactRequest(
            request_id=stable_identifier("gateway-browser-download", identity.binding_digest, path, content_digest(content)),
            session_id=identity.session_id,
            logical_path=path,
            content=content,
            content_type=content_type,
            provenance=provenance,
            operation=OperationKind.BROWSER_TRANSFER,
            mount_kind="download",
            executable_allowed=False,
            archive_expansion_allowed=False,
            idempotency_key=idempotency_key or stable_identifier("browser-download", identity.request_id, path),
            causation_id=identity.request_id,
            metadata={"source_url_digest": content_digest(source_url)},
        )
        policy = self.bundle.policy_runtime.evaluate_file(file_request)
        if policy.hard_denied:
            raise RuntimeError(f"browser_download_denied:{policy.reason}")
        return self.bundle.artifact_port.commit(file_request)

    def export_upload(
        self,
        request: Any,
        *,
        logical_path: str,
    ) -> tuple[bytes, Any]:
        if self.bundle.artifact_port is None:
            raise RuntimeError("sandbox_gateway_artifact_port_unavailable")
        identity = self.identity_for_request(request)
        path = self.bundle.policy_runtime.assert_path(logical_path)
        return self.bundle.artifact_port.export(
            session_id=identity.session_id,
            logical_path=path,
            source_id="BrowserWorker",
        )

    def descriptor(self) -> Mapping[str, Any]:
        return {
            "runtime": "BrowserGatewayBoundary",
            "plan_receipts": len(self._plan_receipts),
            "network_policy_digest": self.bundle.policy_runtime.policy_digest,
            "workspace_file_owner": "GatewayFileArtifactPort",
            "download_quarantine": True,
            "raw_urlopen_fallback": False,
        }

    def _assert_public_resolution(self, url: str) -> None:
        parsed = urlparse(str(url))
        host = parsed.hostname or ""
        if not host:
            raise RuntimeError("browser_network_host_missing")
        try:
            records = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        except socket.gaierror as error:
            raise RuntimeError("browser_network_dns_failed") from error
        addresses = {
            ipaddress.ip_address(item[4][0].split("%", 1)[0])
            for item in records
        }
        if not addresses:
            raise RuntimeError("browser_network_dns_empty")
        for address in addresses:
            if address.is_loopback and not self.bundle.policy_runtime.config.allow_loopback_network:
                raise RuntimeError("browser_loopback_target_denied")
            if (
                address.is_private
                or address.is_link_local
                or address.is_multicast
                or address.is_unspecified
                or address.is_reserved
            ) and not self.bundle.policy_runtime.config.allow_private_network:
                raise RuntimeError("browser_private_target_denied")


def _current_access(port: Any) -> Any:
    current = getattr(port, "current_access", None)
    return current() if callable(current) else None


def _safe_response_headers(headers: Any) -> Mapping[str, str]:
    allowed = {
        "content-type",
        "content-length",
        "content-language",
        "last-modified",
        "etag",
        "cache-control",
    }
    return {
        str(key).casefold(): str(value)
        for key, value in headers.items()
        if str(key).casefold() in allowed
    }


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        selected = int(value)
    except (TypeError, ValueError):
        selected = default
    return max(minimum, min(maximum, selected))


def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        selected = float(value)
    except (TypeError, ValueError):
        selected = default
    return max(minimum, min(maximum, selected))


__all__ = [
    "BrowserFetchResult",
    "BrowserGatewayBoundary",
    "BrowserPlanReceipt",
]
