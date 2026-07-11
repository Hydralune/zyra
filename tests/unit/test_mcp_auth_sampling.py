from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
for package_path in (
    ROOT / "packages" / "core",
    ROOT / "packages" / "integrations",
    ROOT / "packages" / "runtime",
):
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))


from zyra_integrations.mcp.auth import (  # noqa: E402
    AuthConfigurationError,
    AuthMode,
    AuthNetworkError,
    AuthRuntimeConfig,
    AuthStatus,
    CallbackAuthNetworkProvider,
    DurableNeedsAuthCache,
    McpAuthRuntime,
    OAuthMetadata,
    OAuthMetadataCache,
    OAuthTokenResponse,
    ProbeDecision,
    ProbeResult,
    RefreshErrorKind,
    XaaConfiguration,
    classify_refresh_error,
    compute_config_fingerprint,
)
from zyra_integrations.mcp.credentials import (  # noqa: E402
    CredentialConflict,
    CredentialKind,
    CredentialReference,
    FileCredentialVault,
    MemoryCredentialVault,
    SecretValue,
    VaultCredentialProvider,
)
from zyra_integrations.mcp.sampling import (  # noqa: E402
    McpSamplingRuntime,
    SamplingDecision,
    SamplingPolicy,
    SamplingReason,
    SamplingRequest,
    SamplingResponse,
)


class MutableClock:
    def __init__(self, value: float = 1_700_000_000.0) -> None:
        self._value = value
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self._value

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._value += seconds


def metadata() -> OAuthMetadata:
    return OAuthMetadata(
        issuer="https://identity.example.test",
        authorization_endpoint="https://identity.example.test/authorize",
        token_endpoint="https://identity.example.test/token",
        revocation_endpoint="https://identity.example.test/revoke",
        protected_resource="https://mcp.example.test",
        scopes_supported=("read", "write"),
    )


def auth_config(
    *,
    ttl: float = 30.0,
    skew: float = 5.0,
    mode: AuthMode = AuthMode.OAUTH,
    xaa: XaaConfiguration | None = None,
) -> AuthRuntimeConfig:
    raw = {
        "url": "https://mcp.example.test",
        "transport": "streamable_http",
        "headers": {"Authorization": "Bearer initial-secret"},
        "auth": {"type": str(mode), "scope": "read"},
    }
    return AuthRuntimeConfig(
        server_id="server-a",
        config_fingerprint=compute_config_fingerprint(raw),
        mode=mode,
        requested_scopes=("read",),
        resource="https://mcp.example.test",
        needs_auth_ttl_seconds=ttl,
        refresh_skew_seconds=skew,
        xaa=xaa,
    )


class RecordingAuthProvider:
    def __init__(self) -> None:
        self.probe_count = 0
        self.refresh_count = 0
        self.discover_count = 0
        self.revoke_order: list[str] = []
        self.exchange_count = 0
        self.probe_delay = 0.0
        self.probe_result = ProbeResult(ProbeDecision.NEEDS_AUTH, reason_code="login_required")
        self.refresh_result = OAuthTokenResponse(
            access_token="access-refreshed",
            refresh_token="refresh-rotated",
            expires_in=120,
            scopes=("read",),
            resource="https://mcp.example.test",
            issuer="https://identity.example.test",
        )
        self.refresh_error: BaseException | None = None
        self.revoke_error_hints: set[str] = set()
        self._lock = threading.Lock()

    def discover(self, config: AuthRuntimeConfig) -> OAuthMetadata:
        del config
        with self._lock:
            self.discover_count += 1
        return metadata()

    def probe(self, config: AuthRuntimeConfig, discovered: OAuthMetadata | None) -> ProbeResult:
        del config, discovered
        with self._lock:
            self.probe_count += 1
        if self.probe_delay:
            time.sleep(self.probe_delay)
        return self.probe_result

    def refresh(
        self,
        config: AuthRuntimeConfig,
        discovered: OAuthMetadata,
        refresh_token: SecretValue,
        *,
        scopes: tuple[str, ...],
        resource: str,
    ) -> OAuthTokenResponse:
        del config, discovered
        self.asserted_refresh_token = refresh_token.reveal_text()
        self.asserted_scopes = scopes
        self.asserted_resource = resource
        with self._lock:
            self.refresh_count += 1
        if self.refresh_error is not None:
            raise self.refresh_error
        return self.refresh_result

    def revoke(
        self,
        config: AuthRuntimeConfig,
        discovered: OAuthMetadata | None,
        token: SecretValue,
        *,
        token_type_hint: str,
    ) -> None:
        del config, discovered
        self.asserted_revocation_token_present = bool(token.reveal_text())
        self.revoke_order.append(token_type_hint)
        if token_type_hint in self.revoke_error_hints:
            raise AuthNetworkError(code="temporarily_unavailable", status=503, retryable=True)

    def exchange_xaa(
        self,
        config: AuthRuntimeConfig,
        xaa: XaaConfiguration,
        id_token: SecretValue,
    ) -> OAuthTokenResponse:
        del config
        self.exchange_count += 1
        self.asserted_id_token = id_token.reveal_text()
        return OAuthTokenResponse(
            access_token="xaa-access",
            refresh_token="xaa-refresh",
            id_token="",
            expires_in=120,
            scopes=("read",),
            issuer=xaa.issuer,
            resource=xaa.resource,
        )


class CredentialVaultTests(unittest.TestCase):
    def test_atomic_file_vault_keeps_secrets_out_of_safe_surfaces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault = FileCredentialVault(Path(directory) / "vault")
            reference = CredentialReference("mcp", "server-a", "mcp-config:sha256:" + "a" * 64)
            metadata_record = vault.put(
                reference,
                {"access_token": "access-super-secret", "refresh_token": "refresh-super-secret"},
                public_attributes={"token_type": "Bearer", "scopes": ["read"]},
                expires_at="2030-01-01T00:00:00Z",
            )
            self.assertEqual(metadata_record.revision, 1)
            self.assertNotIn("access-super-secret", json.dumps(metadata_record.safe_dict()))
            self.assertNotIn("refresh-super-secret", json.dumps(metadata_record.safe_dict()))
            self.assertNotIn("access-super-secret", repr(vault.get(reference)))
            with vault.get(reference) as envelope:
                self.assertEqual(envelope.reveal_text("access_token"), "access-super-secret")
                self.assertTrue(envelope.secret_fingerprints()["access_token"].startswith("sha256:"))
            files = list((Path(directory) / "vault").glob("*.credential"))
            self.assertEqual(len(files), 1)
            self.assertFalse(list((Path(directory) / "vault").glob("*.tmp-*")))
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(files[0].stat().st_mode) & 0o077, 0)

    def test_compare_and_swap_and_external_version(self) -> None:
        vault = MemoryCredentialVault()
        reference = CredentialReference("mcp", "server-a", "mcp-config:sha256:" + "b" * 64)
        first = vault.put(reference, {"access_token": "one"})
        second = vault.put(reference, {"access_token": "two"}, expected_revision=first.revision)
        self.assertNotEqual(first.external_version, second.external_version)
        with self.assertRaises(CredentialConflict):
            vault.put(reference, {"access_token": "three"}, expected_revision=first.revision)

    def test_secret_value_never_displays_plaintext_and_can_be_destroyed(self) -> None:
        secret = SecretValue("do-not-display")
        self.assertNotIn("do-not-display", repr(secret))
        self.assertNotIn("do-not-display", str(secret))
        self.assertTrue(secret.constant_time_equals("do-not-display"))
        secret.destroy()
        self.assertTrue(secret.destroyed)
        with self.assertRaises(Exception):
            secret.reveal_text()


class AuthRuntimeTests(unittest.TestCase):
    def make_runtime(
        self,
        directory: str,
        clock: MutableClock,
        provider: RecordingAuthProvider,
        *,
        config: AuthRuntimeConfig | None = None,
        events: list[dict] | None = None,
    ) -> McpAuthRuntime:
        selected_config = config or auth_config()
        vault = FileCredentialVault(Path(directory) / "vault", clock=clock)
        return McpAuthRuntime(
            selected_config,
            credential_vault=vault,
            network_provider=provider,
            needs_auth_cache=DurableNeedsAuthCache(
                Path(directory) / "needs-auth.json",
                clock=clock,
                default_ttl_seconds=selected_config.needs_auth_ttl_seconds,
            ),
            metadata_cache=OAuthMetadataCache(Path(directory) / "metadata.json"),
            event_sink=events.append if events is not None else None,
            clock=clock,
        )

    def test_sixteen_concurrent_probes_are_single_flight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clock = MutableClock()
            provider = RecordingAuthProvider()
            provider.probe_delay = 0.12
            runtime = self.make_runtime(directory, clock, provider)
            barrier = threading.Barrier(16)

            def run_probe(_: int) -> AuthStatus:
                barrier.wait(timeout=2)
                return runtime.probe().status

            with ThreadPoolExecutor(max_workers=16) as pool:
                statuses = list(pool.map(run_probe, range(16)))
            self.assertEqual(statuses, [AuthStatus.NEEDS_AUTH] * 16)
            self.assertEqual(provider.probe_count, 1)

    def test_needs_auth_cache_survives_restart_and_expires(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clock = MutableClock()
            first_provider = RecordingAuthProvider()
            first = self.make_runtime(directory, clock, first_provider)
            self.assertEqual(first.probe().status, AuthStatus.NEEDS_AUTH)
            self.assertEqual(first_provider.probe_count, 1)

            second_provider = RecordingAuthProvider()
            second = self.make_runtime(directory, clock, second_provider)
            cached = second.probe()
            self.assertTrue(cached.cache_hit)
            self.assertEqual(second_provider.probe_count, 0)

            clock.advance(31)
            third_provider = RecordingAuthProvider()
            third = self.make_runtime(directory, clock, third_provider)
            self.assertEqual(third.probe().status, AuthStatus.NEEDS_AUTH)
            self.assertEqual(third_provider.probe_count, 1)

    def test_config_fingerprint_includes_secret_change_without_exposing_secret(self) -> None:
        one = compute_config_fingerprint(
            {"url": "https://mcp.example.test", "headers": {"Authorization": "first-secret"}}
        )
        two = compute_config_fingerprint(
            {"url": "https://mcp.example.test", "headers": {"Authorization": "second-secret"}}
        )
        self.assertNotEqual(one, two)
        self.assertNotIn("first-secret", one)
        self.assertNotIn("second-secret", two)

    def test_cold_expiry_refreshes_and_preserves_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clock = MutableClock()
            provider = RecordingAuthProvider()
            runtime = self.make_runtime(directory, clock, provider)
            runtime.set_metadata(metadata())
            runtime.install_tokens(
                OAuthTokenResponse(
                    access_token="access-old",
                    refresh_token="refresh-old",
                    expires_in=10,
                    scopes=("read",),
                    resource="https://mcp.example.test",
                    issuer="https://identity.example.test",
                )
            )
            clock.advance(20)
            restarted = self.make_runtime(directory, clock, provider)
            outcome = restarted.ensure_authorized()
            self.assertTrue(outcome.authorized)
            self.assertEqual(provider.refresh_count, 1)
            self.assertEqual(provider.asserted_refresh_token, "refresh-old")
            self.assertIn("access-refreshed", restarted.authorization_header())

    def test_external_token_update_is_observed_without_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clock = MutableClock()
            provider = RecordingAuthProvider()
            runtime = self.make_runtime(directory, clock, provider)
            runtime.install_tokens(
                OAuthTokenResponse(access_token="first-access", expires_in=300, scopes=("read",))
            )
            runtime.ensure_authorized()
            reference = runtime.credentials.reference(
                runtime.config.server_id,
                runtime.config.config_fingerprint,
                CredentialKind.OAUTH,
            )
            current = runtime.credentials.vault.metadata(reference)
            runtime.credentials.vault.put(
                reference,
                {"access_token": "externally-updated"},
                public_attributes={"token_type": "Bearer", "scopes": ["read"]},
                expires_at="2030-01-01T00:00:00Z",
                expected_revision=current.revision,
            )
            outcome = runtime.ensure_authorized()
            self.assertTrue(outcome.credential_changed)
            self.assertIn("externally-updated", runtime.authorization_header())
            self.assertNotIn("externally-updated", json.dumps(runtime.snapshot.safe_dict()))

    def test_invalid_grant_clears_local_token_and_marks_needs_auth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clock = MutableClock()
            provider = RecordingAuthProvider()
            provider.refresh_error = AuthNetworkError(code="invalid_grant", status=400)
            runtime = self.make_runtime(directory, clock, provider)
            runtime.set_metadata(metadata())
            runtime.install_tokens(
                OAuthTokenResponse(access_token="old", refresh_token="dead-refresh", expires_in=1)
            )
            clock.advance(5)
            outcome = runtime.refresh(force=True)
            self.assertEqual(outcome.status, AuthStatus.NEEDS_AUTH)
            self.assertEqual(outcome.error_kind, RefreshErrorKind.INVALID_GRANT)
            self.assertFalse(
                runtime.credentials.vault.exists(
                    runtime.credentials.reference(
                        runtime.config.server_id,
                        runtime.config.config_fingerprint,
                        CredentialKind.OAUTH,
                    )
                )
            )

    def test_step_up_records_scope_and_resource_without_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clock = MutableClock()
            provider = RecordingAuthProvider()
            runtime = self.make_runtime(directory, clock, provider)
            snapshot = runtime.require_step_up(("write",), "https://mcp.example.test")
            self.assertEqual(snapshot.status, AuthStatus.STEP_UP_REQUIRED)
            self.assertEqual(snapshot.step_up.scopes, ("write",))
            serialized = json.dumps(snapshot.safe_dict())
            self.assertNotIn("some-raw-access-token", serialized)
            self.assertIn('"access_token_digest": ""', serialized)

    def test_revoke_refresh_then_access_and_always_delete_local(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clock = MutableClock()
            provider = RecordingAuthProvider()
            provider.revoke_error_hints.add("refresh_token")
            runtime = self.make_runtime(directory, clock, provider)
            runtime.set_metadata(metadata())
            runtime.install_tokens(
                OAuthTokenResponse(access_token="access", refresh_token="refresh", expires_in=300)
            )
            outcome = runtime.revoke()
            self.assertEqual(provider.revoke_order, ["refresh_token", "access_token"])
            self.assertTrue(outcome.local_deleted)
            self.assertEqual(outcome.snapshot.status, AuthStatus.REVOKED)
            self.assertEqual(outcome.remote_failures, (str(RefreshErrorKind.TRANSIENT),))

    def test_state_events_and_diagnostics_do_not_leak_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clock = MutableClock()
            provider = RecordingAuthProvider()
            events: list[dict] = []
            runtime = self.make_runtime(directory, clock, provider, events=events)
            runtime.install_tokens(
                OAuthTokenResponse(
                    access_token="never-in-event-access",
                    refresh_token="never-in-event-refresh",
                    id_token="never-in-event-id",
                    expires_in=300,
                )
            )
            surfaces = json.dumps(
                {
                    "snapshot": runtime.snapshot.safe_dict(),
                    "diagnostics": runtime.safe_diagnostics(),
                    "events": events,
                },
                sort_keys=True,
            )
            for secret in ("never-in-event-access", "never-in-event-refresh", "never-in-event-id"):
                self.assertNotIn(secret, surfaces)
            self.assertIn("raw_token_included", surfaces)

    def test_xaa_https_and_issuer_resource_mismatch_are_fail_closed(self) -> None:
        with self.assertRaises(AuthConfigurationError):
            XaaConfiguration(
                issuer="http://identity.example.test",
                resource="https://mcp.example.test",
                client_id="client",
            )
        xaa = XaaConfiguration(
            issuer="https://identity.example.test",
            resource="https://mcp.example.test",
            client_id="client",
            token_endpoint="https://identity.example.test/token",
        )
        config = auth_config(mode=AuthMode.XAA, xaa=xaa)
        with tempfile.TemporaryDirectory() as directory:
            runtime = self.make_runtime(directory, MutableClock(), RecordingAuthProvider(), config=config)
            runtime.install_xaa_identity("identity-secret")
            with self.assertRaises(AuthConfigurationError):
                runtime.exchange_xaa(
                    discovered_issuer="https://attacker.example.test",
                    discovered_resource="https://mcp.example.test",
                )

    def test_xaa_state_is_separate_and_exchange_validates_response(self) -> None:
        xaa = XaaConfiguration(
            issuer="https://identity.example.test",
            resource="https://mcp.example.test",
            client_id="client",
            token_endpoint="https://identity.example.test/token",
        )
        config = auth_config(mode=AuthMode.XAA, xaa=xaa)
        with tempfile.TemporaryDirectory() as directory:
            provider = RecordingAuthProvider()
            runtime = self.make_runtime(directory, MutableClock(), provider, config=config)
            runtime.install_xaa_identity("identity-secret")
            outcome = runtime.exchange_xaa(
                discovered_issuer=xaa.issuer,
                discovered_resource=xaa.resource,
                discovered_token_endpoint=xaa.token_endpoint,
            )
            self.assertTrue(outcome.authorized)
            self.assertEqual(provider.asserted_id_token, "identity-secret")
            self.assertEqual(provider.exchange_count, 1)
            self.assertIn("xaa-access", runtime.authorization_header(kind=CredentialKind.XAA))

    def test_refresh_error_classification_is_structured(self) -> None:
        cases = {
            RefreshErrorKind.INVALID_GRANT: AuthNetworkError(code="expired_refresh_token", status=400),
            RefreshErrorKind.INVALID_CLIENT: AuthNetworkError(code="invalid_client", status=401),
            RefreshErrorKind.INSUFFICIENT_SCOPE: AuthNetworkError(code="insufficient_scope", status=403),
            RefreshErrorKind.TRANSIENT: AuthNetworkError(code="server_error", status=503),
        }
        for expected, error in cases.items():
            with self.subTest(expected=expected):
                self.assertEqual(classify_refresh_error(error), expected)


def sampling_request(
    *,
    request_id: str = "request-1",
    session_id: str = "session-1",
    max_tokens: int = 20,
    tool_round: int = 0,
    secret: str = "prompt-secret-never-audit",
) -> SamplingRequest:
    return SamplingRequest(
        server_id="server-a",
        session_id=session_id,
        request_id=request_id,
        messages=({"role": "user", "content": secret},),
        system_prompt="system-secret-never-audit",
        max_tokens=max_tokens,
        model_hint="model-a",
        tools=({"name": "tool-a", "inputSchema": {"type": "object"}},),
        tool_round=tool_round,
        metadata={"trace_id": "trace-1"},
    )


class SamplingRuntimeTests(unittest.TestCase):
    def test_sampling_is_default_deny_even_when_callback_exists(self) -> None:
        called = 0

        def callback(request: SamplingRequest, context) -> SamplingResponse:
            nonlocal called
            called += 1
            return SamplingResponse(content={"type": "text", "text": "x"}, model="model-a", token_count=1)

        runtime = McpSamplingRuntime(callback=callback)
        resolution = runtime.sample(sampling_request())
        self.assertEqual(resolution.decision, SamplingDecision.DENIED)
        self.assertEqual(resolution.reason, SamplingReason.DEFAULT_DENY)
        self.assertEqual(called, 0)
        self.assertFalse(runtime.advertised("server-a"))

    def test_enabled_policy_still_denies_without_explicit_callback(self) -> None:
        runtime = McpSamplingRuntime(SamplingPolicy(enabled=True))
        resolution = runtime.sample(sampling_request())
        self.assertEqual(resolution.reason, SamplingReason.CALLBACK_MISSING)
        self.assertFalse(runtime.advertised("server-a"))

    def test_request_is_capped_before_callback_and_usage_is_charged(self) -> None:
        observed: list[int] = []

        def callback(request: SamplingRequest, context) -> SamplingResponse:
            observed.append(request.max_tokens)
            self.assertEqual(context.effective_max_tokens, 10)
            return SamplingResponse(
                content={"type": "text", "text": "safe output"},
                model="model-a",
                token_count=7,
            )

        runtime = McpSamplingRuntime(
            SamplingPolicy(
                enabled=True,
                max_tokens_per_request=10,
                max_tokens_per_session=20,
                max_requests_per_minute=20,
            ),
            callback=callback,
        )
        resolution = runtime.sample(sampling_request(max_tokens=100))
        self.assertEqual(resolution.decision, SamplingDecision.COMPLETED)
        self.assertEqual(observed, [10])
        budget = runtime.session_snapshot("server-a", "session-1")
        self.assertEqual(budget.tokens_consumed, 7)
        self.assertEqual(budget.tokens_reserved, 0)
        self.assertEqual(budget.requests_completed, 1)

    def test_session_request_and_token_caps_change_execution(self) -> None:
        def callback(request: SamplingRequest, context) -> SamplingResponse:
            return SamplingResponse(
                content={"type": "text", "text": "ok"},
                model="model-a",
                token_count=context.effective_max_tokens,
            )

        runtime = McpSamplingRuntime(
            SamplingPolicy(
                enabled=True,
                max_requests_per_session=2,
                max_requests_per_minute=10,
                max_tokens_per_request=6,
                max_tokens_per_session=10,
            ),
            callback=callback,
        )
        first = runtime.sample(sampling_request(request_id="one", max_tokens=6))
        second = runtime.sample(sampling_request(request_id="two", max_tokens=6))
        third = runtime.sample(sampling_request(request_id="three", max_tokens=1))
        self.assertEqual(first.response.effective_token_count, 6)
        self.assertEqual(second.effective_max_tokens, 4)
        self.assertEqual(third.reason, SamplingReason.REQUEST_CAP)

    def test_provider_token_overrun_is_rejected_and_charged(self) -> None:
        def callback(request: SamplingRequest, context) -> SamplingResponse:
            return SamplingResponse(content={"type": "text", "text": "too much"}, model="model-a", token_count=11)

        runtime = McpSamplingRuntime(
            SamplingPolicy(
                enabled=True,
                max_tokens_per_request=10,
                max_tokens_per_session=20,
                max_requests_per_minute=20,
            ),
            callback=callback,
        )
        resolution = runtime.sample(sampling_request(max_tokens=10))
        self.assertEqual(resolution.reason, SamplingReason.PROVIDER_TOKEN_OVERRUN)
        self.assertIsNone(resolution.response)
        self.assertEqual(runtime.session_snapshot("server-a", "session-1").tokens_consumed, 11)

    def test_tool_round_caps_are_enforced_before_and_after_callback(self) -> None:
        def callback(request: SamplingRequest, context) -> SamplingResponse:
            return SamplingResponse(
                content={"type": "text", "text": "tool"},
                model="model-a",
                token_count=1,
                tool_rounds=3,
            )

        runtime = McpSamplingRuntime(
            SamplingPolicy(
                enabled=True,
                max_requests_per_minute=20,
                max_tool_rounds_per_request=2,
                max_tool_rounds_per_session=4,
            ),
            callback=callback,
        )
        preflight = runtime.sample(sampling_request(request_id="pre", tool_round=3))
        self.assertEqual(preflight.reason, SamplingReason.TOOL_ROUND_CAP)
        postflight = runtime.sample(sampling_request(request_id="post", tool_round=0))
        self.assertEqual(postflight.reason, SamplingReason.TOOL_ROUND_CAP)

    def test_timeout_sets_callback_cancellation_without_waiting_for_thread(self) -> None:
        observed_cancel = threading.Event()

        def callback(request: SamplingRequest, context) -> SamplingResponse:
            del request
            while not context.cancelled:
                time.sleep(0.005)
            observed_cancel.set()
            return SamplingResponse(content={"type": "text", "text": "late"}, model="model-a", token_count=1)

        runtime = McpSamplingRuntime(
            SamplingPolicy(
                enabled=True,
                timeout_seconds=0.05,
                cancellation_poll_seconds=0.005,
                max_requests_per_minute=20,
            ),
            callback=callback,
        )
        started = time.monotonic()
        resolution = runtime.sample(sampling_request())
        elapsed = time.monotonic() - started
        self.assertEqual(resolution.decision, SamplingDecision.TIMED_OUT)
        self.assertEqual(resolution.reason, SamplingReason.CALLBACK_TIMEOUT)
        self.assertLess(elapsed, 0.5)
        self.assertTrue(observed_cancel.wait(0.5))
        self.assertEqual(runtime.session_snapshot("server-a", "session-1").active_requests, 0)

    def test_external_cancel_and_close_session_cancel_active_request(self) -> None:
        entered = threading.Event()

        def callback(request: SamplingRequest, context) -> SamplingResponse:
            del request
            entered.set()
            while not context.cancelled:
                time.sleep(0.005)
            return SamplingResponse(content={"type": "text", "text": "cancelled"}, model="model-a", token_count=1)

        runtime = McpSamplingRuntime(
            SamplingPolicy(
                enabled=True,
                timeout_seconds=2,
                cancellation_poll_seconds=0.005,
                max_requests_per_minute=20,
            ),
            callback=callback,
        )
        holder: list = []
        thread = threading.Thread(target=lambda: holder.append(runtime.sample(sampling_request())), daemon=True)
        thread.start()
        self.assertTrue(entered.wait(1))
        runtime.close_session("server-a", "session-1")
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(holder[0].decision, SamplingDecision.CANCELLED)

    def test_audit_never_contains_prompt_output_tool_arguments_or_exception_text(self) -> None:
        events: list[dict] = []

        def callback(request: SamplingRequest, context) -> SamplingResponse:
            del request, context
            return SamplingResponse(
                content={"type": "text", "text": "response-secret-never-audit"},
                model="model-a",
                token_count=2,
                tool_calls=({"name": "tool-a", "arguments": {"password": "tool-secret"}},),
            )

        runtime = McpSamplingRuntime(
            SamplingPolicy(enabled=True, max_requests_per_minute=20),
            callback=callback,
            event_sink=events.append,
        )
        request = sampling_request(secret="prompt-secret-never-audit")
        resolution = runtime.sample(request)
        self.assertEqual(resolution.decision, SamplingDecision.COMPLETED)
        serialized = json.dumps({"events": events, "audit": resolution.audit.safe_dict()}, sort_keys=True)
        for secret in (
            "prompt-secret-never-audit",
            "system-secret-never-audit",
            "response-secret-never-audit",
            "tool-secret",
        ):
            self.assertNotIn(secret, serialized)
        self.assertIn(request.content_digest, serialized)
        self.assertIn(resolution.response.content_digest, serialized)

    def test_callback_exception_is_classified_without_exception_message(self) -> None:
        events: list[dict] = []

        def callback(request: SamplingRequest, context) -> SamplingResponse:
            del request, context
            raise RuntimeError("provider leaked secret exception")

        runtime = McpSamplingRuntime(
            SamplingPolicy(enabled=True, max_requests_per_minute=20),
            callback=callback,
            event_sink=events.append,
        )
        resolution = runtime.sample(sampling_request())
        self.assertEqual(resolution.decision, SamplingDecision.ERROR)
        serialized = json.dumps(events)
        self.assertNotIn("provider leaked secret exception", serialized)
        self.assertIn("runtimeerror", serialized.casefold())


if __name__ == "__main__":
    unittest.main()
