from __future__ import annotations

import socket
import threading
from collections.abc import Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any

from .errors import PortConflict


@dataclass(frozen=True, slots=True)
class PortObservation:
    host: str
    port: int
    available: bool
    family: str
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "available": self.available,
            "family": self.family,
            "error": self.error,
        }


class PortReservation(AbstractContextManager["PortReservation"]):
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.socket: socket.socket | None = None

    def acquire(self) -> "PortReservation":
        if self.socket is not None:
            return self
        family = socket.AF_INET6 if ":" in self.host else socket.AF_INET
        candidate = socket.socket(family, socket.SOCK_STREAM)
        candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        try:
            candidate.bind((self.host, self.port))
            candidate.listen(1)
        except OSError as error:
            candidate.close()
            raise PortConflict(
                "deployment_port_conflict",
                "deployment endpoint is already occupied or unavailable",
                operation="reserve_port",
                details={
                    "host": self.host,
                    "port": self.port,
                    "error": f"{type(error).__name__}: {error}",
                },
            ) from error
        self.socket = candidate
        return self

    def release(self) -> None:
        current, self.socket = self.socket, None
        if current is not None:
            current.close()

    def __enter__(self) -> "PortReservation":
        return self.acquire()

    def __exit__(self, *args: Any) -> None:
        self.release()


class PortInspector:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._reservations: dict[tuple[str, int], PortReservation] = {}

    @staticmethod
    def validate(host: str, port: int) -> None:
        if not host.strip():
            raise ValueError("port host cannot be empty")
        if not 1 <= int(port) <= 65535:
            raise ValueError("port must be between 1 and 65535")

    def observe(self, host: str, port: int) -> PortObservation:
        self.validate(host, port)
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        candidate = socket.socket(family, socket.SOCK_STREAM)
        candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        try:
            candidate.bind((host, port))
            return PortObservation(
                host=host,
                port=port,
                available=True,
                family="ipv6" if family == socket.AF_INET6 else "ipv4",
            )
        except OSError as error:
            return PortObservation(
                host=host,
                port=port,
                available=False,
                family="ipv6" if family == socket.AF_INET6 else "ipv4",
                error=f"{type(error).__name__}: {error}",
            )
        finally:
            candidate.close()

    def require_available(self, host: str, port: int) -> PortObservation:
        observation = self.observe(host, port)
        if not observation.available:
            raise PortConflict(
                "deployment_port_conflict",
                "deployment port is not available",
                operation="require_available",
                details=observation.to_dict(),
            )
        return observation

    def reserve(self, host: str, port: int) -> PortReservation:
        self.validate(host, port)
        key = (host, port)
        with self._lock:
            if key in self._reservations:
                raise PortConflict(
                    "deployment_port_already_reserved",
                    "deployment port is already reserved by this supervisor",
                    operation="reserve",
                    details={"host": host, "port": port},
                )
            reservation = PortReservation(host, port).acquire()
            self._reservations[key] = reservation
            return reservation

    def release(self, host: str, port: int) -> bool:
        key = (host, port)
        with self._lock:
            reservation = self._reservations.pop(key, None)
        if reservation is None:
            return False
        reservation.release()
        return True

    def release_all(self) -> None:
        with self._lock:
            reservations = list(self._reservations.values())
            self._reservations.clear()
        for reservation in reservations:
            reservation.release()

    def observe_many(
        self,
        endpoints: Iterable[tuple[str, int]],
    ) -> list[PortObservation]:
        seen: set[tuple[str, int]] = set()
        result: list[PortObservation] = []
        for host, port in endpoints:
            key = (host, int(port))
            if key in seen:
                raise PortConflict(
                    "deployment_endpoint_duplicate",
                    "deployment endpoint appears more than once",
                    operation="observe_many",
                    details={"host": host, "port": port},
                )
            seen.add(key)
            result.append(self.observe(*key))
        return result


__all__ = [
    "PortInspector",
    "PortObservation",
    "PortReservation",
]
