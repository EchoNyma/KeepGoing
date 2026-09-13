from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from keepgoing_chatgpt.desktop_bridge import (
    DesktopBridgeContractError,
    DesktopBridgeMethodDenied,
    DesktopBridgeToolError,
    DesktopBridgeTransportUncertain,
    DesktopTaskBridge,
)
from keepgoing_chatgpt.jsonrpc import JsonRpcProcessExitedError


FIXTURES = Path(__file__).parent / "fixtures"


class FixtureRpc:
    def __init__(self, *, tools=None, tool_results=None, error=None):
        self.tools = tools if tools is not None else json.loads((FIXTURES / "bridge_tools.jsonl").read_text())["result"]
        self.tool_results = tool_results or {}
        self.error = error
        self.calls = []

    def request(self, method, params=None, timeout=None):
        self.calls.append((method, params))
        if self.error is not None:
            raise self.error
        if method == "initialize":
            return json.loads((FIXTURES / "bridge_initialize.jsonl").read_text())["result"]
        if method == "tools/list":
            return self.tools
        if method == "tools/call":
            name = params["name"]
            result = self.tool_results.get(name, {"structuredContent": {"ok": True}, "isError": False})
            if isinstance(result, Exception):
                raise result
            return result
        raise AssertionError(f"unexpected method {method}")

    def send_notification(self, method, params=None):
        self.calls.append((method, params))


def make_bridge(rpc=None):
    rpc = rpc or FixtureRpc()
    bridge = DesktopTaskBridge(rpc)
    bridge.initialize()
    return bridge, rpc


class DesktopTaskBridgeTests(unittest.TestCase):
    def test_mcp_initialization_and_required_tool_discovery(self):
        bridge, rpc = make_bridge()

        self.assertTrue(bridge.ready_for_dispatch)
        self.assertEqual(bridge.protocol_version, "2024-11-05")
        self.assertIn(("notifications/initialized", None), rpc.calls)
        self.assertEqual(bridge.read_tool_name, "read_thread")

    def test_missing_send_tool_disables_dispatch(self):
        tools = {"tools": [{"name": name} for name in ("list_threads", "read_thread", "wait_threads")]}
        with self.assertRaises(DesktopBridgeContractError):
            make_bridge(FixtureRpc(tools=tools))

    def test_send_forwards_only_exact_task_arguments(self):
        bridge, rpc = make_bridge()

        bridge.send_message("thread-exact", "keep going", host_id="local")

        self.assertEqual(
            rpc.calls[-1],
            (
                "tools/call",
                {
                    "name": "send_message_to_thread",
                    "arguments": {"threadId": "thread-exact", "prompt": "keep going", "hostId": "local"},
                },
            ),
        )

    def test_tool_calls_include_executor_metadata_without_changing_target_arguments(self):
        rpc = FixtureRpc()
        bridge = DesktopTaskBridge(rpc, executor_thread_id="executor-thread")
        bridge.initialize()

        bridge.send_message("thread-exact", "keep going", host_id="local")

        self.assertEqual(
            rpc.calls[-1],
            (
                "tools/call",
                {
                    "name": "send_message_to_thread",
                    "arguments": {"threadId": "thread-exact", "prompt": "keep going", "hostId": "local"},
                    "_meta": {"threadId": "executor-thread"},
                },
            ),
        )

    def test_structured_tool_error_is_a_confirmed_rejection(self):
        bridge, _ = make_bridge(
            FixtureRpc(
                tool_results={
                    "send_message_to_thread": {
                        "isError": True,
                        "content": [{"type": "text", "text": "task is waiting for approval"}],
                    }
                }
            )
        )

        with self.assertRaises(DesktopBridgeToolError) as raised:
            bridge.send_message("thread-exact", "keep going")

        self.assertTrue(raised.exception.confirmed)
        self.assertIn("waiting for approval", str(raised.exception))

    def test_transport_exit_is_surface_as_uncertain_send(self):
        bridge, rpc = make_bridge()
        rpc.error = JsonRpcProcessExitedError(7, "bridge stopped")

        with self.assertRaises(DesktopBridgeTransportUncertain):
            bridge.send_message("thread-exact", "keep going")

    def test_unknown_tool_is_denied_before_rpc_call(self):
        bridge, rpc = make_bridge()
        call_count = len(rpc.calls)

        with self.assertRaises(DesktopBridgeMethodDenied):
            bridge.call_tool("archive_task", {})

        self.assertEqual(len(rpc.calls), call_count)

    def test_public_send_tool_call_rejects_extra_writer_arguments(self):
        bridge, rpc = make_bridge()
        call_count = len(rpc.calls)

        with self.assertRaises(DesktopBridgeContractError):
            bridge.call_tool(
                "send_message_to_thread",
                {"threadId": "thread-exact", "prompt": "keep going", "model": "gpt-5"},
            )

        self.assertEqual(len(rpc.calls), call_count)

    def test_read_helpers_keep_exact_thread_context(self):
        bridge, rpc = make_bridge(
            FixtureRpc(
                tool_results={
                    "read_thread": {"structuredContent": {"threadId": "thread-exact", "turns": []}, "isError": False},
                    "wait_threads": {"structuredContent": {"done": True}, "isError": False},
                }
            )
        )

        observation = bridge.read_thread("thread-exact", host_id="local")
        bridge.wait_for_thread("thread-exact", host_id="local", timeout_ms=250)

        self.assertEqual(observation["threadId"], "thread-exact")
        self.assertEqual(
            rpc.calls[-2],
            (
                "tools/call",
                {
                    "name": "read_thread",
                    "arguments": {"threadId": "thread-exact", "hostId": "local", "turnLimit": 10},
                },
            ),
        )
        self.assertEqual(
            rpc.calls[-1],
            (
                "tools/call",
                {
                    "name": "wait_threads",
                    "arguments": {"targets": [{"threadId": "thread-exact", "hostId": "local"}], "timeoutMs": 250},
                },
            ),
        )

    def test_list_threads_rejects_limits_above_the_bridge_contract(self):
        bridge, _ = make_bridge()

        with self.assertRaises(ValueError):
            bridge.list_threads(limit=51)

    def test_reconnect_repeats_handshake_and_tool_gate(self):
        bridge, rpc = make_bridge()
        replacement = FixtureRpc()

        bridge.reconnect(replacement)

        self.assertTrue(bridge.ready_for_dispatch)
        self.assertEqual([method for method, _ in replacement.calls], ["initialize", "notifications/initialized", "tools/list"])
        self.assertNotEqual(rpc, replacement)


if __name__ == "__main__":
    unittest.main()
