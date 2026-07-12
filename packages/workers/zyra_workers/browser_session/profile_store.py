from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

from .errors import BrowserProfileCorrupt, BrowserProfileError, BrowserProfileLocked
from .models import BrowserProfileRef, BrowserSessionCommand, browser_id, browser_now
from .store import BrowserStatePort


TRANSIENT_PROFILE_NAMES = {
    "SingletonCookie",
    "SingletonLock",
    "SingletonSocket",
    "lockfile",
    "LOCK",
    "LOG",
    "LOG.old",
}


@dataclass(frozen=True, slots=True)
class BrowserProfilePolicy:
    copy_source: Path | None = None
    profile_directory: str = "Default"
    allow_host_profile_copy: bool = False
    preserve_on_stop: bool = False
    quarantine_corrupt: bool = True
    max_copy_bytes: int = 2 * 1024 * 1024 * 1024
    chrome_args: tuple[str, ...] = ()
    disabled_features: tuple[str, ...] = ()
    enable_features: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.copy_source is not None:
            object.__setattr__(self, "copy_source", Path(self.copy_source).expanduser().resolve())
        if self.max_copy_bytes <= 0:
            raise BrowserProfileError("profile copy size limit must be positive")
        if not self.profile_directory or Path(self.profile_directory).name != self.profile_directory:
            raise BrowserProfileError("profile directory must be a single safe name")


@dataclass(frozen=True, slots=True)
class BrowserProfilePreparation:
    profile: BrowserProfileRef
    created: bool
    copied: bool
    recovered: bool
    chrome_args: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile.to_dict(),
            "created": self.created,
            "copied": self.copied,
            "recovered": self.recovered,
            "chrome_args": list(self.chrome_args),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class BrowserProfileHealth:
    healthy: bool
    profile_id: str
    root_exists: bool
    marker_valid: bool
    directories_valid: bool
    lock_files: tuple[str, ...]
    issues: tuple[str, ...]
    size_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "healthy": self.healthy,
            "profile_id": self.profile_id,
            "root_exists": self.root_exists,
            "marker_valid": self.marker_valid,
            "directories_valid": self.directories_valid,
            "lock_files": list(self.lock_files),
            "issues": list(self.issues),
            "size_bytes": self.size_bytes,
        }


class BrowserProfileStore:
    def __init__(
        self,
        runtime_root: str | Path,
        state_root: str | Path,
        *,
        state_store: BrowserStatePort | None = None,
        disabled: bool = False,
    ) -> None:
        self.runtime_root = Path(runtime_root).expanduser().resolve()
        self.state_root = Path(state_root).expanduser().resolve()
        self.profiles_root = self.runtime_root / "profiles"
        self.quarantine_root = self.runtime_root / "quarantine"
        self.state_store = state_store
        self.disabled = disabled
        self._lock = threading.RLock()
        self._profiles: dict[str, BrowserProfileRef] = {}
        if not disabled:
            self.profiles_root.mkdir(parents=True, exist_ok=True)
            self.quarantine_root.mkdir(parents=True, exist_ok=True)

    def _ensure_available(self) -> None:
        if self.disabled:
            raise BrowserProfileError("browser profile store is disabled", code="browser_profile_store_disabled")

    def prepare(
        self,
        command: BrowserSessionCommand,
        session_id: str,
        *,
        policy: BrowserProfilePolicy | None = None,
    ) -> BrowserProfilePreparation:
        self._ensure_available()
        policy = policy or BrowserProfilePolicy()
        profile_id = self.profile_id_for(command, session_id)
        with self._lock:
            existing = self.get(profile_id)
            if existing is not None:
                health = self.health(existing)
                if health.healthy:
                    return BrowserProfilePreparation(
                        profile=existing,
                        created=False,
                        copied=False,
                        recovered=False,
                        chrome_args=self.chrome_args(existing, command, policy),
                    )
                if not policy.quarantine_corrupt:
                    raise BrowserProfileCorrupt(
                        f"profile {profile_id} is corrupt: {', '.join(health.issues)}",
                        session_id=session_id,
                    )
                self.quarantine(existing, reason="health_check_failed")
            root = self.profiles_root / profile_id
            temp_root = Path(tempfile.mkdtemp(prefix=f".{profile_id}-", dir=self.profiles_root))
            copied = False
            warnings: list[str] = []
            try:
                self._prepare_layout(temp_root)
                if policy.copy_source is not None:
                    if not policy.allow_host_profile_copy:
                        raise BrowserProfileError("host profile copy requires explicit policy authorization")
                    copied = self._copy_source(policy.copy_source, temp_root / "user-data", policy)
                profile = self._build_ref(profile_id, session_id, temp_root)
                self._write_marker(profile, command, policy)
                if root.exists():
                    shutil.rmtree(root)
                os.replace(temp_root, root)
                profile = self._build_ref(profile_id, session_id, root)
                self._write_marker(profile, command, policy)
            except Exception:
                shutil.rmtree(temp_root, ignore_errors=True)
                raise
            self._profiles[profile_id] = profile
            self._persist(profile)
            return BrowserProfilePreparation(
                profile=profile,
                created=True,
                copied=copied,
                recovered=existing is not None,
                chrome_args=self.chrome_args(profile, command, policy),
                warnings=tuple(warnings),
            )

    def profile_id_for(self, command: BrowserSessionCommand, session_id: str) -> str:
        digest = hashlib.sha256(
            f"{command.run_id}\0{command.task_id}\0{command.canonical_session_id}\0{session_id}".encode("utf-8")
        ).hexdigest()[:20]
        return f"brprofile_{digest}"

    def _prepare_layout(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        for name in ("user-data", "cache", "downloads", "temp", "state", "traces"):
            (root / name).mkdir(parents=True, exist_ok=True)

    def _build_ref(self, profile_id: str, session_id: str, root: Path) -> BrowserProfileRef:
        return BrowserProfileRef(
            profile_id=profile_id,
            session_id=session_id,
            root=root,
            cache_dir=root / "cache",
            downloads_dir=root / "downloads",
            temp_dir=root / "temp",
            state_dir=root / "state",
        )

    def _write_marker(
        self,
        profile: BrowserProfileRef,
        command: BrowserSessionCommand,
        policy: BrowserProfilePolicy,
    ) -> None:
        marker = {
            "schema": "zyra.browser-profile.v1",
            "profile_id": profile.profile_id,
            "session_id": profile.session_id,
            "run_id": command.run_id,
            "task_id": command.task_id,
            "canonical_session_id": command.canonical_session_id,
            "workspace_root": str(command.workspace_root),
            "created_at": profile.created_at,
            "updated_at": browser_now(),
            "policy": {
                "preserve_on_stop": policy.preserve_on_stop,
                "profile_directory": policy.profile_directory,
                "host_profile_copied": policy.copy_source is not None,
            },
        }
        path = profile.root / "profile.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(marker, sort_keys=True, indent=2), encoding="utf-8")
        os.replace(temp, path)

    def _copy_source(self, source: Path, destination: Path, policy: BrowserProfilePolicy) -> bool:
        if not source.exists() or not source.is_dir():
            raise BrowserProfileError(f"profile copy source does not exist: {source}")
        size = self._tree_size(source, limit=policy.max_copy_bytes)
        if size > policy.max_copy_bytes:
            raise BrowserProfileError("profile copy source exceeds configured size limit")
        try:
            shutil.copytree(
                source,
                destination,
                dirs_exist_ok=True,
                ignore=self._ignore_transient,
                copy_function=shutil.copy2,
            )
        except PermissionError as error:
            raise BrowserProfileLocked(
                "browser profile contains locked files; close the source browser and retry",
                details={"source": str(source)},
            ) from error
        return True

    @staticmethod
    def _ignore_transient(directory: str, names: list[str]) -> set[str]:
        ignored = set()
        for name in names:
            lower = name.casefold()
            if name in TRANSIENT_PROFILE_NAMES or lower.endswith((".lock", ".journal", "-journal", "-wal", "-shm")):
                ignored.add(name)
        return ignored

    def _tree_size(self, root: Path, *, limit: int) -> int:
        total = 0
        for path in root.rglob("*"):
            if path.is_symlink():
                continue
            try:
                if path.is_file() and path.name not in TRANSIENT_PROFILE_NAMES:
                    total += path.stat().st_size
            except OSError:
                continue
            if total > limit:
                break
        return total

    def chrome_args(
        self,
        profile: BrowserProfileRef,
        command: BrowserSessionCommand,
        policy: BrowserProfilePolicy,
    ) -> tuple[str, ...]:
        args = [
            f"--user-data-dir={profile.root / 'user-data'}",
            f"--disk-cache-dir={profile.cache_dir}",
            "--remote-debugging-port=0",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-sync",
            "--metrics-recording-only",
        ]
        if command.headless:
            args.append("--headless=new")
        if policy.profile_directory:
            args.append(f"--profile-directory={policy.profile_directory}")
        if policy.disabled_features:
            args.append(f"--disable-features={','.join(dict.fromkeys(policy.disabled_features))}")
        if policy.enable_features:
            args.append(f"--enable-features={','.join(dict.fromkeys(policy.enable_features))}")
        args.extend(policy.chrome_args)
        return self._merge_feature_args(args)

    @staticmethod
    def _merge_feature_args(args: list[str]) -> tuple[str, ...]:
        disabled: list[str] = []
        enabled: list[str] = []
        others: list[str] = []
        for arg in args:
            if arg.startswith("--disable-features="):
                disabled.extend(item for item in arg.split("=", 1)[1].split(",") if item)
            elif arg.startswith("--enable-features="):
                enabled.extend(item for item in arg.split("=", 1)[1].split(",") if item)
            elif arg not in others:
                others.append(arg)
        if disabled:
            others.append(f"--disable-features={','.join(dict.fromkeys(disabled))}")
        if enabled:
            others.append(f"--enable-features={','.join(dict.fromkeys(enabled))}")
        return tuple(others)

    def get(self, profile_id: str) -> BrowserProfileRef | None:
        with self._lock:
            cached = self._profiles.get(profile_id)
            if cached is not None:
                return cached
            value = self.state_store.get_profile(profile_id) if self.state_store and hasattr(self.state_store, "get_profile") else None
            if isinstance(value, Mapping):
                profile = BrowserProfileRef(
                    profile_id=str(value["profile_id"]),
                    session_id=str(value["session_id"]),
                    root=Path(str(value["root"])),
                    cache_dir=Path(str(value["cache_dir"])),
                    downloads_dir=Path(str(value["downloads_dir"])),
                    temp_dir=Path(str(value["temp_dir"])),
                    state_dir=Path(str(value["state_dir"])),
                    healthy=bool(value.get("healthy", True)),
                    generation=int(value.get("generation") or 1),
                    created_at=str(value.get("created_at") or ""),
                    updated_at=str(value.get("updated_at") or ""),
                    issues=tuple(str(item) for item in value.get("issues", ())),
                )
                self._profiles[profile_id] = profile
                return profile
            root = self.profiles_root / profile_id
            if not root.exists():
                return None
            marker = self._read_marker(root)
            profile = self._build_ref(profile_id, str(marker.get("session_id") or ""), root)
            self._profiles[profile_id] = profile
            return profile

    def _read_marker(self, root: Path) -> dict[str, Any]:
        path = root / "profile.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise BrowserProfileCorrupt(f"profile marker cannot be read: {error}") from error
        if value.get("schema") != "zyra.browser-profile.v1":
            raise BrowserProfileCorrupt("profile marker schema mismatch")
        return value

    def health(self, profile: BrowserProfileRef) -> BrowserProfileHealth:
        issues: list[str] = []
        root_exists = profile.root.exists() and profile.root.is_dir()
        marker_valid = False
        if root_exists:
            try:
                marker = self._read_marker(profile.root)
                marker_valid = marker.get("profile_id") == profile.profile_id
            except BrowserProfileCorrupt as error:
                issues.append(str(error))
        else:
            issues.append("profile root is missing")
        required = (profile.cache_dir, profile.downloads_dir, profile.temp_dir, profile.state_dir)
        directories_valid = all(path.exists() and path.is_dir() for path in required)
        if not directories_valid:
            issues.append("one or more profile runtime directories are missing")
        locks = tuple(
            str(path.relative_to(profile.root))
            for path in profile.root.rglob("*")
            if path.is_file() and (path.name in TRANSIENT_PROFILE_NAMES or path.name.casefold().endswith(".lock"))
        ) if root_exists else ()
        if locks:
            issues.append("profile contains transient lock files")
        size = self._tree_size(profile.root, limit=2**63 - 1) if root_exists else 0
        return BrowserProfileHealth(
            healthy=root_exists and marker_valid and directories_valid and not locks,
            profile_id=profile.profile_id,
            root_exists=root_exists,
            marker_valid=marker_valid,
            directories_valid=directories_valid,
            lock_files=locks,
            issues=tuple(issues),
            size_bytes=size,
        )

    def quarantine(self, profile: BrowserProfileRef, *, reason: str) -> Path:
        self._ensure_available()
        with self._lock:
            if not profile.root.exists():
                return self.quarantine_root / f"{profile.profile_id}-missing"
            suffix = browser_now().replace(":", "").replace("-", "").replace(".", "")
            destination = self.quarantine_root / f"{profile.profile_id}-{suffix}"
            os.replace(profile.root, destination)
            record = {
                "profile_id": profile.profile_id,
                "session_id": profile.session_id,
                "reason": reason,
                "quarantined_at": browser_now(),
            }
            (destination / "quarantine.json").write_text(json.dumps(record, sort_keys=True, indent=2), encoding="utf-8")
            self._profiles.pop(profile.profile_id, None)
            return destination

    def cleanup(self, profile_id: str, *, preserve: bool = False) -> bool:
        self._ensure_available()
        with self._lock:
            profile = self.get(profile_id)
            if profile is None:
                return False
            if preserve:
                updated = replace(profile, updated_at=browser_now())
                self._profiles[profile_id] = updated
                self._persist(updated)
                return True
            shutil.rmtree(profile.root, ignore_errors=False)
            self._profiles.pop(profile_id, None)
            return True

    def _persist(self, profile: BrowserProfileRef) -> None:
        if self.state_store is not None and hasattr(self.state_store, "put_profile"):
            self.state_store.put_profile(profile.profile_id, profile.to_dict())

    def list_profiles(self) -> tuple[BrowserProfileRef, ...]:
        self._ensure_available()
        with self._lock:
            for root in self.profiles_root.iterdir():
                if root.is_dir() and root.name not in self._profiles:
                    try:
                        marker = self._read_marker(root)
                        self._profiles[root.name] = self._build_ref(root.name, str(marker.get("session_id") or ""), root)
                    except BrowserProfileCorrupt:
                        continue
            return tuple(sorted(self._profiles.values(), key=lambda item: item.profile_id))

    def allocate_download_path(self, profile_id: str, suggested_name: str) -> Path:
        profile = self.get(profile_id)
        if profile is None:
            raise BrowserProfileError(f"profile {profile_id} does not exist")
        name = suggested_name.replace("\\", "/").split("/")[-1].replace("\x00", "").strip()
        if not name or name in {".", ".."}:
            name = f"download-{browser_id('file')}"
        destination = (profile.downloads_dir / name).resolve()
        try:
            destination.relative_to(profile.downloads_dir.resolve())
        except ValueError as error:
            raise BrowserProfileError("download path escaped profile directory") from error
        if not destination.exists():
            return destination
        stem = destination.stem
        suffix = destination.suffix
        for index in range(1, 10000):
            candidate = destination.with_name(f"{stem}-{index}{suffix}")
            if not candidate.exists():
                return candidate
        raise BrowserProfileError("download filename collision limit exceeded")

    def save_storage_state(self, profile_id: str, value: Mapping[str, Any]) -> Path:
        profile = self.get(profile_id)
        if profile is None:
            raise BrowserProfileError(f"profile {profile_id} does not exist")
        sanitized = {
            "cookies": list(value.get("cookies") or []),
            "origins": list(value.get("origins") or []),
            "saved_at": browser_now(),
        }
        target = profile.state_dir / "storage-state.json"
        backup = profile.state_dir / "storage-state.backup.json"
        temp = profile.state_dir / ".storage-state.tmp"
        if target.exists():
            shutil.copy2(target, backup)
        temp.write_text(json.dumps(sanitized, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        os.replace(temp, target)
        return target

    def load_storage_state(self, profile_id: str) -> dict[str, Any]:
        profile = self.get(profile_id)
        if profile is None:
            raise BrowserProfileError(f"profile {profile_id} does not exist")
        target = profile.state_dir / "storage-state.json"
        backup = profile.state_dir / "storage-state.backup.json"
        for candidate in (target, backup):
            if not candidate.exists():
                continue
            try:
                value = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(value, dict) and isinstance(value.get("cookies", []), list) and isinstance(value.get("origins", []), list):
                return value
        return {"cookies": [], "origins": []}

    def clean_transient_files(self, profile_id: str) -> tuple[str, ...]:
        profile = self.get(profile_id)
        if profile is None:
            return ()
        removed: list[str] = []
        for root in (profile.temp_dir, profile.cache_dir):
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                try:
                    path.unlink()
                    removed.append(str(path.relative_to(profile.root)))
                except OSError:
                    continue
        return tuple(removed)

    def runtime_environment(self, profile_id: str) -> dict[str, str]:
        profile = self.get(profile_id)
        if profile is None:
            raise BrowserProfileError(f"profile {profile_id} does not exist")
        return {
            "HOME": str(profile.root),
            "XDG_CACHE_HOME": str(profile.cache_dir),
            "TEMP": str(profile.temp_dir),
            "TMP": str(profile.temp_dir),
            "ZYRA_BROWSER_PROFILE_ID": profile.profile_id,
            "ZYRA_BROWSER_DOWNLOADS": str(profile.downloads_dir),
        }
