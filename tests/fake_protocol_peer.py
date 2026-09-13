"""Tiny JSONL peer used by protocol client tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any


_write_lock = threading.Lock()


def _scenario_value(name: str, default: Any = None) -> Any:
    path = os.environ.get("FAKE_KEEPGOING_CONTROL")
    if not path:
        return default
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    return value.get(name, default) if isinstance(value, dict) else default


def _bridge_state() -> dict[str, Any]:
    path = os.environ.get("FAKE_KEEPGOING_BRIDGE_STATE")
    if not path:
        return {"messages": []}
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"messages": []}
    return value if isinstance(value, dict) else {"messages": []}


def _write_bridge_state(state: dict[str, Any]) -> None:
    path = os.environ.get("FAKE_KEEPGOING_BRIDGE_STATE")
    if path:
        Path(path).write_text(json.dumps(state), encoding="utf-8")


def _fake_app_response(method: str, request: dict[str, Any]) -> Any:
    if method == "initialize":
        return {"version": "0.151.0", "codexHome": r"C:\\fake\\codex"}
    if method == "account/rateLimits/read":
        cleared = bool(_scenario_value("bucket_cleared", False))
        return {
            "rateLimits": {
                "primary": {"usedPercent": 0 if cleared else 100, "windowDurationMins": 300, "resetsAt": 1010},
                "weekly": {"usedPercent": 100, "windowDurationMins": 10080, "resetsAt": 9000},
            }
        }
    if method == "thread/list":
        return {
            "data": [
                {"id": "hidden-task", "kind": "codex", "status": "failed", "hostId": "local"},
                {"id": "visible-task", "kind": "codex", "status": "completed", "hostId": "local"},
            ],
            "nextCursor": None,
        }
    if method in {"thread/read", "thread/get"}:
        thread_id = (request.get("params") or {}).get("threadId")
        if thread_id == "visible-task":
            return {
                "thread": {"id": thread_id, "kind": "codex", "hostId": "local", "status": "completed", "turns": []},
                "nextCursor": None,
            }
        return {
            "thread": {
                "id": "hidden-task",
                "kind": "codex",
                "hostId": "local",
                "status": "failed",
                "turns": [
                    {
                        "id": "failed-turn",
                        "status": "failed",
                        "createdAt": 900000,
                        "error": {"type": "usage_limit", "message": "opaque server usage limit"},
                    }
                ],
            },
            "nextCursor": None,
        }
    return {}


def _fake_bridge_response(method: str, request: dict[str, Any]) -> Any:
    if method == "initialize":
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake-codex-app-tools", "version": "0.151.0"},
        }
    if method != "tools/call":
        return {}
    params = request.get("params") or {}
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if name == "list_threads":
        return {"structuredContent": {"threads": []}, "isError": False}
    if name == "wait_threads":
        return {"structuredContent": {"done": True}, "isError": False}
    if name in {"read_thread", "get_thread", "thread_read"}:
        state = _bridge_state()
        messages = state.get("messages", [])
        turns = []
        for index, message in enumerate(messages):
            turns.append(
                {
                    "id": f"user-{index + 1}",
                    "role": "user",
                    "text": message.get("prompt", ""),
                    "createdAt": 901000 + index,
                }
            )
            turns.append(
                {
                    "id": f"assistant-{index + 1}",
                    "role": "assistant",
                    "status": "active",
                    "createdAt": 902000 + index,
                }
            )
        return {
            "structuredContent": {
                "threadId": arguments.get("threadId"),
                "hostId": arguments.get("hostId", "local"),
                "status": "active" if messages else "failed",
                "turns": turns,
            },
            "isError": False,
        }
    if name == "send_message_to_thread":
        state = _bridge_state()
        messages = state.setdefault("messages", [])
        message = {
            "threadId": arguments.get("threadId"),
            "prompt": arguments.get("prompt"),
            "hostId": arguments.get("hostId", "local"),
        }
        messages.append(message)
        state["messages"] = messages
        if os.environ.get("FAKE_KEEPGOING_LOSE_FIRST_SEND") == "1" and not state.get("lost_first_send", False):
            state["lost_first_send"] = True
        _write_bridge_state(state)
        message_log = os.environ.get("FAKE_KEEPGOING_MESSAGE_LOG")
        if message_log:
            with Path(message_log).open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(message, sort_keys=True) + "\n")
        if os.environ.get("FAKE_KEEPGOING_LOSE_FIRST_SEND") == "1" and state.get("lost_first_send") and len(messages) == 1:
            os._exit(17)
        return {"structuredContent": {"accepted": True}, "isError": False}
    return {"isError": True, "content": [{"type": "text", "text": "tool denied"}]}


def _write(payload: Any) -> None:
    with _write_lock:
        sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def _handle(request: dict[str, Any]) -> None:
    request_id = request.get("id")
    method = request.get("method")
    params = request.get("params") or {}

    role = os.environ.get("FAKE_KEEPGOING_ROLE")
    if role == "app" and method in {"initialize", "account/rateLimits/read", "thread/list", "thread/read", "thread/get"}:
        _write({"jsonrpc": "2.0", "id": request_id, "result": _fake_app_response(method, request)})
    elif role == "bridge" and method in {"initialize", "tools/list", "tools/call"}:
        if method == "tools/list":
            _write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "tools": [
                            {"name": "list_threads"},
                            {"name": "read_thread"},
                            {"name": "wait_threads"},
                            {"name": "send_message_to_thread"},
                        ]
                    },
                }
            )
        else:
            _write({"jsonrpc": "2.0", "id": request_id, "result": _fake_bridge_response(method, request)})
    elif method == "notify_then_echo":
        _write({"jsonrpc": "2.0", "method": "peer/event", "params": {"value": "notice"}})
        _write({"jsonrpc": "2.0", "id": request_id, "result": {"echo": params}})
    elif method == "echo":
        time.sleep(float(params.get("delay", 0)))
        _write({"jsonrpc": "2.0", "id": request_id, "result": {"echo": params.get("value")}})
    elif method == "remote_error":
        _write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32042,
                    "message": "remote validation failed",
                    "data": {"field": "value"},
                },
            }
        )
    elif method == "malformed":
        with _write_lock:
            sys.stdout.write("not-json\n")
            sys.stdout.flush()
    elif method == "stderr_exit":
        sys.stderr.write("Authorization: Bearer super-secret-token\n")
        sys.stderr.flush()
        os._exit(17)
    elif method == "exit":
        os._exit(23)
    elif method == "slow":
        time.sleep(float(params.get("delay", 0.25)))
        _write({"jsonrpc": "2.0", "id": request_id, "result": {"ok": True}})
    else:
        _write({"jsonrpc": "2.0", "id": request_id, "result": {"method": method}})


def main() -> int:
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("request must be an object")
        except (ValueError, json.JSONDecodeError):
            continue
        threading.Thread(target=_handle, args=(request,), daemon=True).start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
