"""Small immutable records shared by the auto-resume adapters."""

from dataclasses import dataclass, replace
from enum import Enum
from typing import Optional


class RetryState(str, Enum):
    WAITING_FOR_RESET = "waiting_for_reset"
    READY = "ready"
    DISPATCHING = "dispatching"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    NEEDS_ATTENTION = "needs_attention"


@dataclass(frozen=True)
class RateLimitBucket:
    """The authoritative account usage bucket used for reset scheduling."""

    window_duration_minutes: int
    resets_at_epoch: int
    reached: bool = True
    name: str = "primary"
    used_percent: Optional[float] = None

    def __post_init__(self) -> None:
        if self.window_duration_minutes <= 0:
            raise ValueError("window_duration_minutes must be positive")
        if self.resets_at_epoch < 0:
            raise ValueError("resets_at_epoch must not be negative")

    @property
    def is_five_hour_primary(self) -> bool:
        return self.name == "primary" and self.window_duration_minutes == 300


@dataclass(frozen=True)
class FailedTask:
    """A single failed turn and the task that owns it."""

    thread_id: str
    host_id: str
    failed_turn_id: str
    failed_at_ms: int
    message: str = ""
    error_class: Optional[str] = None
    task_status: str = "failed"
    latest_turn_id: Optional[str] = None

    def __post_init__(self) -> None:
        for field_name in ("thread_id", "host_id", "failed_turn_id"):
            if not getattr(self, field_name):
                raise ValueError(f"{field_name} must not be empty")
        if self.failed_at_ms < 0:
            raise ValueError("failed_at_ms must not be negative")

    @property
    def queue_key(self) -> str:
        return f"{self.thread_id}:{self.failed_turn_id}"


@dataclass(frozen=True)
class RetryItem:
    """Durable queue entry for one failed task turn."""

    thread_id: str
    host_id: str
    failed_turn_id: str
    failed_at_ms: int
    reset_at_epoch: int
    window_duration_minutes: int = 300
    prompt: str = "keep going"
    state: RetryState = RetryState.WAITING_FOR_RESET
    dispatch_attempts: int = 0
    dispatch_confirmed: bool = False
    last_error: Optional[str] = None
    created_at_ms: int = 0
    updated_at_ms: int = 0

    def __post_init__(self) -> None:
        if not self.thread_id or not self.failed_turn_id or not self.host_id:
            raise ValueError("thread_id, host_id, and failed_turn_id are required")
        if self.failed_at_ms < 0 or self.created_at_ms < 0 or self.updated_at_ms < 0:
            raise ValueError("timestamps must not be negative")
        if self.reset_at_epoch < 0:
            raise ValueError("reset_at_epoch must not be negative")
        if self.window_duration_minutes <= 0:
            raise ValueError("window_duration_minutes must be positive")
        if self.dispatch_attempts < 0:
            raise ValueError("dispatch_attempts must not be negative")
        if not isinstance(self.dispatch_confirmed, bool):
            raise ValueError("dispatch_confirmed must be a boolean")
        if not isinstance(self.state, RetryState):
            raise ValueError("state must be a RetryState")

    @property
    def key(self) -> str:
        return f"{self.thread_id}:{self.failed_turn_id}"

    @classmethod
    def from_failure(
        cls,
        failure: FailedTask,
        bucket: RateLimitBucket,
        created_at_ms: int,
        prompt: str = "keep going",
    ) -> "RetryItem":
        if not bucket.is_five_hour_primary:
            raise ValueError("retry items require the primary five-hour bucket")
        return cls(
            thread_id=failure.thread_id,
            host_id=failure.host_id,
            failed_turn_id=failure.failed_turn_id,
            failed_at_ms=failure.failed_at_ms,
            reset_at_epoch=bucket.resets_at_epoch,
            window_duration_minutes=bucket.window_duration_minutes,
            prompt=prompt,
            created_at_ms=created_at_ms,
            updated_at_ms=created_at_ms,
        )

    def evolve(self, **changes: object) -> "RetryItem":
        """Return a new queue item with the supplied fields changed."""

        return replace(self, **changes)


__all__ = ["FailedTask", "RateLimitBucket", "RetryItem", "RetryState"]
