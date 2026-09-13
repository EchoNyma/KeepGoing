import os
import sys
import unittest


PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from keepgoing_chatgpt.models import (
    FailedTask,
    RateLimitBucket,
    RetryItem,
    RetryState,
)


class RetryModelTests(unittest.TestCase):
    def test_failures_from_hidden_tasks_have_distinct_queue_keys(self):
        first = FailedTask(
            thread_id="thread-hidden-1",
            host_id="local",
            failed_turn_id="turn-1",
            failed_at_ms=1000,
        )
        second = FailedTask(
            thread_id="thread-hidden-2",
            host_id="local",
            failed_turn_id="turn-1",
            failed_at_ms=1001,
        )

        self.assertNotEqual(first.queue_key, second.queue_key)
        self.assertEqual(first.queue_key, "thread-hidden-1:turn-1")
        self.assertEqual(second.queue_key, "thread-hidden-2:turn-1")

    def test_duplicate_failed_turn_produces_same_key(self):
        first = FailedTask(
            thread_id="thread-1",
            host_id="local",
            failed_turn_id="turn-9",
            failed_at_ms=1000,
        )
        duplicate = FailedTask(
            thread_id="thread-1",
            host_id="local",
            failed_turn_id="turn-9",
            failed_at_ms=2000,
        )

        self.assertEqual(first.queue_key, duplicate.queue_key)

    def test_retry_item_retains_originating_thread_id(self):
        failure = FailedTask(
            thread_id="origin-thread",
            host_id="local",
            failed_turn_id="failed-turn",
            failed_at_ms=1234,
        )
        bucket = RateLimitBucket(
            window_duration_minutes=300,
            resets_at_epoch=9999,
            reached=True,
        )

        item = RetryItem.from_failure(failure, bucket, created_at_ms=2000)

        self.assertEqual(item.key, "origin-thread:failed-turn")
        self.assertEqual(item.thread_id, "origin-thread")
        self.assertEqual(item.failed_turn_id, "failed-turn")
        self.assertEqual(item.host_id, "local")
        self.assertEqual(item.reset_at_epoch, 9999)
        self.assertEqual(item.window_duration_minutes, 300)
        self.assertEqual(item.prompt, "keep going")
        self.assertIs(item.state, RetryState.WAITING_FOR_RESET)


if __name__ == "__main__":
    unittest.main()
