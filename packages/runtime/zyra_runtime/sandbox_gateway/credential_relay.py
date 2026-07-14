from __future__ import annotations

import threading
import time
from dataclasses import replace
from typing import Callable, Mapping, Protocol

from .canonical import random_nonce, stable_id, token_digest
from .errors import GatewayErrorCode, SandboxGatewayError
from .models import CredentialEnvelope, CredentialRequest


class CredentialProvider(Protocol):
    def resolve(self, request: CredentialRequest) -> str:
        ...


class CallbackCredentialProvider:
    def __init__(self, callback: Callable[[CredentialRequest], str]) -> None:
        self.callback = callback

    def resolve(self, request: CredentialRequest) -> str:
        return str(self.callback(request))


class CredentialRelay:
    """Credential-free command envelopes with audience-bound ephemeral injection."""

    def __init__(
        self,
        provider: CredentialProvider,
        *,
        maximum_ttl_seconds: float = 300.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.provider = provider
        self.maximum_ttl_seconds = float(maximum_ttl_seconds)
        self.clock = clock
        self._secrets: dict[str, str] = {}
        self._envelopes: dict[str, CredentialEnvelope] = {}
        self._lock = threading.RLock()

    def issue(self, request: CredentialRequest) -> CredentialEnvelope:
        if request.ttl_seconds > self.maximum_ttl_seconds:
            raise SandboxGatewayError(
                GatewayErrorCode.CREDENTIAL_DENIED,
                "credential request exceeds deployment TTL",
                operation="credential_issue",
            )
        try:
            secret = self.provider.resolve(request)
        except Exception as error:
            raise SandboxGatewayError(
                GatewayErrorCode.CREDENTIAL_DENIED,
                f"credential provider failed: {type(error).__name__}",
                operation="credential_issue",
                retryable=True,
            ) from error
        if not secret:
            raise SandboxGatewayError(
                GatewayErrorCode.CREDENTIAL_DENIED,
                "credential provider returned empty material",
                operation="credential_issue",
            )
        now = self.clock()
        nonce = random_nonce()
        envelope = CredentialEnvelope(
            envelope_id=stable_id(
                "credential-envelope",
                request.request_id,
                request.session_id,
                request.command_id,
                request.audience,
                request.scope,
                token_digest(secret),
                nonce,
            ),
            request_id=request.request_id,
            session_id=request.session_id,
            command_id=request.command_id,
            audience=request.audience,
            scope=request.scope,
            secret_digest=token_digest(secret),
            issued_at=now,
            expires_at=now + request.ttl_seconds,
            nonce=nonce,
            metadata={
                "provider": request.provider,
                "credential_name": request.credential_name,
                "provenance_ref": request.provenance_ref,
            },
        )
        with self._lock:
            self._secrets[envelope.envelope_id] = secret
            self._envelopes[envelope.envelope_id] = envelope
        return envelope

    def consume(
        self,
        envelope_id: str,
        *,
        session_id: str,
        command_id: str,
        audience: str,
        required_scope: str = "",
    ) -> str:
        with self._lock:
            envelope = self._envelopes.get(envelope_id)
            secret = self._secrets.get(envelope_id)
            if envelope is None or secret is None:
                raise SandboxGatewayError(
                    GatewayErrorCode.CREDENTIAL_REPLAY,
                    "credential envelope is absent, consumed, or lost across restart",
                    operation="credential_consume",
                )
            if envelope.consumed:
                raise SandboxGatewayError(
                    GatewayErrorCode.CREDENTIAL_REPLAY,
                    "credential envelope has already been consumed",
                    operation="credential_consume",
                )
            if self.clock() >= envelope.expires_at:
                self._secrets.pop(envelope_id, None)
                raise SandboxGatewayError(
                    GatewayErrorCode.CREDENTIAL_DENIED,
                    "credential envelope expired",
                    operation="credential_consume",
                )
            if (
                envelope.session_id != session_id
                or envelope.command_id != command_id
                or envelope.audience != audience
            ):
                raise SandboxGatewayError(
                    GatewayErrorCode.CREDENTIAL_DENIED,
                    "credential envelope audience or command binding mismatch",
                    operation="credential_consume",
                )
            if required_scope and required_scope not in envelope.scope:
                raise SandboxGatewayError(
                    GatewayErrorCode.CREDENTIAL_DENIED,
                    "credential envelope lacks the required scope",
                    operation="credential_consume",
                )
            consumed = replace(envelope, consumed_at=self.clock())
            self._envelopes[envelope_id] = consumed
            self._secrets.pop(envelope_id, None)
            return secret

    def revoke(self, envelope_id: str) -> bool:
        with self._lock:
            existed = envelope_id in self._envelopes
            self._secrets.pop(envelope_id, None)
            self._envelopes.pop(envelope_id, None)
            return existed

    def descriptor(self) -> Mapping[str, object]:
        with self._lock:
            return {
                "provider": type(self.provider).__name__,
                "maximum_ttl_seconds": self.maximum_ttl_seconds,
                "active_envelopes": len(self._secrets),
                "durable_secret_storage": False,
                "restart_behavior": "fail_closed",
                "single_use": True,
            }
