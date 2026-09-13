from __future__ import annotations

import os
import sys
import unittest


PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests.live_chatgpt_smoke import validate_dispatch_target


class LiveChatGPTSmokeTests(unittest.TestCase):
    def test_dispatch_requires_a_marked_disposable_task(self):
        task = {
            "id": "throwaway",
            "hostId": "local",
            "kind": "codex",
            "title": "KeepGoing LIVE SMOKE TEST (DISPOSABLE)",
        }

        self.assertIs(validate_dispatch_target("throwaway", {"threads": [task]}), task)

    def test_dispatch_accepts_the_same_marked_task_from_read_thread_shape(self):
        task = {
            "id": "throwaway",
            "hostId": "local",
            "kind": "codex",
            "title": "KeepGoing LIVE SMOKE TEST (DISPOSABLE)",
        }

        self.assertIs(validate_dispatch_target("throwaway", {"thread": task}), task)

    def test_dispatch_rejects_existing_personal_os_task_even_if_requested(self):
        task = {
            "id": "01a03adb-25a3-75a1-b6de-0f54a18dfaa3",
            "hostId": "local",
            "kind": "codex",
            "title": "Bewerte Personal OS Konzept",
        }

        with self.assertRaises(ValueError):
            validate_dispatch_target(task["id"], {"threads": [task]})

    def test_dispatch_rejects_unmarked_existing_task(self):
        task = {
            "id": "existing",
            "hostId": "local",
            "kind": "codex",
            "title": "An existing task",
        }

        with self.assertRaises(ValueError):
            validate_dispatch_target(task["id"], {"threads": [task]})


if __name__ == "__main__":
    unittest.main()
