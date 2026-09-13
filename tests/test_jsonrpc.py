from __future__ import annotations

import os
import sys
import threading
import time
import unittest


PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from keepgoing_chatgpt.jsonrpc import (
    JsonRpcProcessExitedError,
    JsonRpcProtocolError,
    JsonRpcRemoteError,
    JsonRpcTimeoutError,
    JsonRpcClient,
)


PEER = os.path.join(os.path.dirname(__file__), "fake_protocol_peer.py")


class JsonRpcClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = JsonRpcClient([sys.executable, PEER], request_timeout=1.0)
        self.client.start()
        self.addCleanup(self.client.close)

    def test_request_ids_correlate_out_of_order_responses(self):
        results: dict[str, object] = {}

        def request(name: str, delay: float) -> None:
            results[name] = self.client.request("echo", {"value": name, "delay": delay})

        first = threading.Thread(target=request, args=("first", 0.2))
        second = threading.Thread(target=request, args=("second", 0.0))
        first.start()
        time.sleep(0.02)
        second.start()
        first.join()
        second.join()

        self.assertEqual(results, {"first": {"echo": "first"}, "second": {"echo": "second"}})

    def test_notifications_are_delivered_without_stealing_response(self):
        result = self.client.request("notify_then_echo", {"value": "payload"})

        self.assertEqual(result, {"echo": {"value": "payload"}})
        self.assertEqual(self.client.drain_notifications(), [("peer/event", {"value": "notice"})])

    def test_structured_remote_error_is_typed(self):
        with self.assertRaises(JsonRpcRemoteError) as raised:
            self.client.request("remote_error")

        self.assertEqual(raised.exception.code, -32042)
        self.assertEqual(raised.exception.message, "remote validation failed")
        self.assertEqual(raised.exception.data, {"field": "value"})

    def test_malformed_json_fails_the_pending_request(self):
        with self.assertRaises(JsonRpcProtocolError):
            self.client.request("malformed")

    def test_timeout_does_not_poison_later_responses(self):
        with self.assertRaises(JsonRpcTimeoutError):
            self.client.request("slow", {"delay": 0.2}, timeout=0.03)

        self.assertEqual(self.client.request("echo", {"value": "after-timeout"}), {"echo": "after-timeout"})

    def test_process_exit_includes_redacted_stderr(self):
        with self.assertRaises(JsonRpcProcessExitedError) as raised:
            self.client.request("stderr_exit")

        error_text = str(raised.exception)
        self.assertIn("Authorization: Bearer [REDACTED]", error_text)
        self.assertNotIn("super-secret-token", error_text)
        self.assertEqual(raised.exception.returncode, 17)

    def test_shutdown_joins_reader_threads_and_process(self):
        self.client.request("echo", {"value": "before-close"})
        self.client.close()

        self.assertIsNotNone(self.client.process)
        self.assertIsNotNone(self.client.process.poll())
        self.assertFalse(self.client.reader_thread.is_alive())
        self.assertFalse(self.client.stderr_thread.is_alive())


if __name__ == "__main__":
    unittest.main()
