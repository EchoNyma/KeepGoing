"""Correlated JSON-RPC-over-JSONL transport for private subprocesses."""

from __future__ import annotations

from collections import deque
import json
import os
import queue
import re
import subprocess
import threading
from typing import Any, Callable, Deque, Mapping, Sequence


class JsonRpcError(RuntimeError):
    """Base class for local and remote JSON-RPC failures."""


class JsonRpcRemoteError(JsonRpcError):
    def __init__(self, code: Any, message: str, data: Any = None) -> None:
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"remote JSON-RPC error {code}: {message}")


class JsonRpcProtocolError(JsonRpcError):
    pass


class JsonRpcTimeoutError(JsonRpcError):
    pass


class JsonRpcProcessExitedError(JsonRpcError):
    def __init__(self, returncode: int | None, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = sanitize_error_text(stderr)
        detail = f"protocol child exited with code {returncode}"
        if self.stderr:
            detail += f": {self.stderr}"
        super().__init__(detail)


_BEARER_RE = re.compile(r"(?i)(\bbearer\s+)[^\s,;]+")
_TOKEN_FIELD_RE = re.compile(
    r"(?i)(\b(?:access[_-]?token|refresh[_-]?token|api[_-]?key)\b\s*[:=]\s*[\"']?)[^\"'\s,}]+"
)


def sanitize_error_text(text: str) -> str:
    """Remove common authentication material from human-readable diagnostics."""

    sanitized = _BEARER_RE.sub(r"\1[REDACTED]", text)
    return _TOKEN_FIELD_RE.sub(r"\1[REDACTED]", sanitized)


_Pending = queue.Queue[tuple[bool, Any]]


class JsonRpcClient:
    """A thread-safe JSONL subprocess client with request correlation."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: str | os.PathLike[str] | None = None,
        request_timeout: float = 10.0,
        notification_callback: Callable[[str, Any], None] | None = None,
        popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    ) -> None:
        if not command:
            raise ValueError("command must not be empty")
        if request_timeout <= 0:
            raise ValueError("request_timeout must be positive")
        self.command = tuple(str(part) for part in command)
        self.env = dict(env) if env is not None else None
        self.cwd = cwd
        self.request_timeout = request_timeout
        self.notification_callback = notification_callback
        self._popen_factory = popen_factory

        self.process: subprocess.Popen[str] | None = None
        self.reader_thread: threading.Thread | None = None
        self.stderr_thread: threading.Thread | None = None
        self._next_id = 1
        self._pending: dict[int, _Pending] = {}
        self._notifications: Deque[tuple[str, Any]] = deque()
        self._state_lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._stderr_lock = threading.Lock()
        self._stderr_parts: Deque[str] = deque(maxlen=64)
        self._fatal_error: JsonRpcError | None = None
        self._closed = False

    @property
    def stderr_text(self) -> str:
        with self._stderr_lock:
            return "".join(self._stderr_parts)

    def start(self) -> "JsonRpcClient":
        with self._state_lock:
            if self.process is not None:
                return self
            if self._closed:
                raise JsonRpcError("JSON-RPC client is closed")
            try:
                self.process = self._popen_factory(
                    list(self.command),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=self.env,
                    cwd=self.cwd,
                    shell=False,
                )
            except OSError as exc:
                raise JsonRpcProcessExitedError(None, sanitize_error_text(str(exc))) from exc

            self.reader_thread = threading.Thread(
                target=self._read_stdout,
                name="keepgoing-jsonrpc-stdout",
                daemon=True,
            )
            self.stderr_thread = threading.Thread(
                target=self._read_stderr,
                name="keepgoing-jsonrpc-stderr",
                daemon=True,
            )
            self.reader_thread.start()
            self.stderr_thread.start()
        return self

    def request(self, method: str, params: Any = None, timeout: float | None = None) -> Any:
        if not method:
            raise ValueError("method must not be empty")
        waiter: _Pending = queue.Queue(maxsize=1)
        with self._state_lock:
            self._ensure_running()
            request_id = self._next_id
            self._next_id += 1
            self._pending[request_id] = waiter
            request = {"jsonrpc": "2.0", "id": request_id, "method": method}
            if params is not None:
                request["params"] = params
            try:
                self._write_locked(request)
            except Exception:
                self._pending.pop(request_id, None)
                raise

        wait_seconds = self.request_timeout if timeout is None else timeout
        if wait_seconds <= 0:
            with self._state_lock:
                self._pending.pop(request_id, None)
            raise JsonRpcTimeoutError(f"request {request_id} timed out")
        try:
            is_error, payload = waiter.get(timeout=wait_seconds)
        except queue.Empty as exc:
            with self._state_lock:
                self._pending.pop(request_id, None)
            raise JsonRpcTimeoutError(f"request {request_id} timed out after {wait_seconds:.3f}s") from exc
        if is_error:
            raise payload
        return payload

    def send_notification(self, method: str, params: Any = None) -> None:
        if not method:
            raise ValueError("method must not be empty")
        with self._state_lock:
            self._ensure_running()
            request = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                request["params"] = params
            self._write_locked(request)

    def drain_notifications(self) -> list[tuple[str, Any]]:
        with self._state_lock:
            result = list(self._notifications)
            self._notifications.clear()
            return result

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            self._fail_pending_locked(JsonRpcError("JSON-RPC client closed"))
            process = self.process
            if process is not None and process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass

        if process is not None:
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                try:
                    process.terminate()
                except OSError:
                    pass
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except OSError:
                        pass
                    process.wait(timeout=1.0)

        current = threading.current_thread()
        for thread in (self.reader_thread, self.stderr_thread):
            if thread is not None and thread is not current:
                thread.join(timeout=1.5)
        if process is not None:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass

    def __enter__(self) -> "JsonRpcClient":
        return self.start()

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()

    def _ensure_running(self) -> None:
        if self._closed:
            raise JsonRpcError("JSON-RPC client is closed")
        if self.process is None:
            raise JsonRpcError("JSON-RPC client has not been started")
        if self._fatal_error is not None:
            raise self._fatal_error
        if self.process.poll() is not None:
            error = JsonRpcProcessExitedError(self.process.returncode, self.stderr_text)
            self._fatal_error = error
            raise error
        if self.process.stdin is None:
            raise JsonRpcError("JSON-RPC child has no stdin")

    def _write_locked(self, message: dict[str, Any]) -> None:
        assert self.process is not None
        assert self.process.stdin is not None
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._write_lock:
            try:
                self.process.stdin.write(encoded)
                self.process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                error = JsonRpcProcessExitedError(self.process.poll(), self.stderr_text)
                self._fatal_error = error
                raise error from exc

    def _read_stdout(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    self._set_fatal(JsonRpcProtocolError("malformed JSONL response"))
                    return
                if not isinstance(message, dict):
                    self._set_fatal(JsonRpcProtocolError("JSONL response must be an object"))
                    return
                self._handle_message(message)
        finally:
            if not self._closed and self._fatal_error is None:
                try:
                    returncode = process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    returncode = process.poll()
                self._set_fatal(JsonRpcProcessExitedError(returncode, self.stderr_text))

    def _read_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            with self._stderr_lock:
                self._stderr_parts.append(sanitize_error_text(line))

    def _handle_message(self, message: dict[str, Any]) -> None:
        if "id" not in message or message.get("id") is None:
            method = message.get("method")
            if isinstance(method, str):
                params = message.get("params")
                with self._state_lock:
                    self._notifications.append((method, params))
                if self.notification_callback is not None:
                    try:
                        self.notification_callback(method, params)
                    except Exception:
                        pass
            return

        request_id = message.get("id")
        if not isinstance(request_id, int):
            return
        with self._state_lock:
            waiter = self._pending.pop(request_id, None)
        if waiter is None:
            return
        if "error" in message:
            error = message["error"]
            if isinstance(error, dict):
                payload = JsonRpcRemoteError(error.get("code"), str(error.get("message", "remote error")), error.get("data"))
            else:
                payload = JsonRpcRemoteError(None, str(error))
            waiter.put((True, payload))
            return
        waiter.put((False, message.get("result")))

    def _set_fatal(self, error: JsonRpcError) -> None:
        with self._state_lock:
            if self._fatal_error is None:
                self._fatal_error = error
            self._fail_pending_locked(self._fatal_error)

    def _fail_pending_locked(self, error: JsonRpcError) -> None:
        pending = list(self._pending.values())
        self._pending.clear()
        for waiter in pending:
            try:
                waiter.put_nowait((True, error))
            except queue.Full:
                pass


__all__ = [
    "JsonRpcClient",
    "JsonRpcError",
    "JsonRpcProcessExitedError",
    "JsonRpcProtocolError",
    "JsonRpcRemoteError",
    "JsonRpcTimeoutError",
    "sanitize_error_text",
]
