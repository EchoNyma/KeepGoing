from __future__ import annotations

import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from keepgoing_chatgpt import cli
from keepgoing_chatgpt.models import RetryItem, RetryState
from keepgoing_chatgpt.state import RetryStateStore, RetryStoreState


class FakeLock:
    def __init__(self, acquired=True):
        self.acquired = acquired
        self.released = False

    def acquire(self, timeout_ms=-1):
        return self.acquired

    def release(self):
        self.released = True

    def close(self):
        return None


class FakeSupervisor:
    def __init__(self):
        self.started = False
        self.stopped = False
        self.ticks = 0

    def start(self):
        self.started = True

    def tick(self):
        self.ticks += 1
        raise KeyboardInterrupt

    def stop(self):
        self.stopped = True


class CliTests(unittest.TestCase):
    def test_bundled_version_is_read_from_the_discovered_executable(self):
        completed = subprocess.CompletedProcess(
            ["codex.exe", "--version"],
            0,
            stdout="codex-cli 0.151.0-alpha.7.2\n",
            stderr="",
        )

        with patch("keepgoing_chatgpt.cli.subprocess.run", return_value=completed) as run:
            version = cli._read_bundled_version(r"C:\Program Files\ChatGPT\codex.exe")

        self.assertEqual(version, "0.151.0-alpha.7.2")
        self.assertEqual(run.call_args.args[0], [r"C:\Program Files\ChatGPT\codex.exe", "--version"])

    def test_manual_start_handles_ctrl_c_and_reports_lifecycle(self):
        output = io.StringIO()
        lock = FakeLock()
        supervisor = FakeSupervisor()

        result = cli.main(
            [],
            output=output,
            runtime_factory=lambda args, store: (supervisor, {"codex_version": "0.151.0"}),
            monitor_lock_factory=lambda: lock,
            store_factory=lambda args: object(),
            sleep=lambda seconds: None,
        )

        self.assertEqual(result, 0)
        self.assertTrue(supervisor.started)
        self.assertTrue(supervisor.stopped)
        self.assertTrue(lock.released)
        self.assertIn("connected", output.getvalue().lower())
        self.assertIn("monitoring", output.getvalue().lower())
        self.assertIn("stopped", output.getvalue().lower())

    def test_status_reports_queue_without_starting_monitor_or_prompt_content(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        path = Path(temp_dir.name) / "state.json"
        store = RetryStateStore(path, mutex_name=f"KeepGoingCliStatus-{id(self)}")
        item = RetryItem(
            thread_id="thread-status",
            host_id="local",
            failed_turn_id="turn-status",
            failed_at_ms=1,
            reset_at_epoch=2,
            prompt="secret prompt must not print",
            state=RetryState.WAITING_FOR_RESET,
            created_at_ms=1,
            updated_at_ms=1,
        )
        store.save(RetryStoreState(observation_watermark_ms=1, queue=(item,)))
        output = io.StringIO()

        result = cli.main(
            ["--status"],
            output=output,
            store_factory=lambda args: store,
            monitor_lock_factory=lambda: FakeLock(),
        )

        self.assertEqual(result, 0)
        self.assertIn("thread-status:turn-status", output.getvalue())
        self.assertNotIn("secret prompt", output.getvalue())

    def test_legacy_flag_routes_to_legacy_uia_only_when_explicit(self):
        calls = []
        output = io.StringIO()

        result = cli.main(
            ["--legacy-uia", "--text", "continue"],
            output=output,
            legacy_runner=lambda argv: calls.append(argv) or 7,
        )

        self.assertEqual(result, 7)
        self.assertEqual(calls, [["--text", "continue"]])

    def test_duplicate_monitor_is_rejected_before_runtime_start(self):
        output = io.StringIO()
        supervisor = FakeSupervisor()

        result = cli.main(
            [],
            output=output,
            runtime_factory=lambda args, store: (supervisor, {}),
            monitor_lock_factory=lambda: FakeLock(acquired=False),
            store_factory=lambda args: object(),
        )

        self.assertNotEqual(result, 0)
        self.assertFalse(supervisor.started)
        self.assertIn("already running", output.getvalue().lower())

    def test_invalid_configuration_and_automatic_install_flags_are_rejected(self):
        for argv in (("--margin", "-1"), ("--install",), ("--uninstall",)):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit) as raised:
                    cli.main(list(argv), output=io.StringIO(), runtime_factory=lambda *_: None)
                self.assertEqual(raised.exception.code, 2)

    def test_process_id_targeting_requires_explicit_legacy_mode(self):
        with self.assertRaises(SystemExit) as raised:
            cli.main(["1234"], output=io.StringIO(), runtime_factory=lambda *_: None)

        self.assertEqual(raised.exception.code, 2)

    def test_primary_entrypoint_import_does_not_initialize_legacy_uia(self):
        code = "import chatgpt_attach, sys; print('legacy_uia' in sys.modules)"
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )

        self.assertEqual(result.stdout.strip(), "False")


if __name__ == "__main__":
    unittest.main()
