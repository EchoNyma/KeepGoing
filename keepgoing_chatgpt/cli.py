"""Manual command-line lifecycle for task-aware ChatGPT auto-resume."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Callable, Mapping, Sequence, TextIO

from .app_server import AppServerClient, AppServerInfo
from .desktop_bridge import DesktopTaskBridge
from .discovery import DiscoverySnapshot, discover_runtime
from .jsonrpc import JsonRpcClient, sanitize_error_text
from .state import NamedMutex, RetryStateStore, RetryStoreState
from .supervisor import AutoResumeSupervisor


MONITOR_MUTEX_NAME = "KeepGoing-ChatGPT-AutoResume-Monitor"
DEFAULT_INTERVAL_SECONDS = 15.0


@dataclass(frozen=True)
class RuntimeMetadata:
    snapshot: DiscoverySnapshot
    app_server_info: AppServerInfo


class JsonlAuditLog:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def __call__(self, event: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(dict(event), ensure_ascii=False, sort_keys=True) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="KeepGoing: manually started, task-aware ChatGPT Desktop auto-resume monitor."
    )
    parser.add_argument("pid", nargs="?", type=int, help="Legacy ChatGPT process ID; requires --legacy-uia.")
    parser.add_argument("--text", default="keep going", help="Exact continuation text (default: keep going).")
    parser.add_argument("--margin", type=int, default=60, help="Seconds after the authoritative reset (default: 60).")
    parser.add_argument("--fallback", type=int, default=3600, help="Accepted for legacy compatibility; unused by the primary path.")
    parser.add_argument("--log-path", help="JSONL audit log path.")
    parser.add_argument("--status", action="store_true", help="Report persisted queue state without starting a monitor.")
    parser.add_argument("--legacy-uia", action="store_true", help="Explicitly use the compatibility UI Automation monitor.")
    parser.add_argument(
        "--executor-thread-id",
        default=os.environ.get("KEEPGOING_EXECUTOR_THREAD_ID"),
        help="Codex task ID used as the desktop bridge executor context.",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    output: TextIO | None = None,
    runtime_factory: Callable[[argparse.Namespace, RetryStateStore], Any] | None = None,
    store_factory: Callable[[argparse.Namespace], RetryStateStore] | None = None,
    monitor_lock_factory: Callable[[], Any] | None = None,
    legacy_runner: Callable[[list[str]], int] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    out = output or sys.stdout
    raw_args = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(raw_args)
    if args.margin < 0 or args.fallback < 0:
        parser.error("margin and fallback must not be negative")
    if not args.text:
        parser.error("text must not be empty")

    if args.legacy_uia:
        runner = legacy_runner or _run_legacy
        return int(runner([value for value in raw_args if value != "--legacy-uia"]) or 0)
    if args.pid is not None:
        parser.error("a positional process ID requires --legacy-uia")

    make_store = store_factory or (lambda _args: RetryStateStore())
    store = make_store(args)
    if args.status:
        return _print_status(store, out)

    make_lock = monitor_lock_factory or (lambda: NamedMutex(MONITOR_MUTEX_NAME))
    lock = make_lock()
    if not lock.acquire(timeout_ms=0):
        print("[KeepGoing] Another monitor is already running.", file=out)
        close = getattr(lock, "close", None)
        if callable(close):
            close()
        return 1

    try:
        try:
            make_runtime = runtime_factory or build_runtime
            built = make_runtime(args, store)
            supervisor, metadata = _unpack_runtime(built)
            supervisor.start()
        except Exception as exc:
            print(f"[KeepGoing] Unable to connect: {sanitize_error_text(str(exc))}", file=out)
            return 1

        version = metadata.get("codex_version") if isinstance(metadata, Mapping) else None
        suffix = f" (Codex {version})" if version else ""
        print(f"[KeepGoing] Connected to ChatGPT Desktop{suffix}; monitoring task state.", file=out)
        print("[KeepGoing] Monitor active. Press Ctrl+C to stop.", file=out)
        try:
            while True:
                supervisor.tick()
                sleep(DEFAULT_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            print("[KeepGoing] Monitor stopped.", file=out)
            supervisor.stop()
            return 0
    finally:
        try:
            if not getattr(supervisor, "started", False):
                pass
        except UnboundLocalError:
            pass
        lock.release()
        close = getattr(lock, "close", None)
        if callable(close):
            close()


def build_runtime(args: argparse.Namespace, store: RetryStateStore):
    log_path = args.log_path or _default_log_path()
    audit_log = JsonlAuditLog(log_path)

    app_server, bridge, metadata = _connect_runtime(args.executor_thread_id)

    def reconnect():
        new_app, new_bridge, _ = _connect_runtime(args.executor_thread_id)
        return new_app, new_bridge

    supervisor = AutoResumeSupervisor(
        app_server,
        bridge,
        store,
        prompt=args.text,
        margin_seconds=args.margin,
        reconnect_factory=reconnect,
        audit_sink=audit_log,
    )
    return supervisor, metadata


def _connect_runtime(executor_thread_id: str | None = None) -> tuple[AppServerClient, DesktopTaskBridge, RuntimeMetadata]:
    if not executor_thread_id:
        raise ValueError("--executor-thread-id or KEEPGOING_EXECUTOR_THREAD_ID is required for the primary bridge")
    snapshot = discover_runtime()
    bundled_version = _read_bundled_version(snapshot.codex_executable)
    app_rpc = JsonRpcClient(
        [snapshot.codex_executable, "app-server", "--listen", "stdio://"],
        request_timeout=10.0,
    )
    app_rpc.start()
    app_server = AppServerClient(
        app_rpc,
        bundled_version=bundled_version,
        expected_codex_home=os.environ.get("CODEX_HOME") or None,
        request_timeout=10.0,
    )
    try:
        info = app_server.initialize()
        bridge = DesktopTaskBridge.launch(
            snapshot,
            request_timeout=10.0,
            executor_thread_id=executor_thread_id,
        )
    except Exception:
        app_rpc.close()
        raise
    return app_server, bridge, RuntimeMetadata(snapshot, info)


def _read_bundled_version(codex_executable: str) -> str:
    try:
        result = subprocess.run(
            [codex_executable, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            shell=False,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("unable to read the bundled Codex version") from exc
    if result.returncode != 0:
        raise RuntimeError("bundled Codex version command failed")
    output = "\n".join(value for value in (result.stdout, result.stderr) if value)
    match = re.search(r"(?im)\bcodex-cli\s+([0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?)\b", output)
    if not match:
        raise RuntimeError("bundled Codex version output was not recognized")
    return match.group(1)


def _print_status(store: RetryStateStore, output: TextIO) -> int:
    try:
        state = store.load()
    except Exception as exc:
        print(f"[KeepGoing] Unable to read persisted state: {sanitize_error_text(str(exc))}", file=output)
        return 1
    print(f"[KeepGoing] Queue entries: {len(state.queue)}", file=output)
    print(f"[KeepGoing] Observation watermark: {state.observation_watermark_ms}", file=output)
    for item in state.queue:
        print(
            f"  {item.key} state={item.state.value} reset_at={item.reset_at_epoch} attempts={item.dispatch_attempts}",
            file=output,
        )
    return 0


def _unpack_runtime(value: Any) -> tuple[Any, Mapping[str, Any]]:
    if isinstance(value, tuple) and len(value) == 2:
        supervisor, metadata = value
        if isinstance(metadata, RuntimeMetadata):
            return supervisor, {"codex_version": metadata.app_server_info.version}
        if isinstance(metadata, Mapping):
            return supervisor, metadata
        return supervisor, {}
    return value, {}


def _default_log_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        local_app_data = os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return Path(local_app_data) / "KeepGoing" / "logs" / "chatgpt-auto-resume.jsonl"


def _run_legacy(argv: list[str]) -> int:
    from . import legacy_uia

    original = sys.argv
    try:
        sys.argv = [original[0], *argv]
        result = legacy_uia.main()
        return int(result or 0)
    finally:
        sys.argv = original


__all__ = ["build_parser", "build_runtime", "main"]
