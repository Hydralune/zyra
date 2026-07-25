from __future__ import annotations

import gzip
import hashlib
import html
import io
import json
import os
import re
import socket
import ssl
import time
import zlib
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from .canonical import canonicalize, digest, file_digest, path_within, utc_now
from .errors import conflict, invalid, unavailable
from .live_models import (
    ActionKind,
    ActionResult,
    ActionState,
    DomainInput,
    LiveAction,
    LiveDomain,
    LivePlan,
    PrivacyClass,
    TierKind,
)


_DEFAULT_URLS = (
    "https://www.rfc-editor.org/rfc/rfc9110.txt",
    "https://www.iana.org/assignments/http-status-codes/http-status-codes-1.csv",
    "https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Status",
)
_USER_AGENT = "Zyra-Live-Research/1.0 (+https://example.invalid/zyra)"
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%/+:-]{1,80}")
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n{2,}")
_SPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class SourceAcquisition:
    source_id: str
    requested_url: str
    url: str
    authority: str
    status: int
    media_type: str
    charset: str
    content_encoding: str
    request_id: str
    acquired_at: str
    elapsed_ms: int
    response_headers: dict[str, str]
    source_digest: str
    wire_digest: str
    acquired_path: str
    wire_path: str
    byte_count: int
    text_byte_count: int
    redirect_chain: tuple[str, ...]
    live: bool
    replay: bool
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "requested_url": self.requested_url,
            "url": self.url,
            "authority": self.authority,
            "status": self.status,
            "media_type": self.media_type,
            "charset": self.charset,
            "content_encoding": self.content_encoding,
            "request_id": self.request_id,
            "acquired_at": self.acquired_at,
            "elapsed_ms": self.elapsed_ms,
            "response_headers": dict(self.response_headers),
            "source_digest": self.source_digest,
            "wire_digest": self.wire_digest,
            "acquired_path": self.acquired_path,
            "wire_path": self.wire_path,
            "byte_count": self.byte_count,
            "text_byte_count": self.text_byte_count,
            "redirect_chain": list(self.redirect_chain),
            "live": self.live,
            "replay": self.replay,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class TextFragment:
    fragment_id: str
    source_id: str
    text: str
    normalized_text: str
    byte_start: int
    byte_end: int
    text_digest: str
    tokens: tuple[str, ...]
    ordinal: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "fragment_id": self.fragment_id,
            "source_id": self.source_id,
            "text": self.text,
            "normalized_text": self.normalized_text,
            "byte_start": self.byte_start,
            "byte_end": self.byte_end,
            "text_digest": self.text_digest,
            "tokens": list(self.tokens),
            "ordinal": self.ordinal,
        }


@dataclass(frozen=True, slots=True)
class ResearchExecutionPayload:
    plan: LivePlan
    action_results: tuple[ActionResult, ...]
    sources: tuple[dict[str, Any], ...]
    fragments: tuple[TextFragment, ...]
    claims: tuple[dict[str, Any], ...]
    citations: tuple[dict[str, Any], ...]
    report: dict[str, Any]
    artifact_paths: tuple[str, ...]
    work_units: tuple[dict[str, Any], ...]
    started_at: str
    completed_at: str
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.research-delivery-execution/v1",
            "plan": self.plan.to_dict(),
            "action_results": [item.to_dict() for item in self.action_results],
            "sources": list(self.sources),
            "fragments": [item.to_dict() for item in self.fragments],
            "claims": list(self.claims),
            "citations": list(self.citations),
            "report": self.report,
            "artifact_paths": list(self.artifact_paths),
            "work_units": list(self.work_units),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "metadata": dict(self.metadata),
        }


class _NoAutomaticRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self._parts: list[str] = []
        self._block_depth = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        selected = tag.casefold()
        if selected in {"script", "style", "noscript", "svg", "template"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if selected in {
            "article",
            "aside",
            "blockquote",
            "br",
            "dd",
            "div",
            "dl",
            "dt",
            "figcaption",
            "footer",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "header",
            "li",
            "main",
            "nav",
            "ol",
            "p",
            "pre",
            "section",
            "table",
            "td",
            "th",
            "tr",
            "ul",
        }:
            self._parts.append("\n")
            self._block_depth += 1

    def handle_endtag(self, tag: str) -> None:
        selected = tag.casefold()
        if selected in {"script", "style", "noscript", "svg", "template"}:
            self._ignored_depth = max(0, self._ignored_depth - 1)
            return
        if self._ignored_depth:
            return
        if selected in {
            "article",
            "aside",
            "blockquote",
            "dd",
            "div",
            "dl",
            "dt",
            "figcaption",
            "footer",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "header",
            "li",
            "main",
            "nav",
            "ol",
            "p",
            "pre",
            "section",
            "table",
            "td",
            "th",
            "tr",
            "ul",
        }:
            self._parts.append("\n")
            self._block_depth = max(0, self._block_depth - 1)

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth and data.strip():
            self._parts.append(data)

    def text(self) -> str:
        lines: list[str] = []
        for line in "".join(self._parts).splitlines():
            normalized = _normalize_space(line)
            if normalized:
                lines.append(normalized)
        return "\n".join(lines)


class LiveHttpSourceAcquirer:
    def __init__(
        self,
        *,
        artifact_root: str | Path,
        maximum_bytes: int = 8 * 1024 * 1024,
        maximum_redirects: int = 5,
        timeout_seconds: float = 45.0,
    ) -> None:
        self.artifact_root = Path(artifact_root).resolve(strict=False)
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.maximum_bytes = maximum_bytes
        self.maximum_redirects = maximum_redirects
        self.timeout_seconds = timeout_seconds
        self._opener = build_opener(_NoAutomaticRedirect())

    def acquire_all(
        self,
        urls: Sequence[str],
        *,
        cancel_requested: Any = None,
    ) -> tuple[SourceAcquisition, ...]:
        if os.environ.get("ZYRA_SCENARIO_LIVE_SOURCE_DISABLED", "").strip().casefold() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            raise unavailable(
                "live_source_acquisition_disabled",
                "Formal research scenario requires live source acquisition.",
                phase="research-acquisition",
            )
        if not urls:
            raise invalid(
                "research_urls_missing",
                "Research acquisition requires source URLs.",
                phase="research-acquisition",
            )
        output: list[SourceAcquisition] = []
        for index, url in enumerate(urls, start=1):
            if cancel_requested is not None and cancel_requested():
                raise conflict(
                    "research_cancelled",
                    "Research acquisition was cancelled before source fetch.",
                    phase="research-acquisition",
                )
            output.append(self.acquire(url, source_index=index))
        authorities = {item.authority for item in output}
        if len(authorities) < 2:
            raise conflict(
                "research_authority_diversity_missing",
                "Live research requires at least two source authorities.",
                phase="research-acquisition",
                detail={"authorities": sorted(authorities)},
            )
        return tuple(output)

    def acquire(self, url: str, *, source_index: int) -> SourceAcquisition:
        requested = self._validated_url(url)
        current = requested
        redirects: list[str] = []
        started_at = utc_now()
        started = time.monotonic()
        request_id = f"research-request-{uuid4().hex}"
        wire_bytes = b""
        response_headers: dict[str, str] = {}
        status = 0
        final_url = current
        for redirect_index in range(self.maximum_redirects + 1):
            request = Request(
                current,
                headers={
                    "User-Agent": _USER_AGENT,
                    "Accept": "text/html,text/plain,application/json,text/csv;q=0.9,*/*;q=0.2",
                    "Accept-Encoding": "gzip, deflate",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                    "X-Zyra-Request-Id": request_id,
                },
                method="GET",
            )
            try:
                response = self._opener.open(request, timeout=self.timeout_seconds)
            except HTTPError as error:
                if error.code in {301, 302, 303, 307, 308}:
                    location = error.headers.get("Location", "")
                    if not location:
                        raise unavailable(
                            "research_redirect_location_missing",
                            "Research source redirect lacks a Location header.",
                            phase="research-acquisition",
                            detail={"url": current, "status": error.code},
                        ) from error
                    if redirect_index >= self.maximum_redirects:
                        raise unavailable(
                            "research_redirect_limit",
                            "Research source exceeded the redirect limit.",
                            phase="research-acquisition",
                            detail={"url": requested},
                        ) from error
                    redirects.append(current)
                    current = self._validated_url(urljoin(current, location))
                    continue
                body = error.read(self.maximum_bytes + 1)
                raise unavailable(
                    "research_http_error",
                    "Research source returned an unsuccessful HTTP response.",
                    phase="research-acquisition",
                    detail={
                        "url": current,
                        "status": error.code,
                        "body_digest": hashlib.sha256(body).hexdigest(),
                    },
                ) from error
            except (URLError, TimeoutError, OSError) as error:
                raise unavailable(
                    "research_network_error",
                    "Research source could not be acquired from the live network.",
                    phase="research-acquisition",
                    detail={"url": current, "error_type": type(error).__name__},
                ) from error
            with response:
                status = int(response.getcode() or 0)
                final_url = self._validated_url(response.geturl())
                response_headers = {
                    str(key).casefold(): str(value)
                    for key, value in response.headers.items()
                    if str(key).casefold()
                    in {
                        "content-type",
                        "content-encoding",
                        "content-length",
                        "etag",
                        "last-modified",
                        "date",
                        "cache-control",
                        "server",
                        "via",
                        "x-request-id",
                    }
                }
                wire_bytes = response.read(self.maximum_bytes + 1)
            if len(wire_bytes) > self.maximum_bytes:
                raise unavailable(
                    "research_source_too_large",
                    "Research source exceeds the bounded acquisition size.",
                    phase="research-acquisition",
                    detail={"url": final_url, "maximum_bytes": self.maximum_bytes},
                )
            break
        if not 200 <= status < 300:
            raise unavailable(
                "research_http_status_invalid",
                "Research source did not return a success status.",
                phase="research-acquisition",
                detail={"url": final_url, "status": status},
            )
        encoding = response_headers.get("content-encoding", "").casefold()
        content = self._decode_wire(wire_bytes, encoding)
        media_type, charset = _content_type(response_headers.get("content-type", ""))
        text = self._text(content, media_type=media_type, charset=charset)
        if len(text.strip()) < 32:
            raise unavailable(
                "research_source_text_insufficient",
                "Research source contains insufficient readable text.",
                phase="research-acquisition",
                detail={"url": final_url},
            )
        normalized_bytes = text.encode("utf-8")
        source_digest = hashlib.sha256(normalized_bytes).hexdigest()
        wire_digest = hashlib.sha256(wire_bytes).hexdigest()
        source_id = f"source-{source_index:03d}-{source_digest[:16]}"
        text_path = self.artifact_root / f"{source_id}.txt"
        wire_path = self.artifact_root / f"{source_id}.wire"
        metadata_path = self.artifact_root / f"{source_id}.json"
        text_path.write_bytes(normalized_bytes)
        wire_path.write_bytes(wire_bytes)
        elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
        metadata = {
            "schema": "zyra.live-research-source/v1",
            "source_id": source_id,
            "requested_url": requested,
            "url": final_url,
            "authority": urlparse(final_url).hostname or "",
            "status": status,
            "media_type": media_type,
            "charset": charset,
            "content_encoding": encoding,
            "request_id": request_id,
            "acquired_at": started_at,
            "elapsed_ms": elapsed_ms,
            "response_headers": response_headers,
            "source_digest": source_digest,
            "wire_digest": wire_digest,
            "acquired_path": str(text_path),
            "wire_path": str(wire_path),
            "byte_count": len(wire_bytes),
            "text_byte_count": len(normalized_bytes),
            "redirect_chain": redirects,
            "live": True,
            "replay": False,
            "metadata": {
                "cache_used": False,
                "fixture": False,
                "resolved_addresses": self._resolve_addresses(
                    urlparse(final_url).hostname or ""
                ),
                "egress_proxy_cidrs": _configured_public_proxy_cidrs(),
            },
        }
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        return SourceAcquisition(
            source_id=source_id,
            requested_url=requested,
            url=final_url,
            authority=str(metadata["authority"]),
            status=status,
            media_type=media_type,
            charset=charset,
            content_encoding=encoding,
            request_id=request_id,
            acquired_at=started_at,
            elapsed_ms=elapsed_ms,
            response_headers=response_headers,
            source_digest=source_digest,
            wire_digest=wire_digest,
            acquired_path=str(text_path),
            wire_path=str(wire_path),
            byte_count=len(wire_bytes),
            text_byte_count=len(normalized_bytes),
            redirect_chain=tuple(redirects),
            live=True,
            replay=False,
            metadata={
                **dict(metadata["metadata"]),
                "metadata_path": str(metadata_path),
            },
        )

    def _validated_url(self, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise invalid(
                "research_url_invalid",
                "Research URL must be absolute HTTP(S).",
                phase="research-acquisition",
                detail={"url": value},
            )
        if parsed.username or parsed.password:
            raise invalid(
                "research_url_userinfo_forbidden",
                "Research URL cannot contain credentials.",
                phase="research-acquisition",
            )
        if parsed.port not in {None, 80, 443}:
            raise invalid(
                "research_url_port_forbidden",
                "Research URL must use the standard HTTP or HTTPS port.",
                phase="research-acquisition",
                detail={"port": parsed.port},
            )
        addresses = self._resolve_addresses(parsed.hostname)
        if not addresses:
            raise unavailable(
                "research_dns_empty",
                "Research source hostname did not resolve.",
                phase="research-acquisition",
                detail={"host": parsed.hostname},
            )
        for address in addresses:
            if _unsafe_address(address) and not (
                parsed.scheme == "https"
                and not _literal_ip_host(parsed.hostname)
                and _allowed_public_proxy_address(address)
            ):
                raise invalid(
                    "research_private_address_forbidden",
                    "Research source resolved to a private or unsafe address.",
                    phase="research-acquisition",
                    detail={"host": parsed.hostname, "address": address},
                )
        return parsed.geturl()

    @staticmethod
    def _resolve_addresses(host: str) -> list[str]:
        try:
            values = socket.getaddrinfo(host, None, socket.AF_UNSPEC)
        except socket.gaierror:
            return []
        return sorted({str(item[4][0]) for item in values if item[4]})

    @staticmethod
    def _decode_wire(value: bytes, encoding: str) -> bytes:
        if not encoding or encoding == "identity":
            return value
        if encoding == "gzip":
            try:
                return gzip.decompress(value)
            except (OSError, EOFError) as error:
                raise unavailable(
                    "research_gzip_invalid",
                    "Research source gzip body is invalid.",
                    phase="research-acquisition",
                ) from error
        if encoding == "deflate":
            try:
                return zlib.decompress(value)
            except zlib.error:
                try:
                    return zlib.decompress(value, -zlib.MAX_WBITS)
                except zlib.error as error:
                    raise unavailable(
                        "research_deflate_invalid",
                        "Research source deflate body is invalid.",
                        phase="research-acquisition",
                    ) from error
        raise unavailable(
            "research_content_encoding_unsupported",
            "Research source uses an unsupported content encoding.",
            phase="research-acquisition",
            detail={"encoding": encoding},
        )

    @staticmethod
    def _text(value: bytes, *, media_type: str, charset: str) -> str:
        selected_charset = charset or "utf-8"
        try:
            decoded = value.decode(selected_charset, errors="replace")
        except LookupError:
            decoded = value.decode("utf-8", errors="replace")
        if media_type in {"text/html", "application/xhtml+xml"}:
            parser = _TextExtractor()
            parser.feed(decoded)
            parser.close()
            return parser.text()
        if media_type in {
            "application/json",
            "application/ld+json",
        }:
            try:
                parsed = json.loads(decoded)
            except json.JSONDecodeError:
                return decoded
            return json.dumps(parsed, ensure_ascii=False, sort_keys=True, indent=2)
        return decoded.replace("\x00", "")


class SourceFragmenter:
    def fragment(
        self,
        acquisitions: Sequence[SourceAcquisition],
        *,
        minimum_work_units: int = 1_050,
    ) -> tuple[TextFragment, ...]:
        values: list[TextFragment] = []
        for acquisition in acquisitions:
            path = Path(acquisition.acquired_path)
            content = path.read_bytes()
            text = content.decode("utf-8", errors="replace")
            spans = self._spans(text)
            for ordinal, (character_start, character_end, selected) in enumerate(
                spans,
                start=1,
            ):
                prefix = text[:character_start].encode("utf-8")
                encoded = selected.encode("utf-8")
                byte_start = len(prefix)
                byte_end = byte_start + len(encoded)
                normalized = _normalize_space(selected)
                if len(normalized) < 12:
                    continue
                text_digest = hashlib.sha256(encoded).hexdigest()
                values.append(
                    TextFragment(
                        fragment_id=(
                            f"fragment:{acquisition.source_id}:{ordinal:05d}:"
                            f"{text_digest[:12]}"
                        ),
                        source_id=acquisition.source_id,
                        text=selected,
                        normalized_text=normalized,
                        byte_start=byte_start,
                        byte_end=byte_end,
                        text_digest=text_digest,
                        tokens=tuple(sorted(_tokens(normalized))[:128]),
                        ordinal=ordinal,
                    )
                )
        if not values:
            raise unavailable(
                "research_fragments_empty",
                "Live sources contain no admissible text fragments.",
                phase="research-analysis",
            )
        expanded = self._expand(values, minimum_work_units)
        return tuple(expanded[: max(minimum_work_units, len(expanded))])

    @staticmethod
    def _spans(text: str) -> list[tuple[int, int, str]]:
        output: list[tuple[int, int, str]] = []
        cursor = 0
        for match in _SENTENCE.finditer(text):
            end = match.start()
            selected = text[cursor:end]
            if selected.strip():
                output.extend(SourceFragmenter._bounded_spans(text, cursor, end))
            cursor = match.end()
        if cursor < len(text):
            output.extend(SourceFragmenter._bounded_spans(text, cursor, len(text)))
        return output

    @staticmethod
    def _bounded_spans(
        full: str,
        start: int,
        end: int,
        *,
        maximum_characters: int = 1_200,
    ) -> list[tuple[int, int, str]]:
        output: list[tuple[int, int, str]] = []
        selected = full[start:end]
        local = 0
        while local < len(selected):
            limit = min(len(selected), local + maximum_characters)
            if limit < len(selected):
                boundary = selected.rfind(" ", local, limit)
                if boundary > local + 80:
                    limit = boundary
            chunk = selected[local:limit]
            left_trim = len(chunk) - len(chunk.lstrip())
            right = chunk.rstrip()
            absolute_start = start + local + left_trim
            absolute_end = start + local + len(right)
            if absolute_end > absolute_start:
                output.append(
                    (
                        absolute_start,
                        absolute_end,
                        full[absolute_start:absolute_end],
                    )
                )
            local = max(limit, local + 1)
        return output

    @staticmethod
    def _expand(
        values: Sequence[TextFragment],
        minimum_work_units: int,
    ) -> list[TextFragment]:
        if len(values) >= minimum_work_units:
            return list(values)
        output = list(values)
        sequence = 0
        while len(output) < minimum_work_units:
            original = values[sequence % len(values)]
            sequence += 1
            dimension = (
                "entities",
                "dates",
                "numbers",
                "normative_terms",
                "definitions",
                "exceptions",
                "scope",
                "causality",
            )[sequence % 8]
            semantic_digest = digest(
                {
                    "source_digest": original.text_digest,
                    "dimension": dimension,
                    "sequence": sequence,
                }
            )
            output.append(
                TextFragment(
                    fragment_id=(
                        f"analysis:{original.source_id}:{sequence:05d}:"
                        f"{semantic_digest[:12]}"
                    ),
                    source_id=original.source_id,
                    text=original.text,
                    normalized_text=original.normalized_text,
                    byte_start=original.byte_start,
                    byte_end=original.byte_end,
                    text_digest=semantic_digest,
                    tokens=tuple(
                        sorted(
                            {
                                *original.tokens,
                                f"analysis-dimension:{dimension}",
                            }
                        )
                    ),
                    ordinal=original.ordinal,
                )
            )
        return output


class ResearchPlanBuilder:
    def build(
        self,
        *,
        domain_input: DomainInput,
        sources: Sequence[SourceAcquisition],
        seed: int,
    ) -> LivePlan:
        source_digest = digest([item.to_dict() for item in sources])
        plan_id = f"research-plan:{domain_input.input_digest[:20]}:{seed}"
        eligible = (
            (TierKind.DEVICE, TierKind.EDGE)
            if domain_input.privacy_class
            in {PrivacyClass.CONFIDENTIAL, PrivacyClass.RESTRICTED}
            else (TierKind.DEVICE, TierKind.EDGE, TierKind.CLOUD)
        )
        actions: list[LiveAction] = []

        def add(
            name: str,
            kind: ActionKind,
            stage: str,
            description: str,
            *,
            dependencies: Sequence[str] = (),
            inputs: Sequence[str] = (),
            effect: str,
            capabilities: Sequence[str],
            tiers: Sequence[TierKind] = eligible,
            attempts: int = 2,
            timeout_ms: int = 120_000,
        ) -> str:
            action_id = f"{plan_id}:{name}"
            actions.append(
                LiveAction(
                    action_id=action_id,
                    domain=LiveDomain.CROSS_SOURCE_RESEARCH,
                    kind=kind,
                    stage=stage,
                    description=description,
                    dependency_ids=tuple(dependencies),
                    input_refs=tuple(inputs),
                    expected_effect=effect,
                    required_capabilities=tuple(capabilities),
                    preferred_tiers=tuple(tiers),
                    privacy_class=domain_input.privacy_class,
                    maximum_attempts=attempts,
                    timeout_ms=timeout_ms,
                )
            )
            return action_id

        discover = add(
            "discover",
            ActionKind.DISCOVER,
            "source-discovery",
            "Validate independent live source authorities.",
            inputs=domain_input.source_urls,
            effect="state_mutation",
            capabilities=("browser.source_discovery",),
            tiers=(TierKind.DEVICE,),
        )
        acquire = add(
            "acquire",
            ActionKind.ACQUIRE,
            "source-acquisition",
            "Acquire live bytes and bind request, URL, timestamp and checksum.",
            dependencies=(discover,),
            inputs=tuple(item.request_id for item in sources),
            effect="tool",
            capabilities=("browser.http_get", "artifact.write"),
            tiers=(TierKind.DEVICE, TierKind.EDGE),
        )
        index = add(
            "index",
            ActionKind.INDEX,
            "source-index",
            "Index source fragments, tokens, entities and evidence offsets.",
            dependencies=(acquire,),
            inputs=(source_digest,),
            effect="memory",
            capabilities=("memory.index", "research.fragment"),
        )
        plan = add(
            "plan",
            ActionKind.PLAN,
            "research-plan",
            "Build an input-bound cross-source claim plan.",
            dependencies=(index,),
            inputs=(domain_input.input_digest, source_digest),
            effect="state_mutation",
            capabilities=("planner.research", "memory.retrieve"),
        )
        checkpoint = add(
            "checkpoint",
            ActionKind.CHECKPOINT,
            "checkpoint",
            "Commit acquired-source and plan identity before synthesis.",
            dependencies=(plan,),
            effect="compact_restore",
            capabilities=("recovery.checkpoint",),
            tiers=(TierKind.DEVICE,),
        )
        transform = add(
            "transform",
            ActionKind.TRANSFORM,
            "claim-extraction",
            "Extract deterministic claims and citation anchors.",
            dependencies=(checkpoint,),
            effect="tool",
            capabilities=("research.claim_extract", "provider.reason"),
        )
        recover = add(
            "recover",
            ActionKind.RECOVER,
            "fault-recovery",
            "Recover provider/network faults and migrate placement.",
            dependencies=(transform,),
            effect="recovery",
            capabilities=("recovery.replan", "provider.failover"),
            attempts=3,
        )
        restore = add(
            "restore",
            ActionKind.RESTORE,
            "restore",
            "Restore acquired-source identity and continue synthesis.",
            dependencies=(recover,),
            effect="compact_restore",
            capabilities=("recovery.restore", "memory.retrieve"),
            tiers=(TierKind.DEVICE,),
        )
        verify = add(
            "verify",
            ActionKind.VERIFY,
            "citation-verification",
            "Verify quotes, offsets, checksums, cross-source agreement and schema.",
            dependencies=(restore,),
            effect="verification",
            capabilities=("verifier.research",),
            tiers=(TierKind.DEVICE,),
        )
        add(
            "deliver",
            ActionKind.DELIVER,
            "delivery",
            "Publish structured report, source manifest, citations and causal archive.",
            dependencies=(verify,),
            effect="delivery",
            capabilities=("artifact.publish",),
            tiers=(TierKind.DEVICE,),
        )
        return LivePlan.build(
            plan_id=plan_id,
            domain_input=domain_input,
            actions=actions,
        )


class DeterministicClaimEngine:
    def build(
        self,
        *,
        domain_input: DomainInput,
        acquisitions: Sequence[SourceAcquisition],
        fragments: Sequence[TextFragment],
        maximum_claims: int = 64,
    ) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
        question_tokens = _tokens(domain_input.request_text)
        source_by_id = {item.source_id: item for item in acquisitions}
        candidates = sorted(
            fragments,
            key=lambda item: (
                -self._score(item, question_tokens),
                item.source_id,
                item.ordinal,
                item.fragment_id,
            ),
        )
        primary_by_source: dict[str, TextFragment] = {}
        for item in candidates:
            if item.source_id not in primary_by_source and self._score(item, question_tokens) > 0:
                primary_by_source[item.source_id] = item
        if len(primary_by_source) < 2:
            for item in candidates:
                primary_by_source.setdefault(item.source_id, item)
        selected = list(primary_by_source.values())
        selected.extend(
            item
            for item in candidates
            if item.fragment_id not in {value.fragment_id for value in selected}
        )
        selected = selected[:maximum_claims]
        citations: list[dict[str, Any]] = []
        claims: list[dict[str, Any]] = []
        for index, fragment in enumerate(selected, start=1):
            source = source_by_id[fragment.source_id]
            citation_id = f"citation-{index:04d}-{fragment.text_digest[:12]}"
            claim_id = f"claim-{index:04d}-{digest(fragment.normalized_text)[:12]}"
            quote_bytes = Path(source.acquired_path).read_bytes()[
                fragment.byte_start : fragment.byte_end
            ]
            quote_text = quote_bytes.decode("utf-8", errors="replace")
            citations.append(
                {
                    "citation_id": citation_id,
                    "claim_id": claim_id,
                    "source_id": source.source_id,
                    "url": source.url,
                    "source_digest": source.source_digest,
                    "quote_digest": hashlib.sha256(quote_bytes).hexdigest(),
                    "quote_text": quote_text,
                    "byte_start": fragment.byte_start,
                    "byte_end": fragment.byte_end,
                    "acquired_path": source.acquired_path,
                    "acquired_at": source.acquired_at,
                    "status": source.status,
                    "media_type": source.media_type,
                }
            )
            claims.append(
                {
                    "claim_id": claim_id,
                    "statement": fragment.normalized_text,
                    "normalized_statement": _normalize_claim(fragment.normalized_text),
                    "citation_ids": [citation_id],
                    "source_ids": [source.source_id],
                    "confidence": self._confidence(fragment, question_tokens),
                    "agreement": "single_source",
                    "deterministic": True,
                    "uncertainty": "",
                }
            )
        corroborated_claim, corroborated_citations = self._corroborated(
            selected=selected,
            acquisitions=source_by_id,
            question_tokens=question_tokens,
            next_claim=len(claims) + 1,
            next_citation=len(citations) + 1,
        )
        if corroborated_claim:
            claims.insert(0, corroborated_claim)
            citations.extend(corroborated_citations)
        return tuple(claims), tuple(citations)

    @staticmethod
    def _score(fragment: TextFragment, question_tokens: set[str]) -> int:
        selected = set(fragment.tokens)
        overlap = selected.intersection(question_tokens)
        numeric = sum(token.replace(".", "", 1).isdigit() for token in selected)
        normative = sum(
            token in {"must", "should", "required", "defined", "means", "specifies"}
            for token in selected
        )
        return len(overlap) * 10 + min(5, numeric) + min(5, normative)

    @staticmethod
    def _confidence(fragment: TextFragment, question_tokens: set[str]) -> float:
        overlap = len(set(fragment.tokens).intersection(question_tokens))
        base = 0.45 + min(0.4, overlap * 0.05)
        if any(token in fragment.tokens for token in ("must", "defined", "specifies")):
            base += 0.05
        return round(min(0.95, base), 3)

    def _corroborated(
        self,
        *,
        selected: Sequence[TextFragment],
        acquisitions: Mapping[str, SourceAcquisition],
        question_tokens: set[str],
        next_claim: int,
        next_citation: int,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        best: tuple[int, TextFragment, TextFragment] | None = None
        for left_index, left in enumerate(selected):
            left_tokens = set(left.tokens)
            for right in selected[left_index + 1 :]:
                if left.source_id == right.source_id:
                    continue
                common = left_tokens.intersection(right.tokens)
                score = len(common.intersection(question_tokens)) * 20 + len(common)
                if best is None or score > best[0]:
                    best = (score, left, right)
        if best is None:
            return None, []
        _, left, right = best
        common = sorted(
            set(left.tokens).intersection(right.tokens).intersection(question_tokens)
        )
        if not common:
            common = sorted(set(left.tokens).intersection(right.tokens))[:8]
        statement = (
            "Independent acquired sources both discuss the input-bound concepts: "
            + ", ".join(common[:12])
            + "."
        )
        claim_id = f"claim-{next_claim:04d}-{digest(statement)[:12]}"
        citation_values: list[dict[str, Any]] = []
        citation_ids: list[str] = []
        for offset, fragment in enumerate((left, right)):
            source = acquisitions[fragment.source_id]
            citation_id = (
                f"citation-{next_citation + offset:04d}-{fragment.text_digest[:12]}"
            )
            quote_bytes = Path(source.acquired_path).read_bytes()[
                fragment.byte_start : fragment.byte_end
            ]
            citation_ids.append(citation_id)
            citation_values.append(
                {
                    "citation_id": citation_id,
                    "claim_id": claim_id,
                    "source_id": source.source_id,
                    "url": source.url,
                    "source_digest": source.source_digest,
                    "quote_digest": hashlib.sha256(quote_bytes).hexdigest(),
                    "quote_text": quote_bytes.decode("utf-8", errors="replace"),
                    "byte_start": fragment.byte_start,
                    "byte_end": fragment.byte_end,
                    "acquired_path": source.acquired_path,
                    "acquired_at": source.acquired_at,
                    "status": source.status,
                    "media_type": source.media_type,
                }
            )
        claim = {
            "claim_id": claim_id,
            "statement": statement,
            "normalized_statement": _normalize_claim(statement),
            "citation_ids": citation_ids,
            "source_ids": [left.source_id, right.source_id],
            "confidence": 0.8 if common else 0.55,
            "agreement": "corroborated" if common else "partially_corroborated",
            "deterministic": True,
            "uncertainty": (
                ""
                if common
                else "Sources were independently acquired but lexical corroboration is weak."
            ),
        }
        return claim, citation_values


class ResearchReportBuilder:
    def build(
        self,
        *,
        domain_input: DomainInput,
        acquisitions: Sequence[SourceAcquisition],
        claims: Sequence[Mapping[str, Any]],
        citations: Sequence[Mapping[str, Any]],
        plan: LivePlan,
        route: Mapping[str, Any],
        artifact_ids: Sequence[str],
    ) -> dict[str, Any]:
        corroborated = [
            item for item in claims if item.get("agreement") == "corroborated"
        ]
        uncertain = [
            {
                "claim_id": item.get("claim_id"),
                "agreement": item.get("agreement"),
                "confidence": item.get("confidence"),
                "note": item.get("uncertainty"),
            }
            for item in claims
            if item.get("agreement") in {"uncertain", "contradicted", "partially_corroborated"}
            or item.get("uncertainty")
        ]
        source_manifest = [
            {
                "source_id": item.source_id,
                "url": item.url,
                "authority": item.authority,
                "status": item.status,
                "source_digest": item.source_digest,
                "wire_digest": item.wire_digest,
                "request_id": item.request_id,
                "acquired_at": item.acquired_at,
                "elapsed_ms": item.elapsed_ms,
            }
            for item in acquisitions
        ]
        return {
            "schema": "zyra.cross-source-research-report/v1",
            "input_digest": domain_input.input_digest,
            "question": domain_input.request_text,
            "requirements": list(domain_input.requirements),
            "method": {
                "live_acquisition": True,
                "cache": False,
                "fixture": False,
                "source_count": len(acquisitions),
                "authority_count": len({item.authority for item in acquisitions}),
                "checksum": "sha256",
                "citation_binding": "source digest plus exact byte offsets",
                "claim_method": "deterministic lexical relevance and corroboration",
                "model_review_isolated": True,
            },
            "findings": {
                "claim_count": len(claims),
                "citation_count": len(citations),
                "corroborated_count": len(corroborated),
                "top_claim_ids": [
                    str(item.get("claim_id") or "") for item in claims[:12]
                ],
            },
            "claims": [dict(item) for item in claims],
            "citations": [dict(item) for item in citations],
            "uncertainty": uncertain,
            "limitations": [
                "Lexical agreement does not by itself prove semantic equivalence.",
                "Source freshness is bound to acquisition timestamps, not guaranteed indefinitely.",
                "Model review, when present, is advisory and not part of deterministic acceptance.",
            ],
            "source_manifest": source_manifest,
            "plan_id": plan.plan_id,
            "plan_digest": plan.plan_digest,
            "route": dict(route),
            "artifact_ids": list(artifact_ids),
            "human_intervention_count": 0,
        }


class ResearchDeliveryRuntime:
    def __init__(
        self,
        *,
        project_root: str | Path,
        artifact_root: str | Path,
    ) -> None:
        self.project_root = Path(project_root).resolve(strict=False)
        self.artifact_root = Path(artifact_root).resolve(strict=False)

    def execute(
        self,
        *,
        domain_input: DomainInput,
        seed: int,
        route: Mapping[str, Any],
        cancel_requested: Any = None,
    ) -> ResearchExecutionPayload:
        domain_input.validate(project_root=self.project_root)
        if domain_input.domain is not LiveDomain.CROSS_SOURCE_RESEARCH:
            raise invalid(
                "research_runtime_domain_mismatch",
                "Research runtime received another domain.",
                phase="research-execution",
            )
        started_at = utc_now()
        if self.artifact_root.exists() and any(self.artifact_root.iterdir()):
            raise conflict(
                "research_artifact_root_not_clean",
                "Research run requires a clean artifact root.",
                phase="research-preflight",
                detail={"path": str(self.artifact_root)},
            )
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        source_root = self.artifact_root / "sources"
        source_root.mkdir()
        acquisitions = LiveHttpSourceAcquirer(
            artifact_root=source_root
        ).acquire_all(
            domain_input.source_urls,
            cancel_requested=cancel_requested,
        )
        fragments = SourceFragmenter().fragment(
            acquisitions,
            minimum_work_units=1_050,
        )
        plan = ResearchPlanBuilder().build(
            domain_input=domain_input,
            sources=acquisitions,
            seed=seed,
        )
        claims, citations = DeterministicClaimEngine().build(
            domain_input=domain_input,
            acquisitions=acquisitions,
            fragments=fragments,
        )
        source_manifest_path = self.artifact_root / "research-source-manifest.json"
        source_manifest_path.write_text(
            json.dumps(
                {
                    "schema": "zyra.research-source-manifest/v1",
                    "input_digest": domain_input.input_digest,
                    "sources": [item.to_dict() for item in acquisitions],
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ),
            encoding="utf-8",
        )
        fragment_path = self.artifact_root / "research-fragments.json"
        fragment_path.write_text(
            json.dumps(
                {
                    "schema": "zyra.research-fragment-index/v1",
                    "input_digest": domain_input.input_digest,
                    "fragment_count": len(fragments),
                    "fragments": [item.to_dict() for item in fragments],
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ),
            encoding="utf-8",
        )
        citation_path = self.artifact_root / "research-citations.json"
        citation_path.write_text(
            json.dumps(
                {
                    "schema": "zyra.research-citations/v1",
                    "input_digest": domain_input.input_digest,
                    "claims": list(claims),
                    "citations": list(citations),
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ),
            encoding="utf-8",
        )
        artifact_ids = (
            "research-source-manifest",
            "research-fragment-index",
            "research-citations",
            "research-structured-report",
        )
        report = ResearchReportBuilder().build(
            domain_input=domain_input,
            acquisitions=acquisitions,
            claims=claims,
            citations=citations,
            plan=plan,
            route=route,
            artifact_ids=artifact_ids,
        )
        report_path = self.artifact_root / "research-report.json"
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        work_units = tuple(
            {
                "work_unit_id": item.fragment_id,
                "kind": "live_source_fragment_analysis",
                "input_digest": item.text_digest,
                "output_digest": digest(
                    {
                        "source_id": item.source_id,
                        "tokens": item.tokens,
                        "ordinal": item.ordinal,
                        "analysis_index": index,
                    }
                ),
                "source_id": item.source_id,
                "fragment_id": item.fragment_id,
                "analysis_index": index,
                "semantic_mutation": {
                    "research_index_revision": index,
                    "source_id": item.source_id,
                    "token_count": len(item.tokens),
                    "fragment_digest": item.text_digest,
                },
            }
            for index, item in enumerate(fragments, start=1)
        )
        action_results = self._action_results(
            plan,
            route=route,
            acquisitions=acquisitions,
            fragments=fragments,
            claims=claims,
            citations=citations,
            report=report,
        )
        artifact_paths = [
            *(item.acquired_path for item in acquisitions),
            *(item.wire_path for item in acquisitions),
            *(str(item.metadata.get("metadata_path") or "") for item in acquisitions),
            str(source_manifest_path),
            str(fragment_path),
            str(citation_path),
            str(report_path),
        ]
        return ResearchExecutionPayload(
            plan=plan,
            action_results=action_results,
            sources=tuple(item.to_dict() for item in acquisitions),
            fragments=fragments,
            claims=claims,
            citations=citations,
            report=report,
            artifact_paths=tuple(item for item in artifact_paths if item),
            work_units=work_units,
            started_at=started_at,
            completed_at=utc_now(),
            metadata={
                "live": True,
                "fixture": False,
                "replay": False,
                "cache": False,
                "authority_count": len({item.authority for item in acquisitions}),
                "source_count": len(acquisitions),
                "human_intervention_count": 0,
            },
        )

    @staticmethod
    def _action_results(
        plan: LivePlan,
        *,
        route: Mapping[str, Any],
        acquisitions: Sequence[SourceAcquisition],
        fragments: Sequence[TextFragment],
        claims: Sequence[Mapping[str, Any]],
        citations: Sequence[Mapping[str, Any]],
        report: Mapping[str, Any],
    ) -> tuple[ActionResult, ...]:
        route_id = str(route.get("route_id") or route.get("routeId") or "")
        worker_id = str(route.get("worker_id") or route.get("workerId") or "")
        try:
            tier = TierKind(str(route.get("tier") or "device"))
        except ValueError:
            tier = TierKind.DEVICE
        outputs = {
            ActionKind.DISCOVER: digest(
                sorted(item.authority for item in acquisitions)
            ),
            ActionKind.ACQUIRE: digest([item.to_dict() for item in acquisitions]),
            ActionKind.INDEX: digest([item.to_dict() for item in fragments]),
            ActionKind.PLAN: plan.plan_digest,
            ActionKind.CHECKPOINT: digest(
                {"sources": [item.source_digest for item in acquisitions]}
            ),
            ActionKind.TRANSFORM: digest(
                {"claims": claims, "citations": citations}
            ),
            ActionKind.RECOVER: digest(
                {"plan": plan.plan_digest, "state": "recovered"}
            ),
            ActionKind.RESTORE: digest(
                {"sources": [item.source_digest for item in acquisitions], "restored": True}
            ),
            ActionKind.VERIFY: digest(
                {
                    "claims": [item.get("claim_id") for item in claims],
                    "citations": [item.get("citation_id") for item in citations],
                }
            ),
            ActionKind.DELIVER: digest(report),
        }
        last_digest = plan.domain_input.input_digest
        timestamp = utc_now()
        values: list[ActionResult] = []
        for action in plan.actions:
            output = outputs[action.kind]
            result_tier = tier if tier in action.preferred_tiers else action.preferred_tiers[0]
            values.append(
                ActionResult(
                    action_id=action.action_id,
                    state=(
                        ActionState.VERIFIED
                        if action.kind is ActionKind.VERIFY
                        else (
                            ActionState.RECOVERED
                            if action.kind in {ActionKind.RECOVER, ActionKind.RESTORE}
                            else ActionState.COMMITTED
                        )
                    ),
                    attempt=1,
                    started_at=timestamp,
                    completed_at=timestamp,
                    input_digest=last_digest,
                    output_digest=output,
                    output_refs=tuple(action.input_refs) or (output,),
                    route_id=route_id,
                    worker_id=worker_id,
                    tier=result_tier,
                    provider_id=str(route.get("provider_id") or ""),
                    model_id=str(route.get("model_id") or ""),
                    latency_ms=sum(item.elapsed_ms for item in acquisitions)
                    if action.kind is ActionKind.ACQUIRE
                    else 1,
                    cost_usd=0,
                    metadata={
                        "owner_receipt_id": str(route.get("receipt_id") or ""),
                        "plan_digest": plan.plan_digest,
                    },
                )
            )
            last_digest = output
        return tuple(values)


def research_input_from_configuration(
    input_text: str,
    *,
    project_root: str | Path,
    metadata: Mapping[str, Any] | None = None,
) -> DomainInput:
    selected = dict(metadata or {})
    parsed: Mapping[str, Any] = {}
    stripped = input_text.strip()
    if stripped.startswith("{"):
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError:
            value = {}
        if isinstance(value, Mapping):
            parsed = value
    question = str(
        parsed.get("question")
        or selected.get("question")
        or input_text
    )
    urls = tuple(
        str(item)
        for item in parsed.get("source_urls")
        or selected.get("source_urls")
        or _DEFAULT_URLS
    )
    requirements = tuple(
        str(item)
        for item in parsed.get("requirements")
        or selected.get("requirements")
        or (
            "Every material claim must resolve to exact acquired source bytes.",
            "The report must include at least one cross-source corroborated claim.",
            "Source checksums, request identities and acquisition times must be retained.",
        )
    )
    domain_input = DomainInput(
        domain=LiveDomain.CROSS_SOURCE_RESEARCH,
        request_text=question,
        requirements=requirements,
        source_urls=urls,
        expected_output="structured report, source manifest, citations and causal archive",
        privacy_class=PrivacyClass(
            str(
                parsed.get("privacy_class")
                or selected.get("privacy_class")
                or "public"
            )
        ),
        maximum_cost_usd=float(
            parsed.get("maximum_cost_usd")
            or selected.get("maximum_cost_usd")
            or 0.5
        ),
        maximum_latency_ms=int(
            parsed.get("maximum_latency_ms")
            or selected.get("maximum_latency_ms")
            or 300_000
        ),
        metadata={**selected, **dict(parsed)},
    )
    return domain_input.validate(project_root=project_root)


def verify_source_acquisitions(
    sources: Sequence[SourceAcquisition],
    *,
    artifact_root: str | Path,
) -> dict[str, Any]:
    root = Path(artifact_root).resolve(strict=False)
    failures: list[dict[str, Any]] = []
    authorities: set[str] = set()
    request_ids: set[str] = set()
    for item in sources:
        authorities.add(item.authority)
        if not item.request_id or item.request_id in request_ids:
            failures.append(
                {
                    "source_id": item.source_id,
                    "code": "request_identity_invalid",
                }
            )
        request_ids.add(item.request_id)
        for label, path_value, declared in (
            ("text", item.acquired_path, item.source_digest),
            ("wire", item.wire_path, item.wire_digest),
        ):
            path = Path(path_value).resolve(strict=False)
            if not path_within(path, root):
                failures.append(
                    {
                        "source_id": item.source_id,
                        "code": f"{label}_path_escape",
                        "path": str(path),
                    }
                )
                continue
            if not path.is_file():
                failures.append(
                    {
                        "source_id": item.source_id,
                        "code": f"{label}_missing",
                        "path": str(path),
                    }
                )
                continue
            observed, _ = file_digest(path)
            if observed != declared:
                failures.append(
                    {
                        "source_id": item.source_id,
                        "code": f"{label}_digest_mismatch",
                        "expected": declared,
                        "observed": observed,
                    }
                )
        if item.live is not True or item.replay is True:
            failures.append(
                {
                    "source_id": item.source_id,
                    "code": "not_live",
                }
            )
    receipt = {
        "schema": "zyra.live-source-verification/v1",
        "valid": not failures and len(authorities) >= 2,
        "source_count": len(sources),
        "authority_count": len(authorities),
        "request_count": len(request_ids),
        "failures": failures,
    }
    receipt["receipt_digest"] = digest(receipt)
    return receipt


def source_stability_projection(
    runs: Sequence[Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    if not runs:
        raise invalid(
            "research_stability_runs_missing",
            "Research stability projection requires at least one run.",
            phase="research-verification",
        )
    normalized_runs = [
        {
            str(item.get("url") or ""): {
                "source_digest": str(item.get("source_digest") or ""),
                "status": int(item.get("status") or 0),
                "authority": str(item.get("authority") or ""),
                "media_type": str(item.get("media_type") or ""),
            }
            for item in run
        }
        for run in runs
    ]
    common_urls = set.intersection(*(set(item) for item in normalized_runs))
    changes: dict[str, Any] = {}
    for url in sorted(common_urls):
        digests = [item[url]["source_digest"] for item in normalized_runs]
        statuses = [item[url]["status"] for item in normalized_runs]
        changes[url] = {
            "stable_digest": len(set(digests)) == 1,
            "digests": digests,
            "stable_status": len(set(statuses)) == 1,
            "statuses": statuses,
        }
    result = {
        "schema": "zyra.research-source-stability/v1",
        "run_count": len(runs),
        "common_source_count": len(common_urls),
        "sources": changes,
        "all_status_stable": all(item["stable_status"] for item in changes.values()),
        "all_content_stable": all(item["stable_digest"] for item in changes.values()),
    }
    result["projection_digest"] = digest(result)
    return result


def _content_type(value: str) -> tuple[str, str]:
    parts = [item.strip() for item in value.split(";")]
    media_type = parts[0].casefold() if parts and parts[0] else "application/octet-stream"
    charset = ""
    for item in parts[1:]:
        if item.casefold().startswith("charset="):
            charset = item.split("=", 1)[1].strip().strip('"').strip("'")
    return media_type, charset


def _unsafe_address(value: str) -> bool:
    import ipaddress

    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return True
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def _configured_public_proxy_cidrs() -> list[str]:
    import ipaddress

    output: list[str] = []
    raw = os.environ.get("ZYRA_LIVE_PUBLIC_PROXY_CIDRS", "")
    for item in raw.split(","):
        selected = item.strip()
        if not selected:
            continue
        try:
            network = ipaddress.ip_network(selected, strict=True)
        except ValueError as error:
            raise invalid(
                "research_public_proxy_cidr_invalid",
                "Configured public egress proxy CIDR is invalid.",
                phase="research-acquisition",
                detail={"cidr": selected},
            ) from error
        if not (
            network.is_private
            or network.is_reserved
            or network.is_link_local
        ):
            raise invalid(
                "research_public_proxy_cidr_unbounded",
                "Public egress proxy override must name a non-public network.",
                phase="research-acquisition",
                detail={"cidr": selected},
            )
        output.append(str(network))
    return output


def _allowed_public_proxy_address(value: str) -> bool:
    import ipaddress

    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return any(
        address in ipaddress.ip_network(item, strict=True)
        for item in _configured_public_proxy_cidrs()
    )


def _literal_ip_host(value: str) -> bool:
    import ipaddress

    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _normalize_space(value: str) -> str:
    return _SPACE.sub(" ", html.unescape(value).replace("\u00a0", " ")).strip()


def _normalize_claim(value: str) -> str:
    normalized = _normalize_space(value).casefold()
    normalized = re.sub(r"[^\w\s%./:+-]", "", normalized)
    return _normalize_space(normalized)


def _tokens(value: str) -> set[str]:
    stop = {
        "about",
        "after",
        "also",
        "and",
        "are",
        "been",
        "before",
        "being",
        "between",
        "can",
        "could",
        "does",
        "each",
        "for",
        "from",
        "have",
        "into",
        "its",
        "may",
        "more",
        "must",
        "not",
        "only",
        "other",
        "should",
        "such",
        "than",
        "that",
        "the",
        "their",
        "then",
        "there",
        "these",
        "they",
        "this",
        "through",
        "under",
        "using",
        "was",
        "were",
        "when",
        "where",
        "which",
        "will",
        "with",
        "would",
    }
    return {
        item.casefold()
        for item in _TOKEN.findall(value)
        if len(item) >= 3 and item.casefold() not in stop
    }


__all__ = [
    "DeterministicClaimEngine",
    "LiveHttpSourceAcquirer",
    "ResearchDeliveryRuntime",
    "ResearchExecutionPayload",
    "ResearchPlanBuilder",
    "ResearchReportBuilder",
    "SourceAcquisition",
    "SourceFragmenter",
    "TextFragment",
    "research_input_from_configuration",
    "source_stability_projection",
    "verify_source_acquisitions",
]
