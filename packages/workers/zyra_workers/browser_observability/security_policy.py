from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit

from .models import digest_value, utc_now


class SecurityDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    AUDIT = "audit"


class SecurityReason(StrEnum):
    ALLOWED = "allowed"
    INVALID_URL = "invalid_url"
    FORBIDDEN_SCHEME = "forbidden_scheme"
    USERINFO_PRESENT = "userinfo_present"
    HOST_MISSING = "host_missing"
    HOST_DENIED = "host_denied"
    HOST_NOT_ALLOWED = "host_not_allowed"
    PRIVATE_ADDRESS = "private_address"
    LOOPBACK_ADDRESS = "loopback_address"
    LINK_LOCAL_ADDRESS = "link_local_address"
    MULTICAST_ADDRESS = "multicast_address"
    UNSPECIFIED_ADDRESS = "unspecified_address"
    DNS_FAILED = "dns_failed"
    DNS_REBIND = "dns_rebind"
    PORT_DENIED = "port_denied"
    REDIRECT_LIMIT = "redirect_limit"
    DOWNGRADE_REDIRECT = "downgrade_redirect"
    ORIGIN_CHANGED = "origin_changed"


@dataclass(frozen=True, slots=True)
class SecurityPolicy:
    allowed_schemes: frozenset[str] = frozenset({"http", "https"})
    allowed_hosts: frozenset[str] = frozenset()
    denied_hosts: frozenset[str] = frozenset()
    denied_ports: frozenset[int] = frozenset({0, 25, 110, 143, 445, 3306, 5432, 6379})
    allow_private_network: bool = False
    allow_loopback: bool = False
    allow_link_local: bool = False
    require_dns_resolution: bool = True
    pin_dns_for_chain: bool = True
    allow_https_downgrade: bool = False
    max_redirects: int = 12
    audit_cross_origin: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "allowed_schemes",
            frozenset(item.casefold() for item in self.allowed_schemes),
        )
        object.__setattr__(
            self,
            "allowed_hosts",
            frozenset(_normalize_host_pattern(item) for item in self.allowed_hosts),
        )
        object.__setattr__(
            self,
            "denied_hosts",
            frozenset(_normalize_host_pattern(item) for item in self.denied_hosts),
        )
        if self.max_redirects < 0:
            raise ValueError("max_redirects must be non-negative")
        if any(port < 0 or port > 65535 for port in self.denied_ports):
            raise ValueError("denied port outside valid range")


@dataclass(frozen=True, slots=True)
class UrlIdentity:
    raw: str
    canonical: str
    scheme: str
    host: str
    port: int
    path: str
    query: str
    origin: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "canonical": self.canonical,
            "scheme": self.scheme,
            "host": self.host,
            "port": self.port,
            "path": self.path,
            "query": self.query,
            "origin": self.origin,
        }


@dataclass(frozen=True, slots=True)
class AddressEvidence:
    host: str
    addresses: tuple[str, ...]
    resolved_at: str = field(default_factory=utc_now)
    resolver: str = "socket.getaddrinfo"

    @property
    def digest(self) -> str:
        return digest_value(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "addresses": list(self.addresses),
            "resolved_at": self.resolved_at,
            "resolver": self.resolver,
        }


@dataclass(frozen=True, slots=True)
class SecurityVerdict:
    decision: SecurityDecision
    reason: SecurityReason
    summary: str
    url: UrlIdentity | None = None
    address_evidence: AddressEvidence | None = None
    redirect_index: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.decision != SecurityDecision.DENY

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": str(self.decision),
            "reason": str(self.reason),
            "summary": self.summary,
            "url": self.url.to_dict() if self.url else None,
            "address_evidence": (
                self.address_evidence.to_dict()
                if self.address_evidence
                else None
            ),
            "redirect_index": self.redirect_index,
            "metadata": dict(self.metadata),
        }


class BrowserSecurityPolicyEngine:
    """Deterministic URL/DNS/redirect guard for watchdog observations."""

    def __init__(
        self,
        *,
        policy: SecurityPolicy | None = None,
        resolver: Callable[[str], Sequence[str]] | None = None,
    ) -> None:
        self.policy = policy or SecurityPolicy()
        self.resolver = resolver or self._resolve_host
        self._pinned: dict[str, frozenset[str]] = {}

    def evaluate(
        self,
        raw_url: str,
        *,
        redirect_index: int = 0,
        previous_url: str = "",
        chain_id: str = "",
    ) -> SecurityVerdict:
        try:
            identity = canonicalize_url(raw_url)
        except ValueError as error:
            return SecurityVerdict(
                SecurityDecision.DENY,
                SecurityReason.INVALID_URL,
                str(error),
                redirect_index=redirect_index,
            )
        if identity.scheme not in self.policy.allowed_schemes:
            return self._deny(
                identity,
                SecurityReason.FORBIDDEN_SCHEME,
                f"URL scheme {identity.scheme!r} is not allowed.",
                redirect_index,
            )
        parsed = urlsplit(raw_url)
        if parsed.username or parsed.password:
            return self._deny(
                identity,
                SecurityReason.USERINFO_PRESENT,
                "URL userinfo is prohibited.",
                redirect_index,
            )
        if _matches_any(identity.host, self.policy.denied_hosts):
            return self._deny(
                identity,
                SecurityReason.HOST_DENIED,
                "URL host matches the deny list.",
                redirect_index,
            )
        if self.policy.allowed_hosts and not _matches_any(
            identity.host,
            self.policy.allowed_hosts,
        ):
            return self._deny(
                identity,
                SecurityReason.HOST_NOT_ALLOWED,
                "URL host is outside the allow list.",
                redirect_index,
            )
        if identity.port in self.policy.denied_ports:
            return self._deny(
                identity,
                SecurityReason.PORT_DENIED,
                f"URL port {identity.port} is denied.",
                redirect_index,
            )
        if redirect_index > self.policy.max_redirects:
            return self._deny(
                identity,
                SecurityReason.REDIRECT_LIMIT,
                "Redirect chain exceeded its deterministic limit.",
                redirect_index,
            )
        if previous_url:
            try:
                previous = canonicalize_url(previous_url)
            except ValueError:
                previous = None
            if (
                previous
                and previous.scheme == "https"
                and identity.scheme == "http"
                and not self.policy.allow_https_downgrade
            ):
                return self._deny(
                    identity,
                    SecurityReason.DOWNGRADE_REDIRECT,
                    "HTTPS to HTTP redirect is prohibited.",
                    redirect_index,
                )
        try:
            addresses = tuple(dict.fromkeys(self.resolver(identity.host)))
        except OSError as error:
            if self.policy.require_dns_resolution:
                return self._deny(
                    identity,
                    SecurityReason.DNS_FAILED,
                    f"DNS resolution failed: {error}",
                    redirect_index,
                )
            addresses = ()
        evidence = AddressEvidence(identity.host, addresses)
        address_verdict = self._evaluate_addresses(
            identity,
            evidence,
            redirect_index,
        )
        if address_verdict is not None:
            return address_verdict
        if chain_id and self.policy.pin_dns_for_chain:
            pinned = self._pinned.get(chain_id)
            current = frozenset(addresses)
            if pinned is None:
                self._pinned[chain_id] = current
            elif current and pinned and current.isdisjoint(pinned):
                return SecurityVerdict(
                    SecurityDecision.DENY,
                    SecurityReason.DNS_REBIND,
                    "Redirect chain DNS addresses changed without overlap.",
                    identity,
                    evidence,
                    redirect_index,
                    {
                        "chain_id": chain_id,
                        "pinned_addresses": sorted(pinned),
                    },
                )
        cross_origin = False
        previous_origin = ""
        if previous_url:
            try:
                previous_origin = canonicalize_url(previous_url).origin
                cross_origin = previous_origin != identity.origin
            except ValueError:
                pass
        decision = (
            SecurityDecision.AUDIT
            if cross_origin and self.policy.audit_cross_origin
            else SecurityDecision.ALLOW
        )
        reason = (
            SecurityReason.ORIGIN_CHANGED
            if cross_origin
            else SecurityReason.ALLOWED
        )
        return SecurityVerdict(
            decision,
            reason,
            (
                "Cross-origin redirect allowed with an audit signal."
                if cross_origin
                else "URL passed deterministic security policy."
            ),
            identity,
            evidence,
            redirect_index,
            {
                "previous_origin": previous_origin,
                "cross_origin": cross_origin,
                "chain_id": chain_id,
            },
        )

    def release_chain(
        self,
        chain_id: str,
    ) -> None:
        self._pinned.pop(chain_id, None)

    def snapshot(self) -> dict[str, Any]:
        return {
            "allowed_schemes": sorted(self.policy.allowed_schemes),
            "allowed_hosts": sorted(self.policy.allowed_hosts),
            "denied_hosts": sorted(self.policy.denied_hosts),
            "denied_ports": sorted(self.policy.denied_ports),
            "allow_private_network": self.policy.allow_private_network,
            "allow_loopback": self.policy.allow_loopback,
            "allow_link_local": self.policy.allow_link_local,
            "pinned_chains": {
                key: sorted(value)
                for key, value in self._pinned.items()
            },
        }

    def _evaluate_addresses(
        self,
        identity: UrlIdentity,
        evidence: AddressEvidence,
        redirect_index: int,
    ) -> SecurityVerdict | None:
        for raw in evidence.addresses:
            try:
                address = ipaddress.ip_address(raw)
            except ValueError:
                return SecurityVerdict(
                    SecurityDecision.DENY,
                    SecurityReason.DNS_FAILED,
                    "Resolver returned an invalid IP address.",
                    identity,
                    evidence,
                    redirect_index,
                    {"address": raw},
                )
            if address.is_loopback and not self.policy.allow_loopback:
                return SecurityVerdict(
                    SecurityDecision.DENY,
                    SecurityReason.LOOPBACK_ADDRESS,
                    "Loopback browser destinations are prohibited.",
                    identity,
                    evidence,
                    redirect_index,
                    {"address": raw},
                )
            if address.is_link_local and not self.policy.allow_link_local:
                return SecurityVerdict(
                    SecurityDecision.DENY,
                    SecurityReason.LINK_LOCAL_ADDRESS,
                    "Link-local browser destinations are prohibited.",
                    identity,
                    evidence,
                    redirect_index,
                    {"address": raw},
                )
            if address.is_private and not self.policy.allow_private_network:
                return SecurityVerdict(
                    SecurityDecision.DENY,
                    SecurityReason.PRIVATE_ADDRESS,
                    "Private-network browser destinations are prohibited.",
                    identity,
                    evidence,
                    redirect_index,
                    {"address": raw},
                )
            if address.is_multicast:
                return SecurityVerdict(
                    SecurityDecision.DENY,
                    SecurityReason.MULTICAST_ADDRESS,
                    "Multicast browser destinations are prohibited.",
                    identity,
                    evidence,
                    redirect_index,
                    {"address": raw},
                )
            if address.is_unspecified:
                return SecurityVerdict(
                    SecurityDecision.DENY,
                    SecurityReason.UNSPECIFIED_ADDRESS,
                    "Unspecified browser destinations are prohibited.",
                    identity,
                    evidence,
                    redirect_index,
                    {"address": raw},
                )
        return None

    @staticmethod
    def _deny(
        identity: UrlIdentity,
        reason: SecurityReason,
        summary: str,
        redirect_index: int,
    ) -> SecurityVerdict:
        return SecurityVerdict(
            SecurityDecision.DENY,
            reason,
            summary,
            identity,
            redirect_index=redirect_index,
        )

    @staticmethod
    def _resolve_host(
        host: str,
    ) -> tuple[str, ...]:
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            return (str(literal),)
        values = socket.getaddrinfo(
            host,
            None,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
        return tuple(
            dict.fromkeys(
                str(item[4][0])
                for item in values
                if item[4]
            )
        )


def canonicalize_url(
    raw_url: str,
) -> UrlIdentity:
    raw = str(raw_url or "").strip()
    if not raw:
        raise ValueError("URL must not be empty")
    try:
        parsed = urlsplit(raw)
    except ValueError as error:
        raise ValueError(f"invalid URL: {error}") from error
    scheme = parsed.scheme.casefold()
    host = str(parsed.hostname or "").rstrip(".").casefold()
    if not scheme:
        raise ValueError("URL scheme is missing")
    if not host:
        raise ValueError("URL host is missing")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise ValueError("URL host cannot be normalized") from error
    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError as error:
        raise ValueError("URL port is invalid") from error
    path = parsed.path or "/"
    canonical_netloc = host
    default_port = 443 if scheme == "https" else 80
    if port != default_port:
        canonical_netloc = f"{host}:{port}"
    canonical = urlunsplit(
        SplitResult(
            scheme=scheme,
            netloc=canonical_netloc,
            path=path,
            query=parsed.query,
            fragment="",
        )
    )
    return UrlIdentity(
        raw=raw,
        canonical=canonical,
        scheme=scheme,
        host=host,
        port=port,
        path=path,
        query=parsed.query,
        origin=f"{scheme}://{canonical_netloc}",
    )


def _normalize_host_pattern(
    value: str,
) -> str:
    normalized = str(value or "").strip().rstrip(".").casefold()
    if not normalized:
        raise ValueError("host pattern must not be empty")
    if normalized.startswith("*."):
        suffix = normalized[2:].encode("idna").decode("ascii")
        return f"*.{suffix}"
    return normalized.encode("idna").decode("ascii")


def _matches_any(
    host: str,
    patterns: Iterable[str],
) -> bool:
    for pattern in patterns:
        if pattern.startswith("*."):
            suffix = pattern[1:]
            if host.endswith(suffix) and host != pattern[2:]:
                return True
        elif host == pattern:
            return True
    return False
