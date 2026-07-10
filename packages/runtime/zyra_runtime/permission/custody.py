from __future__ import annotations

"""Bearer-proof custody for persisted permission sessions.

Session identifiers are selectors, not authority.  This module adds the
server-owned possession proof needed before a CodeWorker may attach to an
existing session overlay.  Only a salted token hash and exact run/task/
workspace binding are persisted; plaintext tokens are returned once through a
non-serialized runtime receipt and never enter events or snapshots.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
from pathlib import Path
import secrets
from typing import Any, Mapping
from uuid import uuid4

from .canonical import canonical_arguments_json
from .store import PermissionStateStore


PERMISSION_CUSTODY_SCHEMA = "zyra.permission-session-custody"
PERMISSION_CUSTODY_VERSION = 1


class PermissionSessionCustodyError(RuntimeError):
    code = "permission_session_custody_error"


class PermissionSessionCustodyRequired(PermissionSessionCustodyError):
    code = "session_custody_required"


class PermissionSessionCustodyInvalid(PermissionSessionCustodyError):
    code = "session_custody_invalid"


class PermissionSessionCustodyScopeMismatch(PermissionSessionCustodyError):
    code = "session_custody_scope_mismatch"


class PermissionSessionCustodyRevoked(PermissionSessionCustodyError):
    code = "session_custody_revoked"


@dataclass(frozen=True, slots=True)
class PermissionSessionCustodyBinding:
    session_id: str
    run_id: str
    task_id: str
    workspace_root: str

    def __post_init__(self) -> None:
        if not all((self.session_id, self.run_id, self.task_id, self.workspace_root)):
            raise ValueError("session custody requires session/run/task/workspace identity")
        object.__setattr__(self, "workspace_root", str(Path(self.workspace_root).resolve()))

    @property
    def fingerprint(self) -> str:
        encoded = canonical_arguments_json(self.to_dict())
        return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"

    def to_dict(self) -> dict[str, str]:
        return {
            "session_id": self.session_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "workspace_root": self.workspace_root,
        }


@dataclass(frozen=True, slots=True, repr=False)
class PermissionSessionCustodyReceipt:
    binding: PermissionSessionCustodyBinding
    custody_id: str
    custody_fingerprint: str
    created: bool
    verified: bool
    issued_at: str
    epoch: int
    token: str = field(default="", repr=False)

    @property
    def session_id(self) -> str:
        return self.binding.session_id

    def __repr__(self) -> str:
        return (
            "PermissionSessionCustodyReceipt("
            f"session_id={self.session_id!r}, custody_id={self.custody_id!r}, "
            f"created={self.created!r}, verified={self.verified!r}, epoch={self.epoch!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        # The bearer token is intentionally excluded.
        return {
            "schema": PERMISSION_CUSTODY_SCHEMA,
            "schema_version": PERMISSION_CUSTODY_VERSION,
            "binding": self.binding.to_dict(),
            "custody_id": self.custody_id,
            "custody_fingerprint": self.custody_fingerprint,
            "created": self.created,
            "verified": self.verified,
            "issued_at": self.issued_at,
            "epoch": self.epoch,
        }

    def metadata(self) -> dict[str, str]:
        return {
            "permission_session_custody_id": self.custody_id,
            "permission_session_custody_fingerprint": self.custody_fingerprint,
            "permission_session_custody_created": str(self.created).lower(),
            "permission_session_custody_verified": str(self.verified).lower(),
            "permission_session_custody_epoch": str(self.epoch),
        }


class PermissionSessionCustodyStore:
    """Atomic logical view over ``PermissionStateStore.metadata``."""

    def __init__(self, state_store: PermissionStateStore) -> None:
        self.state_store = state_store

    def claim(
        self,
        binding: PermissionSessionCustodyBinding,
        *,
        presented_token: str = "",
        external_session_exists: bool = False,
    ) -> PermissionSessionCustodyReceipt:
        candidate_token = secrets.token_urlsafe(48)
        candidate_salt = secrets.token_hex(24)
        holder: list[PermissionSessionCustodyReceipt] = []

        def mutate(state: dict[str, Any]) -> None:
            metadata = state.setdefault("metadata", {})
            custody = metadata.setdefault(
                "session_custody",
                {
                    "schema": PERMISSION_CUSTODY_SCHEMA,
                    "schema_version": PERMISSION_CUSTODY_VERSION,
                    "records": {},
                },
            )
            if not isinstance(custody, dict):
                raise PermissionSessionCustodyInvalid("permission custody state is corrupt")
            if custody.get("schema") != PERMISSION_CUSTODY_SCHEMA or int(custody.get("schema_version") or 0) != 1:
                raise PermissionSessionCustodyInvalid("permission custody schema is unsupported")
            records = custody.setdefault("records", {})
            if not isinstance(records, dict):
                raise PermissionSessionCustodyInvalid("permission custody records are corrupt")
            existing = records.get(binding.session_id)
            if isinstance(existing, Mapping):
                holder.append(self._verify_record(existing, binding, presented_token))
                return

            has_permission_state = _state_has_session_material(state, binding.session_id)
            if external_session_exists or has_permission_state:
                raise PermissionSessionCustodyRequired(
                    "an existing session cannot be claimed without its custody token"
                )
            if presented_token:
                raise PermissionSessionCustodyInvalid(
                    "a custody token was presented for a session with no custody record"
                )

            issued_at = _now_iso()
            custody_id = f"permcustody_{uuid4().hex}"
            token_hash = _token_hash(candidate_token, candidate_salt)
            fingerprint = _custody_fingerprint(custody_id, binding, token_hash, epoch=1)
            record = {
                "custody_id": custody_id,
                "binding": binding.to_dict(),
                "binding_fingerprint": binding.fingerprint,
                "token_salt": candidate_salt,
                "token_hash": token_hash,
                "custody_fingerprint": fingerprint,
                "issued_at": issued_at,
                "last_verified_at": issued_at,
                "epoch": 1,
                "revoked": False,
                "metadata": {"plaintext_persisted": False, "owner_unit": "M1-S03A-01"},
            }
            records[binding.session_id] = record
            holder.append(
                PermissionSessionCustodyReceipt(
                    binding=binding,
                    custody_id=custody_id,
                    custody_fingerprint=fingerprint,
                    created=True,
                    verified=True,
                    issued_at=issued_at,
                    epoch=1,
                    token=candidate_token,
                )
            )

        self.state_store.mutate(mutate)
        if not holder:
            raise PermissionSessionCustodyInvalid("permission custody claim produced no receipt")
        return holder[0]

    def verify(
        self,
        binding: PermissionSessionCustodyBinding,
        *,
        presented_token: str,
    ) -> PermissionSessionCustodyReceipt:
        return self.claim(
            binding,
            presented_token=presented_token,
            external_session_exists=True,
        )

    def revoke(self, session_id: str, *, custody_fingerprint: str) -> None:
        if not session_id or not custody_fingerprint:
            raise ValueError("session_id and custody_fingerprint are required")

        def mutate(state: dict[str, Any]) -> None:
            custody = state.get("metadata", {}).get("session_custody", {})
            record = custody.get("records", {}).get(session_id) if isinstance(custody, Mapping) else None
            if not isinstance(record, dict):
                raise KeyError(session_id)
            if not hmac.compare_digest(str(record.get("custody_fingerprint") or ""), custody_fingerprint):
                raise PermissionSessionCustodyInvalid("custody fingerprint mismatch")
            record["revoked"] = True
            record["revoked_at"] = _now_iso()
            record["epoch"] = int(record.get("epoch") or 0) + 1

        self.state_store.mutate(mutate)

    @staticmethod
    def redact_constraints(constraints: Mapping[str, Any]) -> dict[str, Any]:
        return {
            str(key): ("<redacted>" if "custody_token" in str(key).casefold() else value)
            for key, value in constraints.items()
        }

    @staticmethod
    def _verify_record(
        record: Mapping[str, Any],
        binding: PermissionSessionCustodyBinding,
        presented_token: str,
    ) -> PermissionSessionCustodyReceipt:
        if bool(record.get("revoked")):
            raise PermissionSessionCustodyRevoked("permission session custody has been revoked")
        if not presented_token:
            raise PermissionSessionCustodyRequired("session_custody_token is required for an existing session")
        stored_binding = PermissionSessionCustodyBinding(**dict(record.get("binding") or {}))
        if stored_binding != binding:
            raise PermissionSessionCustodyScopeMismatch(
                "session custody does not match the current run/task/workspace"
            )
        salt = str(record.get("token_salt") or "")
        expected_hash = str(record.get("token_hash") or "")
        if not salt or not expected_hash or not hmac.compare_digest(
            expected_hash,
            _token_hash(presented_token, salt),
        ):
            raise PermissionSessionCustodyInvalid("session custody token is invalid")
        custody_id = str(record.get("custody_id") or "")
        epoch = int(record.get("epoch") or 0)
        fingerprint = _custody_fingerprint(custody_id, binding, expected_hash, epoch=epoch)
        if not hmac.compare_digest(str(record.get("custody_fingerprint") or ""), fingerprint):
            raise PermissionSessionCustodyInvalid("session custody record fingerprint mismatch")
        return PermissionSessionCustodyReceipt(
            binding=binding,
            custody_id=custody_id,
            custody_fingerprint=fingerprint,
            created=False,
            verified=True,
            issued_at=str(record.get("issued_at") or ""),
            epoch=epoch,
            token="",
        )


def _state_has_session_material(state: Mapping[str, Any], session_id: str) -> bool:
    overlays = state.get("session_overlays") or {}
    overlay = overlays.get(session_id) if isinstance(overlays, Mapping) else None
    if isinstance(overlay, Mapping):
        if overlay.get("rules") or overlay.get("base_rules") or int(overlay.get("revision") or 0) > 0:
            return True
    requests = state.get("requests") or {}
    if isinstance(requests, Mapping) and any(
        str(item.get("session_id") or "") == session_id
        for item in requests.values()
        if isinstance(item, Mapping)
    ):
        return True
    return any(
        str(item.get("session_id") or "") == session_id
        for item in state.get("decisions") or ()
        if isinstance(item, Mapping)
    )


def _token_hash(token: str, salt: str) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", token.encode("utf-8"), salt.encode("ascii"), 120_000)
    return f"pbkdf2-sha256:{digest.hex()}"


def _custody_fingerprint(
    custody_id: str,
    binding: PermissionSessionCustodyBinding,
    token_hash: str,
    *,
    epoch: int,
) -> str:
    encoded = canonical_arguments_json(
        {
            "custody_id": custody_id,
            "binding": binding.to_dict(),
            "token_hash": token_hash,
            "epoch": epoch,
        }
    )
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
