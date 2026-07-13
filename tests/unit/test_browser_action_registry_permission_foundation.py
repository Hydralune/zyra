from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from zyra_runtime import WorkerRequest
from zyra_runtime.permission.action_gate import BrowserActionPermissionGate
from zyra_runtime.permission.models import PermissionEffect, PermissionResolutionResponse
from zyra_runtime.permission.request_queue import PermissionRequestQueue
from zyra_runtime.permission.store import PermissionStateStore
from zyra_workers.browser_action import (
    ActionArgumentValidator,
    ActionIdentity,
    ActionRequest,
    BrowserActionFoundationFactory,
    BrowserActionFoundationOptions,
    BrowserActionGatewayError,
    BrowserClipboardGuard,
    BrowserFilePolicy,
    BrowserGeometryGuard,
    BrowserNetworkPolicy,
    BrowserRedirectGuard,
    BrowserSecretPolicy,
    BrowserSelectorGuard,
    ClipboardAccess,
    ExecutionMode,
    FileIntent,
    FilePolicyConfig,
    InMemorySecretProvider,
    InterceptedRequest,
    LiveElementProbe,
    MemoryArtifactPort,
    NetworkPolicyConfig,
    Point,
    RecordingCdpTransport,
    RecordingClipboardPort,
    RecordingElementProbe,
    RecordingFetchInterceptionPort,
    Rect,
    SecretDescriptor,
    SecretKind,
    SecretPurpose,
    SecretRedactor,
    SelectorExpectation,
    StaticHostResolver,
    Viewport,
    compile_patterns,
    default_browser_action_registry,
)
from zyra_workers.browser_action.file_policy import BrowserFilePolicyError
from zyra_workers.browser_action.geometry_guard import GeometryGuardError
from zyra_workers.browser_action.network_policy import NetworkPolicyError, canonicalize_url
from zyra_workers.browser_action.redirect_guard import InterceptionDisposition, RequestResourceType
from zyra_workers.browser_action.secret_policy import SecretPolicyError
from zyra_workers.browser_action.selector_guard import SelectorGuardError
from zyra_workers.browser_action.source_audit import BrowserActionSourceAuditor
from zyra_workers.browser_state.contracts import (
    BrowserSelectorEntry,
    BrowserSelectorMapRevision,
    BrowserSelectorRef,
    BrowserSelectorResolution,
    SelectorMapIdentity,
)


class _SelectorStore:
    def __init__(self, resolution: BrowserSelectorResolution) -> None:
        self.resolution = resolution
        self.disabled = False

    def resolve(self, selector_ref: str, *, expected_identity=None, require_current=True):
        if self.disabled:
            raise RuntimeError("selector store disabled")
        if selector_ref != self.resolution.entry.ref.opaque_ref:
            raise RuntimeError("selector missing")
        if expected_identity is not None and expected_identity != self.resolution.revision.identity:
            raise RuntimeError("selector identity mismatch")
        if require_current and (not self.resolution.current or self.resolution.revision.stale):
            raise RuntimeError("selector stale")
        return self.resolution


def _selector_resolution(*, cdp_session_id: str = "cdp-oopif", frame_id: str = "frame-child"):
    identity = SelectorMapIdentity(
        browser_session_id="browser-1",
        target_id="target-child",
        target_generation=3,
        cdp_session_id=cdp_session_id,
        cdp_generation=5,
        document_loader_id="loader-child",
    )
    ref = BrowserSelectorRef.create(
        revision_id="revision-child",
        selector_index=7,
        backend_node_id=701,
        identity_digest=identity.digest,
    )
    entry = BrowserSelectorEntry(
        ref=ref,
        node_id=70,
        backend_node_id=701,
        target_id="target-child",
        frame_id=frame_id,
        cdp_session_id=cdp_session_id,
        tag_name="button",
        role="button",
        accessible_name="Run",
        text_preview="Run",
        xpath="//*[@id='run']",
        css_hint="#run",
        stable_hash="stable-run",
        attributes_digest="attributes-run",
        visible=True,
        interactive=True,
        disabled=False,
        bounds={"x": 10, "y": 20, "width": 100, "height": 30},
    )
    revision = BrowserSelectorMapRevision(
        revision_id="revision-child",
        revision=4,
        identity=identity,
        capture_id="capture-child",
        capture_digest="capture-digest-child",
        entries=(entry,),
    )
    return BrowserSelectorResolution(entry, revision, True), SelectorExpectation(
        selector_ref=ref.opaque_ref,
        identity=identity,
        revision_id=revision.revision_id,
        selector_index=ref.selector_index,
        backend_node_id=entry.backend_node_id,
        frame_id=frame_id,
        stable_hash=entry.stable_hash,
        attributes_digest=entry.attributes_digest,
        expected_tag="button",
        expected_role="button",
    )


class BrowserActionRegistryFoundationTests(unittest.TestCase):
    def test_registry_is_self_contained_strict_and_projects_immutable_tools(self) -> None:
        registry = default_browser_action_registry()
        self.assertGreaterEqual(len(registry.names()), 25)
        self.assertEqual(registry.canonical_name("navigate"), "open_url")
        self.assertFalse(registry.source_summary()["filesystem_source_scan"])
        self.assertFalse(registry.source_summary()["external_runtime_required"])
        registry.assert_self_contained()
        tool = registry.tool_registry().get("open_url")
        self.assertIsNotNone(tool)
        with self.assertRaises(TypeError):
            tool.input_schema["properties"] = {}  # type: ignore[index]
        missing = registry.resolve("open_url", {})
        self.assertFalse(missing.arguments.ok)
        self.assertEqual(missing.arguments.issues[0].code, "missing_required_argument")
        unknown = registry.validate_plan([{"action": "does_not_exist", "arguments": {}}])
        self.assertEqual(unknown[0].reason, "unknown_action")
        wrong = registry.resolve("wait", {"seconds": float("nan")})
        self.assertFalse(wrong.arguments.ok)
        extra = registry.resolve("wait", {"seconds": 1, "unexpected": True})
        self.assertFalse(extra.arguments.ok)

    def test_registry_source_audit_rejects_no_runtime_source_dependency(self) -> None:
        root = Path(__file__).resolve().parents[2]
        package = root / "packages" / "workers" / "zyra_workers" / "browser_action"
        report = BrowserActionSourceAuditor(default_browser_action_registry(), package).audit()
        self.assertTrue(report.ok, [item.to_dict() for item in report.findings])
        self.assertEqual(report.registry_digest, default_browser_action_registry().digest)
        self.assertGreaterEqual(len(report.production_files), 15)


class BrowserNetworkAndRedirectPolicyTests(unittest.TestCase):
    def test_url_canonicalization_domain_boundary_and_ip_classes(self) -> None:
        canonical = canonicalize_url("HTTPS://Bücher.Example.:443/a?q=1")
        self.assertEqual(canonical.host, "xn--bcher-kva.example")
        self.assertEqual(canonical.origin, "https://xn--bcher-kva.example")
        config = NetworkPolicyConfig(
            allowed_patterns=compile_patterns(("*.example.test",)),
            require_allowlist=True,
        )
        resolver = StaticHostResolver({
            "www.example.test": ["8.8.8.8"],
            "example.test.evil": ["8.8.8.8"],
        })
        policy = BrowserNetworkPolicy(config, resolver)
        receipt = policy.preflight(action_id="action-1", raw_url="https://www.example.test/path")
        self.assertEqual(receipt.canonical_url.host, "www.example.test")
        with self.assertRaises(NetworkPolicyError) as denied:
            policy.preflight(action_id="action-1", raw_url="https://example.test.evil/path")
        self.assertEqual(denied.exception.code, "domain_not_allowed")
        private = BrowserNetworkPolicy(
            NetworkPolicyConfig(),
            StaticHostResolver({"private.example": ["169.254.169.254"]}),
        )
        with self.assertRaises(NetworkPolicyError) as blocked:
            private.preflight(action_id="action-private", raw_url="https://private.example/")
        self.assertEqual(blocked.exception.code, "resolved_ip_denied")

    def test_dns_rebinding_and_redirect_are_blocked_before_target_request(self) -> None:
        resolver = StaticHostResolver(
            {
                "allowed.test": [["8.8.8.8"], ["127.0.0.1"]],
                "denied.test": ["10.0.0.8"],
            }
        )
        policy = BrowserNetworkPolicy(NetworkPolicyConfig(), resolver)
        initial = policy.preflight(action_id="action-net", raw_url="https://allowed.test/start")
        with self.assertRaises(NetworkPolicyError) as rebound:
            policy.revalidate(initial)
        self.assertEqual(rebound.exception.code, "resolved_ip_denied")

        stable_policy = BrowserNetworkPolicy(
            NetworkPolicyConfig(),
            StaticHostResolver({"allowed.test": ["8.8.8.8"], "denied.test": ["10.0.0.8"]}),
        )
        admitted = stable_policy.preflight(action_id="action-redirect", raw_url="https://allowed.test/start")
        port = RecordingFetchInterceptionPort()
        guard = BrowserRedirectGuard(
            stable_policy,
            port,
            action_id="action-redirect",
            initial_receipt=admitted,
        )
        first = guard.handle(
            InterceptedRequest(
                interception_id="pause-1",
                network_request_id="request-1",
                frame_id="frame-main",
                target_id="target-main",
                url="https://allowed.test/start",
                method="GET",
                resource_type=RequestResourceType.DOCUMENT,
                is_navigation=True,
            )
        )
        denied = guard.handle(
            InterceptedRequest(
                interception_id="pause-2",
                network_request_id="request-2",
                frame_id="frame-main",
                target_id="target-main",
                url="https://denied.test/sink",
                method="GET",
                resource_type=RequestResourceType.DOCUMENT,
                is_navigation=True,
                redirect_from_request_id="request-1",
                redirect_status=302,
            )
        )
        self.assertEqual(first.disposition, InterceptionDisposition.CONTINUE)
        self.assertEqual(denied.disposition, InterceptionDisposition.FAIL)
        self.assertEqual(port.continued, ["pause-1"])
        self.assertEqual(port.failed, [("pause-2", "BlockedByClient")])


class BrowserFileSecretClipboardPolicyTests(unittest.TestCase):
    def test_file_containment_traversal_filename_and_owned_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            outside = root / "outside"
            workspace.mkdir()
            outside.mkdir()
            upload = workspace / "payload.txt"
            upload.write_text("payload", encoding="utf-8")
            escaped = outside / "secret.txt"
            escaped.write_text("secret", encoding="utf-8")
            policy = BrowserFilePolicy(
                FilePolicyConfig(
                    upload_roots=(str(workspace),),
                    download_root=str(root / "downloads"),
                    artifact_root=str(root / "artifacts"),
                )
            )
            receipt = policy.preflight_upload(action_id="upload-1", paths=(str(upload),))
            self.assertEqual(receipt.uploads[0].identity.size, 7)
            with self.assertRaises(BrowserFilePolicyError):
                policy.preflight_upload(action_id="upload-2", paths=(str(escaped),))
            for invalid in ("../escape.bin", "folder/file.bin", "CON", "name:stream", "name. "):
                with self.subTest(invalid=invalid), self.assertRaises(BrowserFilePolicyError):
                    policy.preflight_destination(action_id="download-invalid", filename=invalid)
            destination = policy.preflight_destination(
                action_id="download-ok",
                filename="evidence.bin",
                intent=FileIntent.DOWNLOAD,
            )
            with policy.open_download(destination) as writer:
                writer.write(b"browser evidence")
                completed = writer.complete()
            self.assertEqual(Path(completed.path).read_bytes(), b"browser evidence")
            self.assertEqual(completed.size, len(b"browser evidence"))

    def test_secret_is_scoped_resolved_after_preflight_once_and_redacted(self) -> None:
        value = "ULTRA-SECRET-SENTINEL"
        descriptor = SecretDescriptor(
            key="login.password",
            kind=SecretKind.PASSWORD,
            allowed_hosts=("login.example.test",),
            allowed_purposes=frozenset({SecretPurpose.FORM_INPUT}),
            value_length=len(value),
        )
        provider = InMemorySecretProvider({"login.password": (value, descriptor)})
        policy = BrowserSecretPolicy(provider)
        receipt = policy.preflight(
            action_id="secret-action",
            arguments={"text": "<secret>login.password</secret>"},
            target_url="https://login.example.test/form",
        )
        self.assertEqual(provider.resolve_count("login.password"), 0)
        self.assertNotIn(value, repr(receipt.public_dict()))
        with self.assertRaises(SecretPolicyError):
            policy.revalidate(receipt, target_url="https://evil.example.test/")
        lease = policy.materialize(receipt, target_url="https://login.example.test/form")
        self.assertEqual(provider.resolve_count("login.password"), 1)
        self.assertEqual(lease.resolved_arguments["text"], value)
        with self.assertRaises(SecretPolicyError):
            policy.materialize(receipt, target_url="https://login.example.test/form")
        redacted = SecretRedactor((value,)).redact({"error": f"failed {value}", "authorization": value})
        self.assertNotIn(value, repr(redacted))

    def test_clipboard_origin_one_use_and_revoke(self) -> None:
        port = RecordingClipboardPort(value="clipboard secret")
        guard = BrowserClipboardGuard(port)
        receipt = guard.preflight(
            action_id="clipboard-read",
            target_url="https://app.example.test/page",
            browser_context_id="context-1",
            access=ClipboardAccess.READ,
        )
        self.assertEqual(guard.read(receipt, target_url="https://app.example.test/other"), "clipboard secret")
        self.assertEqual([item["operation"] for item in port.operations], ["grant", "read", "revoke"])
        with self.assertRaises(Exception):
            guard.read(receipt, target_url="https://app.example.test/")


class BrowserSelectorGeometryGatewayTests(unittest.TestCase):
    def test_selector_guard_binds_oopif_and_rejects_stale_or_wrong_frame(self) -> None:
        resolution, expectation = _selector_resolution()
        store = _SelectorStore(resolution)
        guard = BrowserSelectorGuard(store)
        receipt = guard.resolve(
            action_id="selector-action",
            expectation=expectation,
            action="click_element",
            current_url="https://child.example.test/",
        )
        self.assertEqual(receipt.binding.cdp_session_id, "cdp-oopif")
        self.assertEqual(receipt.binding.frame_id, "frame-child")
        store.resolution = BrowserSelectorResolution(resolution.entry, resolution.revision, False)
        with self.assertRaises(SelectorGuardError):
            guard.revalidate(
                receipt,
                expectation=expectation,
                action="click_element",
                current_url="https://child.example.test/",
            )

    def test_geometry_occlusion_has_no_dispatch_and_offscreen_scroll_reprobes(self) -> None:
        resolution, expectation = _selector_resolution()
        selector = BrowserSelectorGuard(_SelectorStore(resolution)).resolve(
            action_id="geometry-action",
            expectation=expectation,
            action="click_element",
            current_url="https://child.example.test/",
        )
        occluded = LiveElementProbe(
            backend_node_id=701,
            frame_id="frame-child",
            cdp_session_id="cdp-oopif",
            rects=(Rect(10, 20, 100, 30),),
            viewport=Viewport(800, 600),
            connected=True,
            visible=True,
            disabled=False,
            top_backend_node_id=999,
            document_loader_id="loader-child",
            node_name="button",
        )
        port = RecordingElementProbe([occluded])
        with self.assertRaises(GeometryGuardError) as blocked:
            BrowserGeometryGuard(port).preflight(selector=selector, action="click_element")
        self.assertEqual(blocked.exception.code, "element_occluded")
        self.assertEqual([item["operation"] for item in port.operations], ["probe"])

        offscreen = LiveElementProbe(
            backend_node_id=701,
            frame_id="frame-child",
            cdp_session_id="cdp-oopif",
            rects=(Rect(10, 1200, 100, 30),),
            viewport=Viewport(800, 600),
            connected=True,
            visible=True,
            disabled=False,
            top_backend_node_id=701,
            document_loader_id="loader-child",
            node_name="button",
        )
        current = LiveElementProbe(
            backend_node_id=701,
            frame_id="frame-child",
            cdp_session_id="cdp-oopif",
            rects=(Rect(10, 200, 100, 30),),
            viewport=Viewport(800, 600),
            connected=True,
            visible=True,
            disabled=False,
            top_backend_node_id=701,
            document_loader_id="loader-child",
            node_name="button",
        )
        scroll_port = RecordingElementProbe([offscreen, current])
        receipt = BrowserGeometryGuard(scroll_port).preflight(selector=selector, action="click_element")
        self.assertEqual(receipt.scroll_attempts, 1)
        self.assertEqual([item["operation"] for item in scroll_port.operations], ["probe", "scroll", "probe"])

    def test_special_controls_require_matching_dom_semantics(self) -> None:
        resolution, expectation = _selector_resolution()
        upload_entry = replace(
            resolution.entry,
            tag_name="input",
            role="textbox",
            css_hint="input[type='file']",
            accessible_name="Upload evidence",
            text_preview="Upload evidence",
        )
        upload_revision = replace(resolution.revision, entries=(upload_entry,))
        upload = BrowserSelectorGuard(
            _SelectorStore(BrowserSelectorResolution(upload_entry, upload_revision, True))
        ).resolve(
            action_id="upload-control",
            expectation=replace(expectation, expected_tag="input", expected_role="textbox"),
            action="upload_file",
            current_url="https://child.example.test/form",
        )
        self.assertEqual(upload.semantics.input_type, "file")
        self.assertIn("file_read", upload.semantics.inferred_effects)

        select_entry = replace(
            resolution.entry,
            tag_name="select",
            role="combobox",
            css_hint="#country",
            accessible_name="Country",
            text_preview="Country",
        )
        select_revision = replace(resolution.revision, entries=(select_entry,))
        dropdown = BrowserSelectorGuard(
            _SelectorStore(BrowserSelectorResolution(select_entry, select_revision, True))
        ).resolve(
            action_id="dropdown-control",
            expectation=replace(expectation, expected_tag="select", expected_role="combobox"),
            action="select_dropdown",
            current_url="https://child.example.test/form",
        )
        self.assertEqual(dropdown.semantics.input_type, "select")
        self.assertIn("framework_change_event", dropdown.semantics.inferred_effects)

    def test_form_destination_method_and_effects_are_dom_bound_before_permission(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            worker_request = WorkerRequest(
                run_id="run-form-policy",
                task_id="task-form-policy",
                node_id="node-form-policy",
                worker_name="BrowserWorker",
                constraints={"permission_mode": "default", "permission_interactive": True},
            )
            gate = BrowserActionPermissionGate.for_worker_request(
                worker_request,
                workspace_root=root / "workspace",
                state_path=root / "permission-state.json",
            )
            resolution, expectation = _selector_resolution()
            form_entry = replace(
                resolution.entry,
                accessible_name="Submit account",
                text_preview="Submit account",
                css_hint="button[formaction='https://child.example.test/submit'][formmethod='post']",
            )
            form_revision = replace(resolution.revision, entries=(form_entry,))
            selector_store = _SelectorStore(BrowserSelectorResolution(form_entry, form_revision, True))
            transport = RecordingCdpTransport()
            foundation = BrowserActionFoundationFactory.create(
                options=BrowserActionFoundationOptions(
                    workspace_root=root / "workspace",
                    artifact_root=root / "artifacts",
                    downloads_root=root / "downloads",
                    execution_mode=ExecutionMode.INTERACTIVE,
                ),
                permission_gate=gate,
                selector_store=selector_store,
                cdp_transport=transport,
                artifact_port=MemoryArtifactPort(),
                resolver=StaticHostResolver({"child.example.test": ["8.8.8.8"]}),
            )

            def request(step: int, method: str) -> ActionRequest:
                return ActionRequest(
                    ActionIdentity(
                        run_id=worker_request.run_id,
                        task_id=worker_request.task_id,
                        worker_request_id=worker_request.request_id,
                        session_id=gate.session_id,
                        browser_session_id="browser-1",
                        step_index=step,
                        node_id=worker_request.node_id,
                    ),
                    "submit_form",
                    {
                        "selector_ref": expectation.selector_ref,
                        "form_action_url": "https://child.example.test/submit",
                        "method": method,
                        "field_summary": {"account_token": {"$zyra_secret_ref": "account-token"}},
                    },
                    "zyra-browser-productized",
                    current_url="https://child.example.test/form",
                )

            prepared = foundation.gateway.prepare(request(1, "post"), selector_expectation=expectation)
            self.assertIsNotNone(prepared.security.form)
            self.assertEqual(str(prepared.security.form.method), "post")
            self.assertEqual(prepared.security.form.destination_origin, "https://child.example.test")
            self.assertIn("credential_submission", {str(item) for item in prepared.security.form.effects})
            self.assertEqual(prepared.receipt.form_receipt_id, prepared.security.form.receipt_id)
            self.assertEqual(prepared.security.network.canonical_url.url, "https://child.example.test/submit")
            self.assertEqual(len(transport.mutating_commands), 0)

            with self.assertRaises(BrowserActionGatewayError) as mismatch:
                foundation.gateway.prepare(request(2, "get"), selector_expectation=expectation)
            self.assertEqual(mismatch.exception.code, "form_method_hint_mismatch")
            self.assertEqual(len(transport.mutating_commands), 0)

            foundation.gateway.form_policy.disabled = True
            with self.assertRaises(BrowserActionGatewayError) as disabled:
                foundation.gateway.prepare(request(3, "post"), selector_expectation=expectation)
            self.assertEqual(disabled.exception.code, "form_policy_disabled")
            self.assertEqual(len(transport.mutating_commands), 0)

    def test_real_03a_permission_gateway_executes_read_once_and_sealed_evaluate_never_calls_cdp(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            worker_request = WorkerRequest(
                run_id="run-browser-foundation",
                task_id="task-browser-foundation",
                node_id="node-browser-foundation",
                worker_name="BrowserWorker",
                constraints={"permission_mode": "sealed", "permission_headless": True},
            )
            gate = BrowserActionPermissionGate.for_worker_request(
                worker_request,
                workspace_root=root / "workspace",
                state_path=root / "permission-state.json",
            )
            selector_resolution, _expectation = _selector_resolution()
            transport = RecordingCdpTransport(
                responses={"Page.getFrameTree": {"frameTree": {"frame": {"id": "frame-main"}}}}
            )
            foundation = BrowserActionFoundationFactory.create(
                options=BrowserActionFoundationOptions(
                    workspace_root=root / "workspace",
                    artifact_root=root / "artifacts",
                    downloads_root=root / "downloads",
                    execution_mode=ExecutionMode.SEALED_AUTONOMOUS,
                ),
                permission_gate=gate,
                selector_store=_SelectorStore(selector_resolution),
                cdp_transport=transport,
                artifact_port=MemoryArtifactPort(),
                resolver=StaticHostResolver({"example.test": ["8.8.8.8"]}),
            )
            identity = ActionIdentity(
                run_id=worker_request.run_id,
                task_id=worker_request.task_id,
                worker_request_id=worker_request.request_id,
                session_id=gate.session_id,
                browser_session_id="browser-1",
                step_index=1,
                node_id=worker_request.node_id,
            )
            completed = foundation.gateway.run(
                ActionRequest(identity, "snapshot_state", {}, "zyra-browser-productized")
            )
            self.assertTrue(completed.result.ok)
            self.assertEqual(transport.count("Page.getFrameTree"), 1)
            self.assertEqual(completed.result.cdp_effect_count, 0)
            with self.assertRaises(BrowserActionGatewayError) as denied:
                foundation.gateway.prepare(
                    ActionRequest(
                        ActionIdentity(
                            run_id=worker_request.run_id,
                            task_id=worker_request.task_id,
                            worker_request_id=worker_request.request_id,
                            session_id=gate.session_id,
                            browser_session_id="browser-1",
                            step_index=2,
                            node_id=worker_request.node_id,
                        ),
                        "evaluate_js",
                        {"code": "fetch('https://evil.example/')"},
                        "zyra-browser-productized",
                        current_url="https://example.test/",
                    )
                )
            self.assertEqual(denied.exception.code, "browser_action_policy_denied")
            self.assertEqual(transport.count("Runtime.evaluate"), 0)

    def test_interactive_ask_approval_revalidates_dns_consumes_exact_grant_and_replay_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            worker_request = WorkerRequest(
                run_id="run-browser-ask",
                task_id="task-browser-ask",
                node_id="node-browser-ask",
                worker_name="BrowserWorker",
                constraints={"permission_mode": "default", "permission_interactive": True},
            )
            state_path = root / "permission-state.json"
            gate = BrowserActionPermissionGate.for_worker_request(
                worker_request,
                workspace_root=root / "workspace",
                state_path=state_path,
            )
            selector_resolution, _expectation = _selector_resolution()
            transport = RecordingCdpTransport(
                responses={"Page.navigate": {"frameId": "frame-main", "loaderId": "loader-next"}}
            )
            foundation = BrowserActionFoundationFactory.create(
                options=BrowserActionFoundationOptions(
                    workspace_root=root / "workspace",
                    artifact_root=root / "artifacts",
                    downloads_root=root / "downloads",
                    execution_mode=ExecutionMode.INTERACTIVE,
                ),
                permission_gate=gate,
                selector_store=_SelectorStore(selector_resolution),
                cdp_transport=transport,
                artifact_port=MemoryArtifactPort(),
                resolver=StaticHostResolver({"example.test": ["8.8.8.8"]}),
            )
            request = ActionRequest(
                ActionIdentity(
                    run_id=worker_request.run_id,
                    task_id=worker_request.task_id,
                    worker_request_id=worker_request.request_id,
                    session_id=gate.session_id,
                    browser_session_id="browser-ask",
                    step_index=1,
                    node_id=worker_request.node_id,
                ),
                "open_url",
                {"url": "https://example.test/approved"},
                "zyra-browser-productized",
                current_url="https://example.test/start",
            )
            prepared = foundation.gateway.prepare(request)
            pending = foundation.gateway.authorize(prepared)
            self.assertTrue(pending.pending)
            self.assertFalse(pending.allowed)
            self.assertEqual(transport.count("Page.navigate"), 0)
            record = pending.permission.decision.guard.pending_request
            self.assertIsNotNone(record)
            outcome = PermissionRequestQueue(PermissionStateStore(state_path), record.session_id).resolve(
                PermissionResolutionResponse(
                    request_id=record.request_id,
                    session_id=record.session_id,
                    tool_use_id=record.tool_use_id,
                    tool_identity=record.tool_identity,
                    arguments_digest=record.arguments_digest,
                    request_fingerprint=record.request_fingerprint,
                    scope=record.scope,
                    effect=PermissionEffect.ALLOW,
                    actor_id="browser-foundation-test",
                    expected_revision=record.revision,
                    channel="test",
                    reason="approve exact browser action",
                    idempotency_key=f"approve:{record.request_id}",
                )
            )
            self.assertTrue(outcome.accepted)
            allowed = foundation.gateway.authorize(prepared)
            self.assertTrue(allowed.allowed)
            completed = foundation.gateway.execute(allowed)
            self.assertTrue(completed.result.ok)
            self.assertEqual(transport.count("Page.navigate"), 1)
            with self.assertRaises(BrowserActionGatewayError):
                foundation.gateway.execute(allowed)
            self.assertEqual(transport.count("Page.navigate"), 1)

    def test_ask_resume_with_stale_oopif_selector_stops_before_mutating_cdp(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            worker_request = WorkerRequest(
                run_id="run-selector-ask",
                task_id="task-selector-ask",
                node_id="node-selector-ask",
                worker_name="BrowserWorker",
                constraints={"permission_mode": "default", "permission_interactive": True},
            )
            state_path = root / "permission-state.json"
            gate = BrowserActionPermissionGate.for_worker_request(
                worker_request,
                workspace_root=root / "workspace",
                state_path=state_path,
            )
            resolution, expectation = _selector_resolution()
            selector_store = _SelectorStore(resolution)
            transport = RecordingCdpTransport()
            foundation = BrowserActionFoundationFactory.create(
                options=BrowserActionFoundationOptions(
                    workspace_root=root / "workspace",
                    artifact_root=root / "artifacts",
                    downloads_root=root / "downloads",
                    execution_mode=ExecutionMode.INTERACTIVE,
                ),
                permission_gate=gate,
                selector_store=selector_store,
                cdp_transport=transport,
                artifact_port=MemoryArtifactPort(),
                resolver=StaticHostResolver({"child.example.test": ["8.8.8.8"]}),
            )
            request = ActionRequest(
                ActionIdentity(
                    run_id=worker_request.run_id,
                    task_id=worker_request.task_id,
                    worker_request_id=worker_request.request_id,
                    session_id=gate.session_id,
                    browser_session_id="browser-1",
                    step_index=1,
                    node_id=worker_request.node_id,
                ),
                "click_element",
                {"selector_ref": expectation.selector_ref},
                "zyra-browser-productized",
                current_url="https://child.example.test/page",
            )
            prepared = foundation.gateway.prepare(request, selector_expectation=expectation)
            pending = foundation.gateway.authorize(prepared)
            self.assertTrue(pending.pending)
            record = pending.permission.decision.guard.pending_request
            outcome = PermissionRequestQueue(PermissionStateStore(state_path), record.session_id).resolve(
                PermissionResolutionResponse(
                    request_id=record.request_id,
                    session_id=record.session_id,
                    tool_use_id=record.tool_use_id,
                    tool_identity=record.tool_identity,
                    arguments_digest=record.arguments_digest,
                    request_fingerprint=record.request_fingerprint,
                    scope=record.scope,
                    effect=PermissionEffect.ALLOW,
                    actor_id="selector-stale-test",
                    expected_revision=record.revision,
                    channel="test",
                    reason="approve exact selector action",
                    idempotency_key=f"approve:{record.request_id}",
                )
            )
            self.assertTrue(outcome.accepted)
            allowed = foundation.gateway.authorize(prepared)
            self.assertTrue(allowed.allowed)
            selector_store.resolution = BrowserSelectorResolution(
                resolution.entry,
                replace(resolution.revision, stale=True, stale_reason="superseded"),
                False,
            )
            with self.assertRaises(BrowserActionGatewayError) as stale:
                foundation.gateway.execute(allowed)
            self.assertIn(stale.exception.failure_kind, {"selector_stale", "selector_identity"})
            self.assertEqual(len(transport.mutating_commands), 0)


if __name__ == "__main__":
    unittest.main()
