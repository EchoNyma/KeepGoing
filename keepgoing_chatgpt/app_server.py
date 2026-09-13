"""Read-only adapter for the bundled Codex App Server."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
import re
from typing import Any, Iterable, Mapping, Protocol, Sequence

from .jsonrpc import JsonRpcClient
from .models import FailedTask, RateLimitBucket


class AppServerError(RuntimeError):
    """Base class for App Server contract and transport failures."""


class AppServerContractError(AppServerError):
    pass


class AppServerMethodDenied(AppServerError):
    pass


class _Rpc(Protocol):
    def request(self, method: str, params: Any = None, timeout: float | None = None) -> Any: ...

    def send_notification(self, method: str, params: Any = None) -> None: ...


@dataclass(frozen=True)
class AppServerInfo:
    version: str
    codex_home: str


@dataclass(frozen=True)
class TaskTurn:
    turn_id: str
    status: str
    created_at_ms: int
    completed_at_ms: int | None = None
    error_class: str | None = None
    error_message: str = ""


@dataclass(frozen=True)
class TaskRecord:
    thread_id: str
    host_id: str
    kind: str
    status: str
    turns: tuple[TaskTurn, ...] = ()

    @property
    def latest_turn_id(self) -> str | None:
        return self.turns[-1].turn_id if self.turns else None


@dataclass(frozen=True)
class _TaskPage:
    record: TaskRecord
    next_cursor: str | None


READ_METHODS = frozenset(
    {
        "initialize",
        "account/rateLimits/read",
        "thread/list",
        "thread/read",
        "thread/get",
    }
)

_SERVER_USAGE_CLASSES = frozenset(
    {
        "usage_limit",
        "usage_limit_exceeded",
        "usage-limit",
        "usage limit",
        "rate_limit",
        "rate-limit",
        "rate limit",
        "quota_exceeded",
        "quota-exceeded",
        "quota exceeded",
    }
)
_WEEKLY_RE = re.compile(r"(?i)\b(?:weekly|week|woche|wöchentlich|7[ -]?day|7[ -]?tage|7[ -]?tägig)\b")
_ENGLISH_MESSAGE_RE = re.compile(
    r"(?is)^\s*(?:you['’]ve\s+(?:hit|reached)\s+your\s+(?:current\s+)?usage\s+limit\b.*\btry\s+again\s+(?:at|after)\b.*|rate\s+limit\s+reached\.\s*try\s+again\s+(?:in|at|after)\b.*|usage\s+limit\s+exceeded\s+for\s+this\s+model\.?)\s*$"
)
_GERMAN_MESSAGE_RE = re.compile(
    r"(?is)^\s*(?:nutzungslimit\s+erreicht\.\s*wieder\s+verfügbar\s+in\s+\d+\s*(?:sekunden?|minuten?|stunden?)\.?|sie\s+haben\s+ihr\s+(?:aktuelles\s+)?(?:nutzungs)?limit\b.*versuchen\s+sie\s+es\b.*|limit\s+erreicht\.\s*versuche\s+es\s+nach\b.*)\s*$"
)


class AppServerClient:
    """Strict read-only façade over a JSON-RPC App Server transport."""

    def __init__(
        self,
        rpc: _Rpc,
        *,
        bundled_version: str | None = None,
        expected_codex_home: str | None = None,
        request_timeout: float = 10.0,
    ) -> None:
        self.rpc = rpc
        self.bundled_version = bundled_version
        self.expected_codex_home = expected_codex_home
        self.request_timeout = request_timeout
        self.info: AppServerInfo | None = None

    def request(self, method: str, params: Any = None) -> Any:
        if method not in READ_METHODS:
            raise AppServerMethodDenied(f"App Server method is not allowlisted: {method}")
        return self.rpc.request(method, params, timeout=self.request_timeout)

    def close(self) -> None:
        close = getattr(self.rpc, "close", None)
        if callable(close):
            close()

    def initialize(self) -> AppServerInfo:
        result = self.request(
            "initialize",
            {
                "clientInfo": {"name": "keepgoing", "title": "KeepGoing", "version": "1.0.0"},
                "capabilities": {},
            },
        )
        if not isinstance(result, Mapping):
            raise AppServerContractError("initialize returned a non-object result")
        version = _first_string(result, "version", "codexVersion", "codex_version")
        if not version:
            user_agent = _first_string(result, "userAgent", "user_agent") or ""
            version = _version_from_user_agent(user_agent)
        codex_home = _first_string(result, "codexHome", "codex_home", "home")
        if not version or not codex_home:
            raise AppServerContractError("initialize did not expose a Codex version and home")
        if self.bundled_version and _normalize_version(version) != _normalize_version(self.bundled_version):
            raise AppServerContractError("bundled Codex version does not match App Server version")
        if self.expected_codex_home and _canonical_home(codex_home) != _canonical_home(self.expected_codex_home):
            raise AppServerContractError("App Server Codex home does not match the explicit runtime binding")
        self.info = AppServerInfo(version=version, codex_home=codex_home)
        self.rpc.send_notification("initialized")
        return self.info

    def read_rate_limits(self) -> tuple[RateLimitBucket, ...]:
        result = self.request("account/rateLimits/read", {})
        return _parse_rate_limit_buckets(result)

    def read_five_hour_bucket(self) -> RateLimitBucket | None:
        for bucket in self.read_rate_limits():
            if bucket.name == "primary" and bucket.window_duration_minutes == 300:
                return bucket
        return None

    def list_local_tasks(self, *, limit: int = 100) -> tuple[TaskRecord, ...]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        tasks: list[TaskRecord] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            params: dict[str, Any] = {"limit": limit}
            if cursor:
                params["cursor"] = cursor
            result = self.request("thread/list", params)
            entries, next_cursor = _parse_task_list_page(result)
            remaining = limit - len(tasks)
            tasks.extend(entries[:remaining])
            if len(tasks) >= limit:
                break
            if not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return tuple(task for task in tasks if task.kind == "codex" and task.host_id == "local")

    def read_task_history(
        self,
        thread_id: str,
        *,
        expected_host_id: str | None = None,
        expected_kind: str | None = None,
    ) -> TaskRecord:
        if not thread_id:
            raise ValueError("thread_id must not be empty")
        if expected_host_id is not None and not expected_host_id:
            raise ValueError("expected_host_id must not be empty")
        if expected_kind is not None and not expected_kind:
            raise ValueError("expected_kind must not be empty")
        all_turns: list[TaskTurn] = []
        task: TaskRecord | None = None
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            params: dict[str, Any] = {"threadId": thread_id, "includeTurns": True}
            if cursor:
                params["cursor"] = cursor
            result = self.request("thread/read", params)
            page = _parse_task_page(
                result,
                thread_id,
                expected_host_id=expected_host_id,
                expected_kind=expected_kind,
            )
            task = TaskRecord(
                thread_id=page.record.thread_id,
                host_id=page.record.host_id,
                kind=page.record.kind,
                status=page.record.status,
                turns=tuple(all_turns + list(page.record.turns)),
            )
            all_turns = list(task.turns)
            if not page.next_cursor or page.next_cursor in seen_cursors:
                break
            seen_cursors.add(page.next_cursor)
            cursor = page.next_cursor
        if task is None:
            raise AppServerContractError("thread/read returned no task page")
        if task.host_id != "local" or task.kind != "codex":
            raise AppServerContractError("thread/read returned a non-local Codex task")
        return task

    def find_five_hour_failures(self, *, observation_watermark_ms: int) -> tuple[FailedTask, ...]:
        failures: list[FailedTask] = []
        seen: set[str] = set()
        for task in self.list_local_tasks():
            history = self.read_task_history(
                task.thread_id,
                expected_host_id=task.host_id,
                expected_kind=task.kind,
            )
            turns = history.turns
            latest_turn_id = turns[-1].turn_id if turns else None
            for turn in turns:
                if not classify_failed_turn(turn):
                    continue
                failed_at_ms = turn.completed_at_ms if turn.completed_at_ms is not None else turn.created_at_ms
                if failed_at_ms <= observation_watermark_ms:
                    continue
                failure = FailedTask(
                    thread_id=history.thread_id,
                    host_id=history.host_id,
                    failed_turn_id=turn.turn_id,
                    failed_at_ms=failed_at_ms,
                    message=turn.error_message,
                    error_class=turn.error_class,
                    task_status=history.status,
                    latest_turn_id=latest_turn_id,
                )
                if failure.queue_key not in seen:
                    seen.add(failure.queue_key)
                    failures.append(failure)
        return tuple(failures)


def classify_failed_turn(turn: TaskTurn) -> bool:
    if turn.status.lower() != "failed":
        return False
    error_class = (turn.error_class or "").strip().lower().replace("_", " ").replace("-", " ")
    message = (turn.error_message or "").strip()
    if _WEEKLY_RE.search(error_class) or _WEEKLY_RE.search(message):
        return False
    error_token = re.sub(r"[^a-z0-9]+", "", error_class)
    usage_tokens = {re.sub(r"[^a-z0-9]+", "", value.lower()) for value in _SERVER_USAGE_CLASSES}
    if error_token in usage_tokens:
        return True
    return bool(_ENGLISH_MESSAGE_RE.match(message) or _GERMAN_MESSAGE_RE.match(message))


def _parse_rate_limit_buckets(result: Any) -> tuple[RateLimitBucket, ...]:
    if not isinstance(result, Mapping):
        raise AppServerContractError("rate-limit response is not an object")
    container: Any = result.get("rateLimits", result.get("rate_limits", result.get("buckets", result)))
    if isinstance(container, Mapping):
        entries: list[tuple[str, Any]] = list(container.items())
    elif isinstance(container, list):
        entries = [(str(item.get("name", item.get("id", ""))), item) for item in container if isinstance(item, Mapping)]
    else:
        raise AppServerContractError("rate-limit response has no bucket collection")

    buckets: list[RateLimitBucket] = []
    for name, raw in entries:
        if not isinstance(raw, Mapping):
            continue
        duration = _first_number(raw, "windowDurationMins", "window_duration_minutes", "windowDurationMinutes")
        resets = _first_number(raw, "resetsAt", "resets_at", "resetAt")
        if duration is None or resets is None:
            continue
        used = _first_number(raw, "usedPercent", "used_percent")
        reached_value = raw.get("isReached", raw.get("reached"))
        reached = bool(reached_value) if reached_value is not None else (used is None or used >= 100)
        buckets.append(
            RateLimitBucket(
                window_duration_minutes=int(duration),
                resets_at_epoch=int(resets),
                reached=reached,
                name=name or "unknown",
                used_percent=used,
            )
        )
    return tuple(buckets)


def _parse_task_list_page(result: Any) -> tuple[tuple[TaskRecord, ...], str | None]:
    if isinstance(result, list):
        raw_entries = result
        next_cursor = None
    elif isinstance(result, Mapping):
        raw_entries = result.get("data", result.get("threads", result.get("items", [])))
        next_cursor = _cursor(result)
    else:
        raise AppServerContractError("thread/list response is invalid")
    if not isinstance(raw_entries, list):
        raise AppServerContractError("thread/list data is not a list")
    tasks_list: list[TaskRecord] = []
    for item in raw_entries:
        if not isinstance(item, Mapping):
            raise AppServerContractError("thread/list contains a malformed task entry")
        tasks_list.append(_parse_task_record(item, scope_host_id="local", scope_kind="codex"))
    tasks = tuple(tasks_list)
    return tasks, next_cursor


def _parse_task_page(
    result: Any,
    requested_thread_id: str,
    *,
    expected_host_id: str | None = None,
    expected_kind: str | None = None,
) -> _TaskPage:
    if not isinstance(result, Mapping):
        raise AppServerContractError("thread/read response is invalid")
    raw_thread = result.get("thread", result)
    if not isinstance(raw_thread, Mapping):
        raise AppServerContractError("thread/read did not include a thread")
    task = _parse_task_record(raw_thread, expected_host_id=expected_host_id, expected_kind=expected_kind)
    if task.thread_id != requested_thread_id:
        raise AppServerContractError("thread/read task ID did not match the requested task")
    turns_raw = raw_thread.get("turns", result.get("turns", []))
    if not isinstance(turns_raw, list):
        raise AppServerContractError("thread/read turns are not a list")
    turns: list[TaskTurn] = []
    for turn in turns_raw:
        if not isinstance(turn, Mapping):
            raise AppServerContractError("thread/read contains a malformed turn")
        turns.append(_parse_task_turn(turn))
    return _TaskPage(
        record=TaskRecord(task.thread_id, task.host_id, task.kind, task.status, tuple(turns)),
        next_cursor=_cursor(result) or _cursor(raw_thread),
    )


def _parse_task_record(
    raw: Mapping[str, Any],
    *,
    expected_host_id: str | None = None,
    expected_kind: str | None = None,
    scope_host_id: str | None = None,
    scope_kind: str | None = None,
) -> TaskRecord:
    thread_id = _first_string(raw, "id", "threadId", "thread_id")
    if not thread_id:
        raise AppServerContractError("task has no thread ID")
    host_id = _first_string(raw, "hostId", "host_id", "host")
    if not host_id:
        if expected_host_id is not None:
            host_id = expected_host_id
        elif scope_host_id is not None:
            host_id = scope_host_id
        else:
            raise AppServerContractError("task has no host ID")
    if expected_host_id is not None and host_id != expected_host_id:
        raise AppServerContractError("task host ID did not match the validated task identity")
    if scope_host_id is not None and host_id != scope_host_id:
        raise AppServerContractError("task host ID did not match the App Server list scope")
    kind_value = _first_string(raw, "kind", "type")
    if not kind_value:
        if expected_kind is not None:
            kind_value = expected_kind
        elif scope_kind is not None:
            kind_value = scope_kind
        else:
            raise AppServerContractError("task has no kind")
    if expected_kind is not None and kind_value.lower() != expected_kind.lower():
        raise AppServerContractError("task kind did not match the validated task identity")
    if scope_kind is not None and kind_value.lower() != scope_kind.lower():
        raise AppServerContractError("task kind did not match the App Server list scope")
    kind = kind_value.lower()
    status = (_first_string(raw, "status", "state") or "unknown").lower()
    raw_turns = raw.get("turns", [])
    if not isinstance(raw_turns, list):
        raise AppServerContractError("task turns are not a list")
    turns: list[TaskTurn] = []
    for turn in raw_turns:
        if not isinstance(turn, Mapping):
            raise AppServerContractError("task contains a malformed turn")
        turns.append(_parse_task_turn(turn))
    return TaskRecord(thread_id=thread_id, host_id=host_id, kind=kind, status=status, turns=tuple(turns))


def _parse_task_turn(raw: Mapping[str, Any]) -> TaskTurn:
    turn_id = _first_string(raw, "id", "turnId", "turn_id")
    if not turn_id:
        raise AppServerContractError("turn has no ID")
    status = (_first_string(raw, "status", "state") or "unknown").lower()
    created = _first_number(raw, "createdAt", "created_at_ms", "createdAtMs", "startedAt")
    if created is None:
        created = 0
    completed = _first_number(raw, "completedAt", "completed_at_ms", "completedAtMs", "finishedAt")
    error_class: str | None = None
    error_message = _first_string(raw, "errorMessage", "error_message") or ""
    error = raw.get("error")
    if isinstance(error, Mapping):
        error_class = _first_string(error, "type", "errorType", "code", "class")
        error_message = _first_string(error, "message", "detail") or error_message
        error_info = error.get("codexErrorInfo")
        if isinstance(error_info, str) and error_info:
            error_class = error_class or error_info
        elif isinstance(error_info, Mapping):
            error_class = error_class or _first_string(error_info, "type", "errorType", "code", "class")
    elif isinstance(error, str):
        error_message = error
    return TaskTurn(
        turn_id=turn_id,
        status=status,
        created_at_ms=_timestamp_ms(created),
        completed_at_ms=_timestamp_ms(completed) if completed is not None else None,
        error_class=error_class,
        error_message=error_message,
    )


def _cursor(value: Mapping[str, Any]) -> str | None:
    result = value.get("nextCursor", value.get("next_cursor", value.get("cursor")))
    return str(result) if result not in (None, "") else None


def _first_string(value: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        item = value.get(key)
        if isinstance(item, str) and item:
            return item
    return None


def _first_number(value: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        item = value.get(key)
        if isinstance(item, bool):
            continue
        if isinstance(item, (int, float)):
            return float(item)
        if isinstance(item, str):
            try:
                return float(item)
            except ValueError:
                parsed = _parse_iso_epoch(item)
                if parsed is not None:
                    return parsed
    return None


def _timestamp_ms(value: float) -> int:
    return int(value if value >= 10**12 else value * 1000)


def _parse_iso_epoch(value: str) -> float | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _normalize_version(value: str) -> str:
    match = re.search(r"\d+\.\d+\.\d+(?:[-+][\w.]+)?", value)
    return match.group(0) if match else value.strip()


def _canonical_home(value: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(os.path.expandvars(value))))


def _version_from_user_agent(value: str) -> str | None:
    match = re.search(r"\d+\.\d+\.\d+(?:[-+][\w.]+)?", value)
    return match.group(0) if match else None


__all__ = [
    "AppServerClient",
    "AppServerContractError",
    "AppServerError",
    "AppServerInfo",
    "AppServerMethodDenied",
    "READ_METHODS",
    "TaskRecord",
    "TaskTurn",
    "classify_failed_turn",
]
