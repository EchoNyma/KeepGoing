from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from keepgoing_chatgpt.app_server import AppServerClient
from keepgoing_chatgpt.desktop_bridge import DesktopTaskBridge
from keepgoing_chatgpt.jsonrpc import JsonRpcClient
from keepgoing_chatgpt.state import RetryStateStore
from keepgoing_chatgpt.supervisor import AutoResumeSupervisor


PEER = str(Path(__file__).with_name("fake_protocol_peer.py"))


class FakeClock:
    def __init__(self, epoch=1000):
        self.epoch = epoch

    def now_epoch(self):
        return self.epoch

    def now_ms(self):
        return self.epoch * 1000

    def advance(self, seconds):
        self.epoch += seconds


class ChatGPTIntegrationTests(unittest.TestCase):
    def _scenario(self, *, manual=False):
        temp_dir = tempfile.TemporaryDirectory()
        root = Path(temp_dir.name)
        control_path = root / "control.json"
        bridge_state_path = root / "bridge-state.json"
        message_log_path = root / "messages.jsonl"
        control_path.write_text(json.dumps({"bucket_cleared": False}), encoding="utf-8")
        bridge_state_path.write_text(json.dumps({"messages": []}), encoding="utf-8")

        def write_control(**changes):
            values = json.loads(control_path.read_text(encoding="utf-8"))
            values.update(changes)
            control_path.write_text(json.dumps(values), encoding="utf-8")

        base_env = os.environ.copy()
        base_env.update(
            {
                "FAKE_KEEPGOING_CONTROL": str(control_path),
                "FAKE_KEEPGOING_BRIDGE_STATE": str(bridge_state_path),
                "FAKE_KEEPGOING_MESSAGE_LOG": str(message_log_path),
                "PYTHONPATH": PROJECT_ROOT + os.pathsep + base_env.get("PYTHONPATH", ""),
            }
        )

        def start_app():
            env = {**base_env, "FAKE_KEEPGOING_ROLE": "app"}
            rpc = JsonRpcClient([sys.executable, PEER], env=env, request_timeout=2)
            rpc.start()
            client = AppServerClient(rpc, bundled_version="0.151.0", request_timeout=2)
            client.initialize()
            return client

        def start_bridge():
            env = {**base_env, "FAKE_KEEPGOING_ROLE": "bridge"}
            if not manual:
                env["FAKE_KEEPGOING_LOSE_FIRST_SEND"] = "1"
            rpc = JsonRpcClient([sys.executable, PEER], env=env, request_timeout=2)
            rpc.start()
            bridge = DesktopTaskBridge(rpc, request_timeout=2)
            bridge.initialize()
            return bridge

        app = start_app()
        bridge = start_bridge()
        clock = FakeClock()
        state_path = root / "state.json"
        store = RetryStateStore(state_path, now_ms=clock.now_ms, mutex_name=f"KeepGoingIntegration-{id(self)}")
        supervisor = AutoResumeSupervisor(
            app,
            bridge,
            store,
            clock=clock,
            margin_seconds=2,
            reconnect_factory=lambda: (start_app(), start_bridge()),
        )
        supervisor.start()
        try:
            supervisor.tick()
            self.assertEqual(len(store.load().queue), 1)
            self.assertEqual(store.load().queue[0].thread_id, "hidden-task")
            self.assertEqual(store.load().queue[0].state.value, "waiting_for_reset")

            if manual:
                state = {"messages": [{"threadId": "hidden-task", "prompt": "manual continuation", "hostId": "local"}]}
                bridge_state_path.write_text(json.dumps(state), encoding="utf-8")
            clock.advance(5)
            supervisor.tick()
            self.assertEqual(store.load().queue[0].state.value, "waiting_for_reset")

            write_control(bucket_cleared=True)
            clock.advance(7)
            supervisor.tick()
            self.assertEqual(store.load().queue[0].state.value, "ready")

            supervisor.tick()
            if manual:
                self.assertEqual(store.load().queue[0].state.value, "cancelled")
            else:
                self.assertEqual(store.load().queue[0].state.value, "verifying")
                supervisor.tick()
                self.assertEqual(store.load().queue[0].state.value, "completed")

            messages = json.loads(bridge_state_path.read_text(encoding="utf-8"))["messages"]
            logged_messages = [
                json.loads(line)
                for line in message_log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ] if message_log_path.exists() else []
            if manual:
                self.assertEqual(len(messages), 1)
                self.assertEqual(messages[0]["prompt"], "manual continuation")
                self.assertEqual(logged_messages, [])
            else:
                self.assertEqual(len(messages), 1)
                self.assertEqual(messages[0], {"threadId": "hidden-task", "prompt": "keep going", "hostId": "local"})
                self.assertEqual(logged_messages, [messages[0]])
            self.assertNotIn("visible-task", {message["threadId"] for message in messages})
        finally:
            supervisor.stop()
            temp_dir.cleanup()

    def test_hidden_task_uncertain_send_reconnects_and_completes_once(self):
        self._scenario(manual=False)

    def test_manual_continuation_before_reset_cancels_automatic_send(self):
        self._scenario(manual=True)


if __name__ == "__main__":
    unittest.main()
