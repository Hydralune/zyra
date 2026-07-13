from __future__ import annotations

import ipaddress
import re
import socket
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import SplitResult, urlsplit, urlunsplit

from .models import digest_value, stable_id


class NetworkPolicyError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.details = dict(details or {})
        super().__init__(message)


class AddressClass(StrEnum):
    PUBLIC = "public"
    LOOPBACK = "loopback"
    PRIVATE = "private"
    LINK_LOCAL = "link_local"
    MULTICAST = "multicast"
    RESERVED = "reserved"
    UNSPECIFIED = "unspecified"
    DOCUMENTATION = "documentation"
    BENCHMARK = "benchmark"
    CARRIER_GRADE_NAT = "carrier_grade_nat"
    METADATA = "metadata"


class DomainPatternKind(StrEnum):
    EXACT_HOST = "exact_host"
    SUBDOMAIN = "subdomain"
    EXACT_ORIGIN = "exact_origin"


@dataclass(frozen=True, slots=True)
class CanonicalUrl:
    raw: str
    url: str
    scheme: str
    host: str
    port: int
    origin: str
    path: str
    query: str
    fragment: str
    username_present: bool
    password_present: bool
    literal_ip: str = ""

    @property
    def host_port(self) -> str:
        default = 443 if self.scheme == "https" else 80 if self.scheme == "http" else None
        if self.port and self.port != default:
            host = f"[{self.host}]" if ":" in self.host else self.host
            return f"{host}:{self.port}"
        return f"[{self.host}]" if ":" in self.host else self.host

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "scheme": self.scheme,
            "host": self.host,
            "port": self.port,
            "origin": self.origin,
            "path": self.path,
            "query": self.query,
            "fragment": self.fragment,
            "username_present": self.username_present,
            "password_present": self.password_present,
            "literal_ip": self.literal_ip,
        }


@dataclass(frozen=True, slots=True)
class DomainPattern:
    raw: str
    kind: DomainPatternKind
    host: str
    scheme: str = ""
    port: int = 0

    def matches(self, url: CanonicalUrl) -> bool:
        if self.scheme and self.scheme != url.scheme:
            return False
        if self.port and self.port != url.port:
            return False
        if self.kind == DomainPatternKind.SUBDOMAIN:
            return url.host.endswith("." + self.host) and url.host != self.host
        return url.host == self.host


@dataclass(frozen=True, slots=True)
class ResolvedAddress:
    address: str
    family: str
    classification: AddressClass

    @property
    def public(self) -> bool:
        return self.classification == AddressClass.PUBLIC

    def to_dict(self) -> dict[str, str]:
        return {
            "address": self.address,
            "family": self.family,
            "classification": str(self.classification),
        }


@dataclass(frozen=True, slots=True)
class DnsResolution:
    host: str
    epoch: int
    addresses: tuple[ResolvedAddress, ...]
    aliases: tuple[str, ...] = ()
    resolver_id: str = ""

    @property
    def digest(self) -> str:
        return digest_value(self.to_dict())

    @property
    def address_set(self) -> frozenset[str]:
        return frozenset(item.address for item in self.addresses)

    @property
    def safe_public_only(self) -> bool:
        return bool(self.addresses) and all(item.public for item in self.addresses)

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "epoch": self.epoch,
            "addresses": [item.to_dict() for item in self.addresses],
            "aliases": list(self.aliases),
            "resolver_id": self.resolver_id,
        }


class HostResolver(Protocol):
    @property
    def resolver_id(self) -> str: ...

    def resolve(self, host: str) -> DnsResolution: ...


class SystemHostResolver:
    """Bounded system resolver used only after static domain admission."""

    def __init__(self, *, resolver_id: str = "system-getaddrinfo") -> None:
        self._resolver_id = resolver_id
        self._lock = threading.Lock()
        self._epoch = 0

    @property
    def resolver_id(self) -> str:
        return self._resolver_id

    def resolve(self, host: str) -> DnsResolution:
        canonical = canonical_host(host)
        with self._lock:
            self._epoch += 1
            epoch = self._epoch
        try:
            records = socket.getaddrinfo(canonical, None, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise NetworkPolicyError("dns_resolution_failed", f"DNS resolution failed for {canonical}: {exc}") from exc
        addresses: dict[str, ResolvedAddress] = {}
        for family, _socktype, _proto, _canonname, sockaddr in records:
            raw = str(sockaddr[0])
            parsed = ipaddress.ip_address(raw)
            addresses[str(parsed)] = ResolvedAddress(
                address=str(parsed),
                family="ipv6" if family == socket.AF_INET6 else "ipv4",
                classification=classify_address(parsed),
            )
        return DnsResolution(canonical, epoch, tuple(addresses.values()), resolver_id=self.resolver_id)


class StaticHostResolver:
    """Hermetic resolver with monotonic epochs for deterministic policy tests."""

    def __init__(self, records: Mapping[str, Sequence[str] | Sequence[Sequence[str]]]) -> None:
        self._records = {canonical_host(key): value for key, value in records.items()}
        self._counts: dict[str, int] = {}
        self._epoch = 0
        self._lock = threading.Lock()

    @property
    def resolver_id(self) -> str:
        return "zyra-static-resolver"

    def resolve(self, host: str) -> DnsResolution:
        canonical = canonical_host(host)
        with self._lock:
            self._epoch += 1
            count = self._counts.get(canonical, 0)
            self._counts[canonical] = count + 1
            epoch = self._epoch
        configured = self._records.get(canonical)
        if configured is None:
            raise NetworkPolicyError("dns_resolution_failed", f"no hermetic DNS record for {canonical}")
        selected: Sequence[str]
        if configured and isinstance(configured[0], Sequence) and not isinstance(configured[0], str):  # type: ignore[index]
            history = configured  # type: ignore[assignment]
            selected = history[min(count, len(history) - 1)]  # type: ignore[index]
        else:
            selected = configured  # type: ignore[assignment]
        addresses = tuple(resolved_address(value) for value in selected)
        return DnsResolution(canonical, epoch, addresses, resolver_id=self.resolver_id)

    def count(self, host: str) -> int:
        return self._counts.get(canonical_host(host), 0)


@dataclass(frozen=True, slots=True)
class NetworkPolicyConfig:
    allowed_patterns: tuple[DomainPattern, ...] = ()
    denied_patterns: tuple[DomainPattern, ...] = ()
    allowed_schemes: frozenset[str] = frozenset({"https", "http"})
    allow_loopback: bool = False
    allow_private: bool = False
    allow_link_local: bool = False
    allow_literal_ip: bool = False
    allow_url_credentials: bool = False
    require_allowlist: bool = False
    max_redirects: int = 10

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_patterns", tuple(self.allowed_patterns))
        object.__setattr__(self, "denied_patterns", tuple(self.denied_patterns))
        object.__setattr__(self, "allowed_schemes", frozenset(str(item).lower() for item in self.allowed_schemes))
        if self.max_redirects < 0 or self.max_redirects > 32:
            raise ValueError("browser network max_redirects must be between 0 and 32")

    @property
    def digest(self) -> str:
        return digest_value(
            {
                "allowed": [pattern.raw for pattern in self.allowed_patterns],
                "denied": [pattern.raw for pattern in self.denied_patterns],
                "schemes": sorted(self.allowed_schemes),
                "loopback": self.allow_loopback,
                "private": self.allow_private,
                "link_local": self.allow_link_local,
                "literal_ip": self.allow_literal_ip,
                "credentials": self.allow_url_credentials,
                "require_allowlist": self.require_allowlist,
                "max_redirects": self.max_redirects,
            }
        )


@dataclass(frozen=True, slots=True)
class NetworkReceipt:
    receipt_id: str
    policy_digest: str
    action_id: str
    canonical_url: CanonicalUrl
    resolution: DnsResolution
    redirect_depth: int = 0
    parent_receipt_id: str = ""
    creator_origin: str = ""

    @property
    def binding_digest(self) -> str:
        return digest_value(
            {
                "policy": self.policy_digest,
                "action": self.action_id,
                "url": self.canonical_url.to_dict(),
                "dns": self.resolution.to_dict(),
                "redirect_depth": self.redirect_depth,
                "parent": self.parent_receipt_id,
                "creator_origin": self.creator_origin,
            }
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "policy_digest": self.policy_digest,
            "action_id": self.action_id,
            "canonical_url": self.canonical_url.to_dict(),
            "resolution": self.resolution.to_dict(),
            "redirect_depth": self.redirect_depth,
            "parent_receipt_id": self.parent_receipt_id,
            "creator_origin": self.creator_origin,
            "binding_digest": self.binding_digest,
        }


class BrowserNetworkPolicy:
    def __init__(self, config: NetworkPolicyConfig, resolver: HostResolver) -> None:
        self.config = config
        self.resolver = resolver

    def preflight(self, *, action_id: str, raw_url: str, creator_origin: str = "") -> NetworkReceipt:
        url = canonicalize_url(raw_url)
        self._check_static(url, creator_origin=creator_origin)
        resolution = self._resolve(url)
        self._check_resolution(resolution)
        return self._receipt(action_id, url, resolution, creator_origin=creator_origin)

    def revalidate(self, receipt: NetworkReceipt) -> NetworkReceipt:
        if receipt.policy_digest != self.config.digest:
            raise NetworkPolicyError("network_policy_changed", "network policy changed after approval")
        current = self._resolve(receipt.canonical_url)
        self._check_resolution(current)
        if current.address_set != receipt.resolution.address_set:
            raise NetworkPolicyError(
                "dns_rebinding",
                "resolved address set changed after browser action approval",
                details={
                    "approved": sorted(receipt.resolution.address_set),
                    "current": sorted(current.address_set),
                    "approved_epoch": receipt.resolution.epoch,
                    "current_epoch": current.epoch,
                },
            )
        return self._receipt(
            receipt.action_id,
            receipt.canonical_url,
            current,
            redirect_depth=receipt.redirect_depth,
            parent_receipt_id=receipt.parent_receipt_id,
            creator_origin=receipt.creator_origin,
        )

    def redirect(self, parent: NetworkReceipt, target_url: str) -> NetworkReceipt:
        depth = parent.redirect_depth + 1
        if depth > self.config.max_redirects:
            raise NetworkPolicyError("too_many_redirects", "browser action redirect limit exceeded")
        target = canonicalize_url(target_url)
        self._check_static(target, creator_origin=parent.canonical_url.origin)
        resolution = self._resolve(target)
        self._check_resolution(resolution)
        return self._receipt(
            parent.action_id,
            target,
            resolution,
            redirect_depth=depth,
            parent_receipt_id=parent.receipt_id,
            creator_origin=parent.canonical_url.origin,
        )

    def new_target(self, parent: NetworkReceipt, target_url: str) -> NetworkReceipt:
        target = canonicalize_url(target_url)
        self._check_static(target, creator_origin=parent.canonical_url.origin)
        resolution = self._resolve(target)
        self._check_resolution(resolution)
        return self._receipt(
            parent.action_id,
            target,
            resolution,
            redirect_depth=parent.redirect_depth,
            parent_receipt_id=parent.receipt_id,
            creator_origin=parent.canonical_url.origin,
        )

    def _check_static(self, url: CanonicalUrl, *, creator_origin: str) -> None:
        if url.scheme not in self.config.allowed_schemes:
            raise NetworkPolicyError("scheme_denied", f"URL scheme {url.scheme!r} is not allowed")
        if (url.username_present or url.password_present) and not self.config.allow_url_credentials:
            raise NetworkPolicyError("url_credentials_denied", "URL credentials are prohibited")
        if url.literal_ip and not self.config.allow_literal_ip:
            raise NetworkPolicyError("literal_ip_denied", "literal IP navigation is prohibited")
        if any(pattern.matches(url) for pattern in self.config.denied_patterns):
            raise NetworkPolicyError("domain_denied", f"destination host {url.host!r} is denied")
        if self.config.require_allowlist and not any(pattern.matches(url) for pattern in self.config.allowed_patterns):
            raise NetworkPolicyError("domain_not_allowed", f"destination host {url.host!r} is outside the allowlist")
        if url.scheme in {"blob", "data"} and creator_origin != url.origin:
            raise NetworkPolicyError("opaque_origin_denied", "opaque URL does not match its creator origin")

    def _resolve(self, url: CanonicalUrl) -> DnsResolution:
        if url.literal_ip:
            address = resolved_address(url.literal_ip)
            return DnsResolution(url.host, 0, (address,), resolver_id="literal-ip")
        return self.resolver.resolve(url.host)

    def _check_resolution(self, resolution: DnsResolution) -> None:
        if not resolution.addresses:
            raise NetworkPolicyError("dns_empty", "DNS resolution returned no addresses")
        denied: list[ResolvedAddress] = []
        for address in resolution.addresses:
            if address.classification == AddressClass.PUBLIC:
                continue
            if address.classification == AddressClass.LOOPBACK and self.config.allow_loopback:
                continue
            if address.classification == AddressClass.PRIVATE and self.config.allow_private:
                continue
            if address.classification == AddressClass.LINK_LOCAL and self.config.allow_link_local:
                continue
            denied.append(address)
        if denied:
            raise NetworkPolicyError(
                "resolved_ip_denied",
                "destination resolves to a prohibited network range",
                details={"addresses": [item.to_dict() for item in denied]},
            )

    def _receipt(
        self,
        action_id: str,
        url: CanonicalUrl,
        resolution: DnsResolution,
        *,
        redirect_depth: int = 0,
        parent_receipt_id: str = "",
        creator_origin: str = "",
    ) -> NetworkReceipt:
        receipt_id = stable_id(
            "brnetwork",
            action_id,
            self.config.digest,
            url.url,
            sorted(resolution.address_set),
            resolution.aliases,
            resolution.resolver_id,
            redirect_depth,
            parent_receipt_id,
        )
        return NetworkReceipt(
            receipt_id=receipt_id,
            policy_digest=self.config.digest,
            action_id=action_id,
            canonical_url=url,
            resolution=resolution,
            redirect_depth=redirect_depth,
            parent_receipt_id=parent_receipt_id,
            creator_origin=creator_origin,
        )


def canonical_host(value: str) -> str:
    candidate = str(value).strip().rstrip(".")
    if not candidate:
        raise NetworkPolicyError("invalid_host", "URL host is empty")
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        pass
    try:
        ascii_host = candidate.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise NetworkPolicyError("invalid_idna", f"URL host is not valid IDNA: {candidate!r}") from exc
    if len(ascii_host) > 253 or any(len(label) > 63 for label in ascii_host.split(".")):
        raise NetworkPolicyError("invalid_host", "URL host exceeds DNS length limits")
    if not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label) for label in ascii_host.split(".")):
        raise NetworkPolicyError("invalid_host", f"URL host contains an invalid label: {candidate!r}")
    return ascii_host


def canonicalize_url(raw_url: str) -> CanonicalUrl:
    raw = str(raw_url).strip()
    if not raw or any(ord(char) < 32 for char in raw):
        raise NetworkPolicyError("invalid_url", "URL is empty or contains control characters")
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise NetworkPolicyError("invalid_url", f"URL cannot be parsed: {exc}") from exc
    scheme = parsed.scheme.lower()
    if not scheme or not parsed.hostname:
        raise NetworkPolicyError("absolute_url_required", "browser action requires an absolute URL")
    host = canonical_host(parsed.hostname)
    try:
        port = parsed.port or (443 if scheme == "https" else 80 if scheme == "http" else 0)
    except ValueError as exc:
        raise NetworkPolicyError("invalid_port", "URL port is invalid") from exc
    if not 0 <= port <= 65535:
        raise NetworkPolicyError("invalid_port", "URL port is outside the valid range")
    default = 443 if scheme == "https" else 80 if scheme == "http" else None
    host_text = f"[{host}]" if ":" in host else host
    authority = host_text if default == port or not port else f"{host_text}:{port}"
    path = parsed.path or "/"
    canonical = urlunsplit(SplitResult(scheme, authority, path, parsed.query, parsed.fragment))
    origin = f"{scheme}://{authority}"
    literal_ip = ""
    try:
        literal_ip = str(ipaddress.ip_address(host))
    except ValueError:
        pass
    return CanonicalUrl(
        raw=raw,
        url=canonical,
        scheme=scheme,
        host=host,
        port=port,
        origin=origin,
        path=path,
        query=parsed.query,
        fragment=parsed.fragment,
        username_present=parsed.username is not None,
        password_present=parsed.password is not None,
        literal_ip=literal_ip,
    )


def parse_domain_pattern(raw_pattern: str) -> DomainPattern:
    raw = str(raw_pattern).strip()
    if not raw:
        raise NetworkPolicyError("invalid_domain_pattern", "domain pattern is empty")
    if raw.startswith("*."):
        return DomainPattern(raw, DomainPatternKind.SUBDOMAIN, canonical_host(raw[2:]))
    if "://" in raw:
        parsed = canonicalize_url(raw if "/" in raw.partition("://")[2] else raw + "/")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise NetworkPolicyError("invalid_domain_pattern", "origin pattern cannot contain path, query or fragment")
        return DomainPattern(raw, DomainPatternKind.EXACT_ORIGIN, parsed.host, parsed.scheme, parsed.port)
    if "*" in raw:
        raise NetworkPolicyError("invalid_domain_pattern", "wildcard is allowed only as a leading '*.'")
    return DomainPattern(raw, DomainPatternKind.EXACT_HOST, canonical_host(raw))


def resolved_address(raw: str) -> ResolvedAddress:
    candidate = str(raw).strip()
    try:
        parsed = ipaddress.ip_address(candidate)
    except ValueError as exc:
        raise NetworkPolicyError("invalid_resolved_ip", f"resolver returned invalid IP address {candidate!r}") from exc
    return ResolvedAddress(
        address=str(parsed),
        family="ipv6" if parsed.version == 6 else "ipv4",
        classification=classify_address(parsed),
    )


def classify_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> AddressClass:
    metadata = (
        ipaddress.ip_network("169.254.169.254/32"),
        ipaddress.ip_network("fd00:ec2::254/128"),
    )
    documentation = (
        ipaddress.ip_network("192.0.2.0/24"),
        ipaddress.ip_network("198.51.100.0/24"),
        ipaddress.ip_network("203.0.113.0/24"),
        ipaddress.ip_network("2001:db8::/32"),
    )
    benchmark = (ipaddress.ip_network("198.18.0.0/15"),)
    cgnat = (ipaddress.ip_network("100.64.0.0/10"),)
    if any(address in network for network in metadata if network.version == address.version):
        return AddressClass.METADATA
    if any(address in network for network in documentation if network.version == address.version):
        return AddressClass.DOCUMENTATION
    if any(address in network for network in benchmark if network.version == address.version):
        return AddressClass.BENCHMARK
    if any(address in network for network in cgnat if network.version == address.version):
        return AddressClass.CARRIER_GRADE_NAT
    if address.is_loopback:
        return AddressClass.LOOPBACK
    if address.is_link_local:
        return AddressClass.LINK_LOCAL
    if address.is_private:
        return AddressClass.PRIVATE
    if address.is_multicast:
        return AddressClass.MULTICAST
    if address.is_unspecified:
        return AddressClass.UNSPECIFIED
    if address.is_reserved:
        return AddressClass.RESERVED
    if not address.is_global:
        return AddressClass.RESERVED
    return AddressClass.PUBLIC


def compile_patterns(values: Iterable[str]) -> tuple[DomainPattern, ...]:
    return tuple(parse_domain_pattern(value) for value in values)
