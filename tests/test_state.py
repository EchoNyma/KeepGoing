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

from keepgoing_chatgpt.models import RetryItem, RetryState
from keepgoing_chatgpt.state import (
    RetryStoreState,
    StateCorruptError,
    StateSchemaError,
    StateStoreLocked,
    RetryStateStore,
)


class RetryStateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "chatgpt-auto-resume-state.json"
        self.now_ms = 20 * 24 * 3600 * 1000
        self.store = RetryStateStore(self.path, now_ms=lambda: self.now_ms, mutex_name=f"KeepGoingTest-{id(self)}")

    def _item(self, state: RetryState, *, updated_at_ms: int | None = None, key_suffix: str = "turn") -> RetryItem:
        updated = self.now_ms if updated_at_ms is None else updated_at_ms
        return RetryItem(
            thread_id="thread-" + key_suffix,
            host_id="local",
            failed_turn_id="failed-" + key_suffix,
            failed_at_ms=100,
            reset_at_epoch=123,
            state=state,
            created_at_ms=100,
            updated_at_ms=updated,
        )

    def test_first_load_creates_a_durable_observation_watermark(self):
        state = self.store.load()

        self.assertEqual(state.observation_watermark_ms, self.now_ms)
        self.assertEqual(state.queue, ())
        self.assertTrue(self.path.exists())

    def test_round_trip_preserves_every_queue_state(self):
        items = tuple(self._item(state, key_suffix=str(index)) for index, state in enumerate(RetryState))
        items = items + (self._item(RetryState.VERIFYING, key_suffix="confirmed").evolve(dispatch_confirmed=True),)
        expected = RetryStoreState(observation_watermark_ms=50, queue=items)

        self.store.save(expected)

        loaded = self.store.load()
        self.assertEqual(loaded.observation_watermark_ms, 50)
        self.assertEqual(tuple(item.state for item in loaded.queue[:-1]), tuple(RetryState))
        self.assertEqual(tuple(item.key for item in loaded.queue), tuple(item.key for item in items))
        self.assertTrue(loaded.queue[-1].dispatch_confirmed)

    def test_save_uses_same_directory_atomic_replace(self):
        replacements = []

        def replace(source, destination):
            replacements.append((Path(source).parent, Path(destination)))
            os.replace(source, destination)

        store = RetryStateStore(self.path, now_ms=lambda: self.now_ms, mutex_name=f"KeepGoingReplace-{id(self)}", replace=replace)
        store.save(RetryStoreState(observation_watermark_ms=10))

        self.assertEqual(replacements[0][0], self.path.parent)
        self.assertEqual(replacements[0][1], self.path)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_interruption_before_replace_keeps_previous_state(self):
        self.store.save(RetryStoreState(observation_watermark_ms=10))

        def fail_replace(source, destination):
            raise OSError("simulated interruption")

        failing = RetryStateStore(self.path, now_ms=lambda: self.now_ms, mutex_name=f"KeepGoingInterrupt-{id(self)}", replace=fail_replace)
        with self.assertRaises(OSError):
            failing.save(RetryStoreState(observation_watermark_ms=99))

        self.assertEqual(self.store.load().observation_watermark_ms, 10)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_schema_version_mismatch_is_rejected_without_erasing_file(self):
        self.path.write_text(json.dumps({"schema_version": 99, "observation_watermark_ms": 1, "queue": []}), encoding="utf-8")

        with self.assertRaises(StateSchemaError):
            self.store.load()

        self.assertIn('"schema_version": 99', self.path.read_text(encoding="utf-8"))

    def test_corrupt_json_is_quarantined_and_fails_closed(self):
        self.path.write_text("{not-json", encoding="utf-8")

        with self.assertRaises(StateCorruptError):
            self.store.load()

        quarantined = list(self.path.parent.glob(self.path.name + ".corrupt-*"))
        self.assertEqual(len(quarantined), 1)
        self.assertTrue(self.path.exists())

    def test_named_mutex_excludes_a_second_supervisor(self):
        first = RetryStateStore(self.path, mutex_name=f"KeepGoingMutex-{id(self)}")
        second = RetryStateStore(self.path, mutex_name=f"KeepGoingMutex-{id(self)}")

        with first.lock():
            with self.assertRaises(StateStoreLocked):
                with second.lock(timeout_ms=0):
                    pass

    def test_terminal_entries_compact_after_seven_days_but_remain_deduplicated(self):
        old = self._item(RetryState.COMPLETED, updated_at_ms=self.now_ms - 8 * 24 * 3600 * 1000, key_suffix="old")
        recent = self._item(RetryState.CANCELLED, updated_at_ms=self.now_ms, key_suffix="recent")
        pending = self._item(RetryState.WAITING_FOR_RESET, updated_at_ms=self.now_ms - 20 * 24 * 3600 * 1000, key_suffix="pending")
        state = RetryStoreState(observation_watermark_ms=1, queue=(old, recent, pending))

        compacted = self.store.compact(state)

        self.assertEqual(tuple(item.key for item in compacted.queue), (recent.key, pending.key))
        self.assertEqual(compacted.dedupe_ledger[0]["key"], old.key)


if __name__ == "__main__":
    unittest.main()
