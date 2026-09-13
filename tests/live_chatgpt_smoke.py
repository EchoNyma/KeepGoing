"""Explicit live checks for the bundled ChatGPT desktop protocols.

The default mode is read-only. Dispatch is deliberately gated behind both
``--allow-dispatch`` and a clearly marked disposable task title.
"""

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
from typing import Any, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from keepgoing_chatgpt.app_server import AppServerClient, classify_failed_turn
from keepgoing_chatgpt.desktop_bridge import DesktopTaskBridge
from keepgoing_chatgpt.discovery import diagnostic_payload, discover_runtime
from keepgoing_chatgpt.jsonrpc import JsonRpcClient, sanitize_error_text
from keepgoing_chatgpt.models import RetryItem, RetryState
from keepgoing_chatgpt.state import RetryStateStore, RetryStoreState
from keepgoing_chatgpt.supervisor import AutoResumeSupervisor, normalize_task_observation


FORBIDDEN_TASK_ID = "01a03adb-25a3-75a1-b6de-0f54a18dfaa3"
TITLE_MARKERS = ("keepgoing", "smoke", "disposable")
VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:[-+][\w.]+)?")


@dataclass
class LiveRuntime:
    app_rpc: JsonRpcClient
    app: AppServerClient
    bridge: DesktopTaskBridge

    def close(self) -> None:
        self.bridge.close()
        self.app_rpc.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only/live smoke checks for KeepGoing ChatGPT adapters.")
    parser.add_argument(
        "--executor-thread-id",
        default=os.environ.get("KEEPGOING_EXECUTOR_THREAD_ID"),
        help="Codex task ID used as the desktop bridge executor context.",
    )
    parser.add_argument("--read-task-id", help="Existing task ID to inspect read-only.")
    parser.add_argument("--task-id", help="Clearly marked disposable task ID for the gated dispatch test.")
    parser.add_argument("--allow-dispatch", action="store_true", help="Enable the one-task controlled dispatch test.")
    parser.add_argument("--state-dir", help="Isolated temporary state directory for --allow-dispatch.")
    parser.add_argument("--timeout-seconds", type=float, default=45.0, help="Bounded dispatch verification timeout.")
    return parser


def validate_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if not args.executor_thread_id:
        parser.error("--executor-thread-id or KEEPGOING_EXECUTOR_THREAD_ID is required")
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    if args.allow_dispatch:
        if not args.task_id:
            parser.error("--allow-dispatch requires --task-id")
        if not args.state_dir:
            parser.error("--allow-dispatch requires --state-dir")
        if args.read_task_id:
            parser.error("--read-task-id cannot be combined with --allow-dispatch")
    elif args.task_id:
        parser.error("--task-id requires --allow-dispatch")


def validate_dispatch_target(task_id: str, listing: Any) -> Mapping[str, Any]:
    """Return only a marked local Codex task; reject every other target."""

    if task_id == FORBIDDEN_TASK_ID:
        raise ValueError("the existing Personal OS task is forbidden for dispatch")
    if not isinstance(listing, Mapping):
        raise ValueError("desktop task listing is not an object")
    entries: list[Mapping[str, Any]] = []
    for key in ("threads", "pinnedThreads"):
        raw_entries = listing.get(key)
        if isinstance(raw_entries, list):
            entries.extend(item for item in raw_entries if isinstance(item, Mapping))
    raw_thread = listing.get("thread")
    if isinstance(raw_thread, Mapping):
        entries.append(raw_thread)
    target = next((item for item in entries if item.get("id") == task_id), None)
    if target is None:
        raise ValueError("dispatch target was not found in the desktop task listing")
    if str(target.get("hostId", target.get("host_id", ""))).lower() != "local":
        raise ValueError("dispatch target is not local")
    if str(target.get("kind", target.get("type", ""))).lower() != "codex":
        raise ValueError("dispatch target is not a Codex task")
    title = str(target.get("title", target.get("name", "")))
    lowered = title.casefold()
    if not all(marker in lowered for marker in TITLE_MARKERS):
        raise ValueError("dispatch target title is not clearly marked as a disposable KeepGoing smoke test")
    return target


def connect_runtime(executor_thread_id: str, timeout_seconds: float) -> LiveRuntime:
    snapshot = discover_runtime()
    bundled_version = _bundled_version(snapshot.codex_executable)
    app_rpc = JsonRpcClient(
        [snapshot.codex_executable, "app-server", "--listen", "stdio://"],
        request_timeout=timeout_seconds,
    )
    app_rpc.start()
    app = AppServerClient(
        app_rpc,
        bundled_version=bundled_version,
        expected_codex_home=os.environ.get("CODEX_HOME") or None,
        request_timeout=timeout_seconds,
    )
    try:
        app.initialize()
        bridge = DesktopTaskBridge.launch(
            snapshot,
            request_timeout=timeout_seconds,
            executor_thread_id=executor_thread_id,
        )
    except Exception:
        app_rpc.close()
        raise
    return LiveRuntime(app_rpc, app, bridge)


def read_only_probe(runtime: LiveRuntime, snapshot_version: str | None, read_task_id: str | None) -> dict[str, Any]:
    snapshot = discover_runtime()
    bucket = runtime.app.read_five_hour_bucket()
    tasks = runtime.app.list_local_tasks(limit=100)
    result: dict[str, Any] = {
        "mode": "read_only",
        "ok": True,
        "discovery": diagnostic_payload(snapshot, version=snapshot_version),
        "app_server": {
            "version": runtime.app.info.version if runtime.app.info else None,
            "codex_home": runtime.app.info.codex_home if runtime.app.info else None,
        },
        "rate_limit": (
            {
                "name": bucket.name,
                "window_duration_minutes": bucket.window_duration_minutes,
                "resets_at_epoch": bucket.resets_at_epoch,
                "reached": bucket.reached,
                "used_percent": bucket.used_percent,
            }
            if bucket
            else None
        ),
        "local_codex_task_count": len(tasks),
        "bridge": {
            "server_name": runtime.bridge.info.server_name if runtime.bridge.info else None,
            "server_version": runtime.bridge.info.server_version if runtime.bridge.info else None,
            "read_tool": runtime.bridge.read_tool_name,
            "required_tools_present": all(name in runtime.bridge.tools for name in ("list_threads", "wait_threads", "send_message_to_thread")),
        },
    }
    if read_task_id:
        listed_task = next((task for task in tasks if task.thread_id == read_task_id), None)
        if listed_task is None:
            raise ValueError("read-only task was not present in the validated local task list")
        history = runtime.app.read_task_history(
            read_task_id,
            expected_host_id=listed_task.host_id,
            expected_kind=listed_task.kind,
        )
        bridge_raw = runtime.bridge.read_thread(read_task_id, host_id="local", turn_limit=10)
        observation = normalize_task_observation(
            bridge_raw,
            expected_thread_id=read_task_id,
            expected_host_id="local",
        )
        result["task"] = {
            "thread_id": history.thread_id,
            "host_id": history.host_id,
            "history_turn_count": len(history.turns),
            "five_hour_failure_count": sum(1 for turn in history.turns if classify_failed_turn(turn)),
            "bridge_turn_count": len(observation.turns),
            "bridge_status": observation.status,
            "bridge_active": observation.is_active,
        }
    return result


def controlled_dispatch(runtime: LiveRuntime, task_id: str, state_dir: str, timeout_seconds: float) -> dict[str, Any]:
    target_raw = runtime.bridge.read_thread(task_id, host_id="local", turn_limit=10)
    target = validate_dispatch_target(task_id, target_raw)
    baseline_raw = target_raw
    baseline = normalize_task_observation(
        baseline_raw,
        expected_thread_id=task_id,
        expected_host_id="local",
    )
    if baseline.is_active or baseline.waiting_for_approval or baseline.waiting_for_user_input:
        raise ValueError("disposable target is not idle before the controlled dispatch")

    state_path = Path(state_dir) / "chatgpt-auto-resume-state.json"
    if state_path.exists():
        raise ValueError("controlled dispatch state path already exists; use a fresh isolated directory")
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    now_ms = int(time.time() * 1000)
    baseline_latest_ms = max((turn.created_at_ms for turn in baseline.turns), default=0)
    failed_at_ms = max(now_ms, baseline_latest_ms)
    item = RetryItem(
        thread_id=task_id,
        host_id="local",
        failed_turn_id="keepgoing-live-smoke-seed",
        failed_at_ms=failed_at_ms,
        reset_at_epoch=int(time.time()) - 1,
        prompt="keep going",
        state=RetryState.READY,
        created_at_ms=now_ms,
        updated_at_ms=now_ms,
    )
    store = RetryStateStore(
        state_path,
        mutex_name=f"KeepGoing-LiveSmoke-{os.getpid()}",
    )
    store.save(RetryStoreState(observation_watermark_ms=now_ms, queue=(item,)))
    audit: list[Mapping[str, Any]] = []
    supervisor = AutoResumeSupervisor(
        runtime.app,
        runtime.bridge,
        store,
        prompt="keep going",
        margin_seconds=0,
        audit_sink=audit.append,
    )
    supervisor.start()
    deadline = time.monotonic() + timeout_seconds
    try:
        while time.monotonic() < deadline:
            supervisor.tick()
            state = store.load()
            current = next(entry for entry in state.queue if entry.key == item.key)
            if current.state == RetryState.COMPLETED:
                break
            if current.state in {RetryState.CANCELLED, RetryState.NEEDS_ATTENTION}:
                raise RuntimeError(f"controlled dispatch ended in {current.state.value}")
            time.sleep(0.5)
        else:
            raise TimeoutError("controlled dispatch verification timed out")

        final_raw = runtime.bridge.read_thread(task_id, host_id="local", turn_limit=10)
        final = normalize_task_observation(
            final_raw,
            expected_thread_id=task_id,
            expected_host_id="local",
        )
        new_turns = [turn for turn in final.turns if turn.created_at_ms > failed_at_ms]
        assistant_progress = any(
            turn.role == "turn"
            and (
                turn.status in {"active", "running", "in_progress", "working", "processing", "started"}
                or (turn.status in {"completed", "succeeded", "done"} and bool(turn.text))
            )
            for turn in new_turns
        )
        completed_item = store.load().queue[0]
        dispatch_events = [event for event in audit if event.get("event") == "dispatched"]
        if len(new_turns) != 1 or not assistant_progress or len(dispatch_events) != 1 or completed_item.dispatch_attempts != 1:
            raise RuntimeError("controlled dispatch did not produce exactly one verified new turn")
        return {
            "mode": "controlled_dispatch",
            "ok": True,
            "target_thread_id": task_id,
            "target_title": target.get("title", target.get("name", "")),
            "prompt": "keep going",
            "state": completed_item.state.value,
            "dispatch_attempts": completed_item.dispatch_attempts,
            "injected_turn_count": len(new_turns),
            "assistant_progress": assistant_progress,
            "duplicate_detected": False,
            "audit_events": [str(event.get("event")) for event in audit],
        }
    finally:
        supervisor.stop()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_args(args, parser)
    runtime: LiveRuntime | None = None
    try:
        runtime = connect_runtime(args.executor_thread_id, args.timeout_seconds)
        version = runtime.app.info.version if runtime.app.info else None
        if args.allow_dispatch:
            result = controlled_dispatch(runtime, args.task_id, args.state_dir, args.timeout_seconds)
        else:
            result = read_only_probe(runtime, version, args.read_task_id)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {"mode": "controlled_dispatch" if args.allow_dispatch else "read_only", "ok": False, "error_class": type(exc).__name__, "error": sanitize_error_text(str(exc))},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    finally:
        if runtime is not None:
            runtime.close()


def _bundled_version(executable: str) -> str:
    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            shell=False,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("could not query bundled Codex version") from exc
    match = VERSION_RE.search((result.stdout or "") + "\n" + (result.stderr or ""))
    if result.returncode != 0 or match is None:
        raise RuntimeError("bundled Codex version query failed")
    return match.group(0)


if __name__ == "__main__":
    raise SystemExit(main())
