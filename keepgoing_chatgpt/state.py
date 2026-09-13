"""Atomic, mutex-protected durable retry state."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import ctypes
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
from typing import Any, Callable, Iterator, Mapping

from .models import RetryItem, RetryState


SCHEMA_VERSION = 1
TERMINAL_STATES = frozenset({RetryState.COMPLETED, RetryState.CANCELLED, RetryState.NEEDS_ATTENTION})
NON_TERMINAL_STATES = frozenset(set(RetryState) - TERMINAL_STATES)


class StateStoreError(RuntimeError):
    pass


class StateCorruptError(StateStoreError):
    pass


class StateSchemaError(StateStoreError):
    pass


class StateStoreLocked(StateStoreError):
    pass


@dataclass(frozen=True)
class RetryStoreState:
    schema_version: int = SCHEMA_VERSION
    observation_watermark_ms: int = 0
    queue: tuple[RetryItem, ...] = ()
    dedupe_ledger: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise StateSchemaError(f"unsupported state schema version: {self.schema_version}")
        if self.observation_watermark_ms < 0:
            raise ValueError("observation_watermark_ms must not be negative")
        if any(not isinstance(item, RetryItem) for item in self.queue):
            raise TypeError("queue must contain RetryItem records")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "observation_watermark_ms": self.observation_watermark_ms,
            "queue": [_item_to_dict(item) for item in self.queue],
            "dedupe_ledger": [dict(entry) for entry in self.dedupe_ledger],
        }


class _ThreadMutex:
    _registry_lock = threading.Lock()
    _registry: dict[str, threading.Lock] = {}

    def __init__(self, name: str) -> None:
        with self._registry_lock:
            self._lock = self._registry.setdefault(name, threading.Lock())

    def acquire(self, timeout_ms: int) -> bool:
        if timeout_ms < 0:
            return self._lock.acquire()
        return self._lock.acquire(timeout=timeout_ms / 1000)

    def release(self) -> None:
        self._lock.release()

    def close(self) -> None:
        return None


class _WindowsMutex:
    WAIT_OBJECT_0 = 0x00000000
    WAIT_ABANDONED = 0x00000080
    WAIT_TIMEOUT = 0x00000102
    INFINITE = 0xFFFFFFFF

    def __init__(self, name: str) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
        kernel32.ReleaseMutex.restype = ctypes.c_bool
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_bool
        self._kernel32 = kernel32
        self._local_guard = _ThreadMutex(name + "-in-process")
        self._handle = kernel32.CreateMutexW(None, False, name)
        if not self._handle:
            raise OSError(ctypes.get_last_error(), "CreateMutexW failed")

    def acquire(self, timeout_ms: int) -> bool:
        if not self._local_guard.acquire(timeout_ms):
            return False
        timeout = self.INFINITE if timeout_ms < 0 else max(0, timeout_ms)
        result = self._kernel32.WaitForSingleObject(self._handle, timeout)
        if result in (self.WAIT_OBJECT_0, self.WAIT_ABANDONED):
            return True
        if result == self.WAIT_TIMEOUT:
            self._local_guard.release()
            return False
        self._local_guard.release()
        raise OSError(ctypes.get_last_error(), "WaitForSingleObject failed")

    def release(self) -> None:
        try:
            if not self._kernel32.ReleaseMutex(self._handle):
                raise OSError(ctypes.get_last_error(), "ReleaseMutex failed")
        finally:
            self._local_guard.release()

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def _make_mutex(name: str) -> Any:
    normalized = name if "\\" in name else f"Local\\{name}"
    return _WindowsMutex(normalized) if os.name == "nt" else _ThreadMutex(normalized)


class NamedMutex:
    """Small public lifecycle wrapper around the platform named mutex."""

    def __init__(self, name: str) -> None:
        self._mutex = _make_mutex(name)

    def acquire(self, timeout_ms: int = -1) -> bool:
        return self._mutex.acquire(timeout_ms)

    def release(self) -> None:
        self._mutex.release()

    def close(self) -> None:
        self._mutex.close()


class RetryStateStore:
    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        now_ms: Callable[[], int] | None = None,
        mutex_name: str = "KeepGoing-ChatGPT-AutoResume-State",
        replace: Callable[[str, str], None] = os.replace,
    ) -> None:
        if path is None:
            local_app_data = os.environ.get("LOCALAPPDATA")
            if not local_app_data:
                local_app_data = os.path.join(os.path.expanduser("~"), "AppData", "Local")
            path = Path(local_app_data) / "KeepGoing" / "chatgpt-auto-resume-state.json"
        self.path = Path(path)
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._mutex = NamedMutex(mutex_name)
        self._replace = replace
        self._fail_closed = False

    @contextmanager
    def lock(self, *, timeout_ms: int = -1) -> Iterator[None]:
        if not self._mutex.acquire(timeout_ms):
            raise StateStoreLocked(f"state store mutex is busy: {self.path}")
        try:
            yield
        finally:
            self._mutex.release()

    def load(self) -> RetryStoreState:
        with self.lock():
            if not self.path.exists():
                state = RetryStoreState(observation_watermark_ms=int(self.now_ms()))
                self._write_locked(state)
                return state
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                quarantine = self._quarantine()
                self._fail_closed = True
                raise StateCorruptError(f"state JSON is corrupt; quarantined copy: {quarantine}") from exc
            try:
                state = _state_from_dict(raw)
            except StateSchemaError:
                self._fail_closed = True
                raise
            except (TypeError, ValueError, KeyError, StateStoreError) as exc:
                quarantine = self._quarantine()
                self._fail_closed = True
                raise StateCorruptError(f"state contents are invalid; quarantined copy: {quarantine}") from exc
            compacted = self.compact(state)
            if compacted != state:
                self._write_locked(compacted)
            return compacted

    def save(self, state: RetryStoreState) -> None:
        if not isinstance(state, RetryStoreState):
            raise TypeError("state must be a RetryStoreState")
        if self._fail_closed:
            raise StateCorruptError("state store is fail-closed after a corrupt or unsupported state")
        with self.lock():
            self._write_locked(state)

    def compact(self, state: RetryStoreState, *, now_ms: int | None = None) -> RetryStoreState:
        now = int(self.now_ms() if now_ms is None else now_ms)
        terminal_cutoff = now - 7 * 24 * 3600 * 1000
        ledger_cutoff = now - 30 * 24 * 3600 * 1000
        kept: list[RetryItem] = []
        ledger: dict[str, dict[str, Any]] = {}
        for entry in state.dedupe_ledger:
            key = entry.get("key")
            seen_at = entry.get("seen_at_ms")
            if isinstance(key, str) and isinstance(seen_at, (int, float)) and seen_at >= ledger_cutoff:
                ledger[key] = {"key": key, "seen_at_ms": int(seen_at)}
        for item in state.queue:
            if item.state in TERMINAL_STATES and item.updated_at_ms < terminal_cutoff:
                ledger[item.key] = {"key": item.key, "seen_at_ms": item.updated_at_ms}
            else:
                kept.append(item)
        return RetryStoreState(
            schema_version=state.schema_version,
            observation_watermark_ms=state.observation_watermark_ms,
            queue=tuple(kept),
            dedupe_ledger=tuple(ledger.values()),
        )

    def _write_locked(self, state: RetryStoreState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(state.to_dict(), stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            self._replace(temp_name, str(self.path))
        except Exception:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise

    def _quarantine(self) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        destination = self.path.with_name(f"{self.path.name}.corrupt-{timestamp}")
        try:
            shutil.copy2(self.path, destination)
        except OSError as exc:
            raise StateCorruptError("state is corrupt and could not be quarantined") from exc
        return destination


def _item_to_dict(item: RetryItem) -> dict[str, Any]:
    return {
        "key": item.key,
        "thread_id": item.thread_id,
        "host_id": item.host_id,
        "failed_turn_id": item.failed_turn_id,
        "failed_at_ms": item.failed_at_ms,
        "reset_at_epoch": item.reset_at_epoch,
        "window_duration_minutes": item.window_duration_minutes,
        "prompt": item.prompt,
        "state": item.state.value,
        "dispatch_attempts": item.dispatch_attempts,
        "dispatch_confirmed": item.dispatch_confirmed,
        "last_error": item.last_error,
        "created_at_ms": item.created_at_ms,
        "updated_at_ms": item.updated_at_ms,
    }


def _state_from_dict(raw: Any) -> RetryStoreState:
    if not isinstance(raw, Mapping):
        raise StateCorruptError("state root must be an object")
    schema_version = raw.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        raise StateSchemaError(f"unsupported state schema version: {schema_version}")
    queue_raw = raw.get("queue")
    if not isinstance(queue_raw, list):
        raise StateCorruptError("state queue must be a list")
    items = tuple(_item_from_dict(item) for item in queue_raw)
    ledger_raw = raw.get("dedupe_ledger", [])
    if not isinstance(ledger_raw, list):
        raise StateCorruptError("state dedupe ledger must be a list")
    ledger = tuple(entry for entry in ledger_raw if isinstance(entry, Mapping))
    return RetryStoreState(
        schema_version=schema_version,
        observation_watermark_ms=int(raw.get("observation_watermark_ms", 0)),
        queue=items,
        dedupe_ledger=ledger,
    )


def _item_from_dict(raw: Any) -> RetryItem:
    if not isinstance(raw, Mapping):
        raise StateCorruptError("queue entry must be an object")
    try:
        item = RetryItem(
            thread_id=str(raw["thread_id"]),
            host_id=str(raw.get("host_id", "local")),
            failed_turn_id=str(raw["failed_turn_id"]),
            failed_at_ms=int(raw["failed_at_ms"]),
            reset_at_epoch=int(raw["reset_at_epoch"]),
            window_duration_minutes=int(raw.get("window_duration_minutes", 300)),
            prompt=str(raw.get("prompt", "keep going")),
            state=RetryState(str(raw["state"])),
            dispatch_attempts=int(raw.get("dispatch_attempts", 0)),
            dispatch_confirmed=raw.get("dispatch_confirmed", False),
            last_error=raw.get("last_error"),
            created_at_ms=int(raw.get("created_at_ms", 0)),
            updated_at_ms=int(raw.get("updated_at_ms", 0)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise StateCorruptError("queue entry is invalid") from exc
    stored_key = raw.get("key")
    if stored_key is not None and stored_key != item.key:
        raise StateCorruptError("queue entry key does not match its task identity")
    return item


__all__ = [
    "NON_TERMINAL_STATES",
    "NamedMutex",
    "RetryStateStore",
    "RetryStoreState",
    "SCHEMA_VERSION",
    "StateCorruptError",
    "StateSchemaError",
    "StateStoreError",
    "StateStoreLocked",
    "TERMINAL_STATES",
]
