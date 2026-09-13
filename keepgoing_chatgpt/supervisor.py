"""Deterministic, short-tick task auto-resume supervisor."""

from __future__ import annotations

from dataclasses import dataclass, replace
import time
from typing import Any, Callable, Iterable, Mapping, Protocol

from .app_server import TaskTurn, classify_failed_turn
from .desktop_bridge import (
    DesktopBridgeError,
    DesktopBridgeToolError,
    DesktopBridgeTransportUncertain,
)
from .models import FailedTask, RateLimitBucket, RetryItem, RetryState
from .state import RetryStateStore, RetryStoreState


OBSERVATION_SETTLE_LAG_MS = 5 * 60 * 1000


class Clock(Protocol):
    def now_epoch(self) -> int: ...

    def now_ms(self) -> int: ...


class _SystemClock:
    def now_epoch(self) -> int:
        return int(time.time())

    def now_ms(self) -> int:
        return int(time.time() * 1000)


@dataclass(frozen=True)
class TaskTurnObservation:
    turn_id: str
    role: str
    status: str
    text: str
    created_at_ms: int
    error_class: str | None = None
    error_message: str = ""


@dataclass(frozen=True)
class TaskObservation:
    thread_id: str
    host_id: str
    status: str
    turns: tuple[TaskTurnObservation, ...]
    waiting_for_approval: bool = False
    waiting_for_user_input: bool = False

    @property
    def latest_failed_limit_turn_id(self) -> str | None:
        for turn in reversed(self.turns):
            if classify_failed_turn(
                TaskTurn(
                    turn_id=turn.turn_id,
                    status=turn.status,
                    created_at_ms=turn.created_at_ms,
                    error_class=turn.error_class,
                    error_message=turn.error_message,
                )
            ):
                return turn.turn_id
        return None

    @property
    def is_active(self) -> bool:
        active_statuses = {"active", "running", "in_progress", "working", "processing", "started"}
        return self.status in active_statuses or any(
            turn.role in {"assistant", "turn"} and turn.status in active_statuses for turn in self.turns
        )


class SupervisorContractError(RuntimeError):
    pass


class AutoResumeSupervisor:
    """Coordinates read-only discovery and exact-task dispatch one short tick at a time."""

    MAX_DISPATCH_ATTEMPTS = 3

    def __init__(
        self,
        app_server: Any,
        bridge: Any,
        store: RetryStateStore,
        *,
        clock: Clock | None = None,
        prompt: str = "keep going",
        margin_seconds: int = 60,
        reconnect_factory: Callable[[], tuple[Any, Any]] | None = None,
        audit_sink: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> None:
        if not prompt:
            raise ValueError("prompt must not be empty")
        if margin_seconds < 0:
            raise ValueError("margin_seconds must not be negative")
        self.app_server = app_server
        self.bridge = bridge
        self.store = store
        self.clock = clock or _SystemClock()
        self.prompt = prompt
        self.margin_seconds = margin_seconds
        self.reconnect_factory = reconnect_factory
        self.audit_sink = audit_sink
        self.state: RetryStoreState | None = None
        self.audit_events: list[dict[str, Any]] = []
        self._confirmed_dispatch_keys: set[str] = set()
        self.started = False

    @property
    def connected(self) -> bool:
        return self.app_server is not None and self.bridge is not None

    def start(self) -> RetryStoreState:
        if self.started and self.state is not None:
            return self.state
        self.state = self.store.load()
        recovered = []
        now_ms = self.clock.now_ms()
        for item in self.state.queue:
            if item.state == RetryState.DISPATCHING:
                recovered.append(item.evolve(state=RetryState.VERIFYING, updated_at_ms=now_ms, last_error="recovered dispatch"))
            else:
                recovered.append(item)
        recovered_state = replace(self.state, queue=tuple(recovered))
        if recovered_state != self.state:
            self.state = recovered_state
            self.store.save(self.state)
            self._audit("recovered", count=sum(item.state == RetryState.VERIFYING for item in recovered))
        self.started = True
        self._audit("started", connected=self.connected)
        return self.state

    def stop(self) -> None:
        self.started = False
        close = getattr(self.bridge, "close", None)
        if callable(close):
            close()
        close = getattr(self.app_server, "close", None)
        if callable(close):
            close()
        self._audit("stopped")

    def tick(self) -> RetryStoreState:
        if not self.started or self.state is None:
            self.start()
        assert self.state is not None
        if not self._ensure_connected():
            return self.state
        now_ms = self.clock.now_ms()
        now_epoch = self.clock.now_epoch()
        settled_watermark_ms = max(0, now_ms - OBSERVATION_SETTLE_LAG_MS)
        effective_watermark_ms = min(self.state.observation_watermark_ms, settled_watermark_ms)
        try:
            failures = tuple(
                self.app_server.find_five_hour_failures(
                    observation_watermark_ms=effective_watermark_ms
                )
            )
            bucket = self.app_server.read_five_hour_bucket() if failures else None
            self._record_failures(failures, bucket, now_ms)
            primary_bucket_available = bucket is not None and bucket.is_five_hour_primary
            if (
                (not failures or primary_bucket_available)
                and self.state.observation_watermark_ms < settled_watermark_ms
            ):
                self.state = replace(self.state, observation_watermark_ms=settled_watermark_ms)
                self.store.save(self.state)
        except Exception as exc:
            self._disconnect("scan", exc)
            return self.state

        try:
            self._process_fifo(now_epoch, now_ms)
        except (DesktopBridgeTransportUncertain, DesktopBridgeError, ConnectionError, OSError) as exc:
            self._disconnect("bridge", exc)
        except Exception as exc:
            self._disconnect("supervisor", exc)
        return self.state

    def _ensure_connected(self) -> bool:
        if self.connected:
            return True
        if self.reconnect_factory is None:
            self._audit("disconnected", error_class="no_reconnect_factory")
            return False
        try:
            app_server, bridge = self.reconnect_factory()
            if app_server is None or bridge is None:
                raise ConnectionError("reconnect returned incomplete adapters")
            self.app_server = app_server
            self.bridge = bridge
            self._audit("reconnected")
            return True
        except Exception as exc:
            self._audit("reconnect_failed", error_class=type(exc).__name__)
            return False

    def _record_failures(
        self,
        failures: Iterable[FailedTask],
        bucket: RateLimitBucket | None,
        now_ms: int,
    ) -> None:
        if self.state is None:
            return
        if bucket is not None and not bucket.is_five_hour_primary:
            bucket = None
        known = {item.key for item in self.state.queue}
        known.update(
            str(entry.get("key")) for entry in self.state.dedupe_ledger if isinstance(entry, Mapping) and entry.get("key")
        )
        queue = list(self.state.queue)
        for failure in failures:
            if failure.queue_key in known:
                continue
            self._audit("detected", thread_id=failure.thread_id, failed_turn_id=failure.failed_turn_id)
            if bucket is None:
                continue
            item = RetryItem.from_failure(failure, bucket, created_at_ms=now_ms, prompt=self.prompt)
            queue.append(item)
            known.add(item.key)
            self._audit(
                "queued",
                key=item.key,
                thread_id=item.thread_id,
                failed_turn_id=item.failed_turn_id,
                reset_at_epoch=item.reset_at_epoch,
            )
        if tuple(queue) != self.state.queue:
            self.state = replace(self.state, queue=tuple(queue))
            self.store.save(self.state)

    def _process_fifo(self, now_epoch: int, now_ms: int) -> None:
        assert self.state is not None
        for item in self.state.queue:
            if item.state in {RetryState.COMPLETED, RetryState.CANCELLED, RetryState.NEEDS_ATTENTION}:
                continue
            self._process_item(item, now_epoch, now_ms)
            return

    def _process_item(self, item: RetryItem, now_epoch: int, now_ms: int) -> None:
        assert self.state is not None
        if item.state == RetryState.DISPATCHING:
            item = item.evolve(state=RetryState.VERIFYING, updated_at_ms=now_ms, last_error="recovered dispatch")
            self._replace_item(item)
            self._audit("recovered", key=item.key)
            return

        if item.state == RetryState.WAITING_FOR_RESET:
            if now_epoch < item.reset_at_epoch + self.margin_seconds:
                return
            bucket = self.app_server.read_five_hour_bucket()
            if bucket is None:
                return
            if bucket.reached:
                if bucket.resets_at_epoch > item.reset_at_epoch:
                    updated = item.evolve(reset_at_epoch=bucket.resets_at_epoch, updated_at_ms=now_ms)
                    self._replace_item(updated)
                return
            ready = item.evolve(state=RetryState.READY, updated_at_ms=now_ms, last_error=None)
            self._replace_item(ready)
            self._audit("reset_confirmed", key=item.key, reset_at_epoch=item.reset_at_epoch)
            return

        if item.state == RetryState.READY:
            observation = self._read_observation(item)
            decision = self._preflight_decision(item, observation)
            if decision is not None:
                state = RetryState.CANCELLED if decision != "needs_attention" else RetryState.NEEDS_ATTENTION
                cancelled = item.evolve(state=state, updated_at_ms=now_ms, last_error=decision)
                self._replace_item(cancelled)
                self._audit("preflight", key=item.key, result=decision)
                return
            dispatching = item.evolve(
                state=RetryState.DISPATCHING,
                dispatch_attempts=item.dispatch_attempts + 1,
                updated_at_ms=now_ms,
                last_error=None,
            )
            self._replace_item(dispatching)
            self._audit("dispatching", key=item.key, attempt=dispatching.dispatch_attempts)
            try:
                self.bridge.send_message(item.thread_id, item.prompt, host_id=item.host_id)
            except DesktopBridgeTransportUncertain as exc:
                verifying = dispatching.evolve(state=RetryState.VERIFYING, updated_at_ms=now_ms, last_error="transport_uncertain")
                self._replace_item(verifying)
                self._audit("dispatch_uncertain", key=item.key, error_class=type(exc).__name__)
                raise
            except DesktopBridgeToolError as exc:
                attention = dispatching.evolve(state=RetryState.NEEDS_ATTENTION, updated_at_ms=now_ms, last_error="confirmed_rejection")
                self._replace_item(attention)
                self._audit("dispatch_rejected", key=item.key, error_class=type(exc).__name__)
                return
            self._confirmed_dispatch_keys.add(dispatching.key)
            verifying = dispatching.evolve(
                state=RetryState.VERIFYING,
                dispatch_confirmed=True,
                updated_at_ms=now_ms,
            )
            self._replace_item(verifying)
            self._audit("dispatched", key=item.key, attempt=verifying.dispatch_attempts)
            return

        if item.state == RetryState.VERIFYING:
            observation = self._read_observation(item)
            if _verification_succeeds(
                item,
                observation,
                allow_unlabeled_progress=item.dispatch_confirmed or item.key in self._confirmed_dispatch_keys,
            ):
                completed = item.evolve(state=RetryState.COMPLETED, updated_at_ms=now_ms, last_error=None)
                self._replace_item(completed)
                self._audit("verified", key=item.key)
                return
            if item.dispatch_attempts >= self.MAX_DISPATCH_ATTEMPTS:
                attention = item.evolve(state=RetryState.NEEDS_ATTENTION, updated_at_ms=now_ms, last_error="verification_budget_exhausted")
                self._replace_item(attention)
                self._audit("verification_failed", key=item.key, result="needs_attention")
                return
            backoff = min(60, 2 ** max(1, item.dispatch_attempts))
            if now_epoch < (item.updated_at_ms // 1000) + backoff:
                return
            ready = item.evolve(state=RetryState.READY, updated_at_ms=now_ms, last_error="verification_pending")
            self._replace_item(ready)
            self._audit("verification_failed", key=item.key, result="retry_ready")

    def _read_observation(self, item: RetryItem) -> TaskObservation:
        raw = self.bridge.read_thread(item.thread_id, host_id=item.host_id)
        return normalize_task_observation(raw, expected_thread_id=item.thread_id, expected_host_id=item.host_id)

    def _preflight_decision(self, item: RetryItem, observation: TaskObservation) -> str | None:
        if observation.thread_id != item.thread_id or observation.host_id != item.host_id:
            return "needs_attention"
        if observation.latest_failed_limit_turn_id and observation.latest_failed_limit_turn_id != item.failed_turn_id:
            return "newer_limit_failure"
        if observation.is_active:
            return "active_task"
        if observation.waiting_for_approval:
            return "waiting_for_approval"
        if observation.waiting_for_user_input:
            return "waiting_for_user_input"
        for turn in observation.turns:
            if turn.role in {"user", "turn"} and turn.created_at_ms > item.failed_at_ms:
                return "manual_continuation"
        return None

    def _replace_item(self, item: RetryItem) -> None:
        assert self.state is not None
        queue = tuple(item if current.key == item.key else current for current in self.state.queue)
        if queue == self.state.queue:
            raise SupervisorContractError(f"queue item disappeared: {item.key}")
        self.state = replace(self.state, queue=queue)
        self.store.save(self.state)

    def _disconnect(self, phase: str, exc: BaseException) -> None:
        self._audit("disconnected", phase=phase, error_class=type(exc).__name__)
        for adapter in (self.bridge, self.app_server):
            close = getattr(adapter, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        self.bridge = None
        self.app_server = None

    def _audit(self, event: str, **fields: Any) -> None:
        record = {"event": event, "at_epoch": self.clock.now_epoch()}
        for key, value in fields.items():
            if key in {"message", "text", "transcript", "prompt"}:
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                record[key] = value
        self.audit_events.append(record)
        if self.audit_sink is not None:
            self.audit_sink(record)


def normalize_task_observation(
    raw: Any,
    *,
    expected_thread_id: str,
    expected_host_id: str,
) -> TaskObservation:
    if not isinstance(raw, Mapping):
        raise SupervisorContractError("task inspection returned a non-object")
    if isinstance(raw.get("structuredContent"), Mapping):
        raw = raw["structuredContent"]
    thread = raw.get("thread") if isinstance(raw.get("thread"), Mapping) else raw
    thread_id = _string(thread, "threadId", "thread_id", "id")
    host_id = _string(thread, "hostId", "host_id", "host") or expected_host_id
    if thread_id != expected_thread_id or host_id != expected_host_id:
        raise SupervisorContractError("task inspection identity did not match the queued task")
    status = _status(thread, "status", "state")
    turns_raw = thread.get("turns", raw.get("turns", []))
    if not isinstance(turns_raw, list):
        raise SupervisorContractError("task inspection turns are not a list")
    turns = tuple(
        sorted(
            (_normalize_turn(turn) for turn in turns_raw if isinstance(turn, Mapping)),
            key=lambda turn: (turn.created_at_ms, turn.turn_id),
        )
    )
    waiting_approval = _truthy(thread, "waitingForApproval", "waiting_for_approval", "awaitingApproval", "needsApproval")
    waiting_input = _truthy(thread, "waitingForUserInput", "waiting_for_user_input", "awaitingUserInput", "needsUserInput")
    waiting_approval = waiting_approval or status in {"waiting_for_approval", "awaiting_approval", "approval_required"}
    waiting_input = waiting_input or status in {"waiting_for_user_input", "awaiting_user_input", "input_required"}
    return TaskObservation(thread_id, host_id, status, turns, waiting_approval, waiting_input)


def _verification_succeeds(
    item: RetryItem,
    observation: TaskObservation,
    *,
    allow_unlabeled_progress: bool = False,
) -> bool:
    users = [
        turn
        for turn in observation.turns
        if turn.role == "user" and turn.created_at_ms > item.failed_at_ms and item.prompt in turn.text
    ]
    if not users and allow_unlabeled_progress:
        return any(
            turn.role == "turn"
            and turn.created_at_ms > item.failed_at_ms
            and (
                turn.status in {"active", "running", "in_progress", "working", "processing", "started"}
                or (turn.status in {"completed", "succeeded", "done"} and bool(turn.text))
            )
            for turn in observation.turns
        )
    if not users:
        return False
    user = users[-1]
    active_statuses = {"active", "running", "in_progress", "working", "processing", "started"}
    if observation.status in active_statuses:
        return True
    if any(
        turn.role in {"assistant", "turn"}
        and turn.created_at_ms > user.created_at_ms
        and turn.status in active_statuses
        for turn in observation.turns
    ):
        return True
    return any(
        turn.role == "turn"
        and turn.created_at_ms > item.failed_at_ms
        and turn.status in {"completed", "succeeded", "done"}
        and bool(turn.text)
        for turn in observation.turns
    )


def _normalize_turn(raw: Mapping[str, Any]) -> TaskTurnObservation:
    turn_id = _string(raw, "turnId", "turn_id", "id")
    if not turn_id:
        raise SupervisorContractError("task inspection turn has no ID")
    role = (_string(raw, "role", "authorRole", "author_role", "type") or "unknown").lower()
    if role in {"user_message", "human"}:
        role = "user"
    elif role in {"assistant_message", "agent"}:
        role = "assistant"
    elif role == "unknown" and isinstance(raw.get("items"), list):
        role = "turn"
    status = _status(raw, "status", "state")
    created = _number(raw, "createdAt", "created_at_ms", "createdAtMs", "startedAt")
    created_ms = int(created if created >= 10**12 else created * 1000)
    error_class = None
    error_message = _string(raw, "errorMessage", "error_message") or ""
    error = raw.get("error")
    if isinstance(error, Mapping):
        error_class = _string(error, "type", "errorType", "code", "class")
        error_message = _string(error, "message", "detail") or error_message
        error_info = error.get("codexErrorInfo")
        if isinstance(error_info, str) and error_info:
            error_class = error_class or error_info
        elif isinstance(error_info, Mapping):
            error_class = error_class or _string(error_info, "type", "errorType", "code", "class")
    elif isinstance(error, str):
        error_message = error
    return TaskTurnObservation(
        turn_id=turn_id,
        role=role,
        status=status,
        text=_text(raw),
        created_at_ms=created_ms,
        error_class=error_class,
        error_message=error_message,
    )


def _text(raw: Mapping[str, Any]) -> str:
    for key in ("text", "message", "content"):
        value = raw.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts = [item.get("text") for item in value if isinstance(item, Mapping) and isinstance(item.get("text"), str)]
            if parts:
                return "".join(parts)
        if isinstance(value, Mapping) and isinstance(value.get("text"), str):
            return value["text"]
    items = raw.get("items")
    if isinstance(items, list):
        parts: list[str] = []
        for item in items:
            if not isinstance(item, Mapping) or item.get("type") not in {"agentMessage", "assistantMessage"}:
                continue
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
        if parts:
            return "".join(parts)
    return ""


def _string(raw: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _number(raw: Mapping[str, Any], *keys: str) -> float:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                continue
    return 0.0


def _truthy(raw: Mapping[str, Any], *keys: str) -> bool:
    return any(bool(raw.get(key)) for key in keys)


def _status(raw: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, Mapping):
            value = value.get("type", value.get("status", value.get("state")))
        if isinstance(value, str) and value:
            return _canonical_status(value)
    return "unknown"


def _canonical_status(value: str) -> str:
    normalized = value.strip().replace("-", "_").replace(" ", "_")
    result: list[str] = []
    for char in normalized:
        if char.isupper() and result and result[-1] != "_":
            result.append("_")
        result.append(char.lower())
    return "".join(result)


__all__ = [
    "AutoResumeSupervisor",
    "SupervisorContractError",
    "TaskObservation",
    "TaskTurnObservation",
    "normalize_task_observation",
]
