from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from keepgoing_chatgpt.models import FailedTask, RateLimitBucket, RetryItem, RetryState
from keepgoing_chatgpt.state import RetryStateStore, RetryStoreState
from keepgoing_chatgpt.desktop_bridge import DesktopBridgeTransportUncertain
from keepgoing_chatgpt.supervisor import AutoResumeSupervisor, normalize_task_observation


class FakeClock:
    def __init__(self, epoch: int = 1_000):
        self.epoch = epoch

    def now_epoch(self) -> int:
        return self.epoch

    def now_ms(self) -> int:
        return self.epoch * 1000

    def advance(self, seconds: int) -> None:
        self.epoch += seconds


def failure(thread_id: str, turn_id: str, created_at_ms: int) -> FailedTask:
    return FailedTask(
        thread_id=thread_id,
        host_id="local",
        failed_turn_id=turn_id,
        failed_at_ms=created_at_ms,
        message="usage-limit failure",
        error_class="usage_limit",
        task_status="failed",
    )


class FakeAppServer:
    def __init__(self, clock: FakeClock, failures=(), bucket=None):
        self.clock = clock
        self.failures = list(failures)
        self.bucket = bucket
        self.scan_watermarks = []
        self.bucket_reads = 0
        self.fail_next_scan = False

    def find_five_hour_failures(self, *, observation_watermark_ms):
        self.scan_watermarks.append(observation_watermark_ms)
        if self.fail_next_scan:
            self.fail_next_scan = False
            raise ConnectionError("app server disconnected")
        return tuple(item for item in self.failures if item.failed_at_ms > observation_watermark_ms)

    def read_five_hour_bucket(self):
        self.bucket_reads += 1
        return self.bucket


class FakeBridge:
    def __init__(self, observations=None):
        self.observations = observations or {}
        self.sends = []
        self.reads = []
        self.fail_next_read = False
        self.send_outcomes = []

    def read_thread(self, thread_id, *, host_id=None, cursor=None):
        self.reads.append((thread_id, host_id))
        if self.fail_next_read:
            self.fail_next_read = False
            raise ConnectionError("bridge disconnected")
        return self.observations.get(thread_id, {"threadId": thread_id, "hostId": host_id or "local", "status": "failed", "turns": []})

    def send_message(self, thread_id, prompt, host_id=None):
        self.sends.append((thread_id, prompt, host_id))
        if self.send_outcomes:
            outcome = self.send_outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            if callable(outcome):
                outcome()
        return {"accepted": True}


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.clock = FakeClock()
        self.path = Path(self.temp_dir.name) / "state.json"
        self.store = RetryStateStore(self.path, now_ms=self.clock.now_ms, mutex_name=f"KeepGoingSupervisor-{id(self)}")

    def _supervisor(self, app, bridge, *, reconnect_factory=None, margin_seconds=10):
        self.store.save(RetryStoreState(observation_watermark_ms=0))
        supervisor = AutoResumeSupervisor(
            app,
            bridge,
            self.store,
            clock=self.clock,
            margin_seconds=margin_seconds,
            reconnect_factory=reconnect_factory,
        )
        supervisor.start()
        return supervisor

    def _ready_item(self, thread_id="thread-1", turn_id="failed-1"):
        return RetryItem(
            thread_id=thread_id,
            host_id="local",
            failed_turn_id=turn_id,
            failed_at_ms=900_000,
            reset_at_epoch=900,
            state=RetryState.READY,
            created_at_ms=900_000,
            updated_at_ms=900_000,
        )

    def test_scans_hidden_local_tasks_and_keeps_failures_separate(self):
        app = FakeAppServer(
            self.clock,
            [failure("hidden-a", "turn-a", 100), failure("hidden-b", "turn-b", 200)],
            RateLimitBucket(300, 2_000, reached=True),
        )
        supervisor = self._supervisor(app, FakeBridge())

        supervisor.tick()

        state = self.store.load()
        self.assertEqual([item.key for item in state.queue], ["hidden-a:turn-a", "hidden-b:turn-b"])
        self.assertEqual([item.thread_id for item in state.queue], ["hidden-a", "hidden-b"])

    def test_scanning_continues_while_first_item_waits_for_hours(self):
        app = FakeAppServer(
            self.clock,
            [failure("hidden-a", "turn-a", 100)],
            RateLimitBucket(300, 2_000, reached=True),
        )
        supervisor = self._supervisor(app, FakeBridge())
        supervisor.tick()
        app.failures.append(failure("hidden-b", "turn-b", self.clock.now_ms() + 1))

        self.clock.advance(1_000)
        supervisor.tick()

        self.assertEqual([item.key for item in self.store.load().queue], ["hidden-a:turn-a", "hidden-b:turn-b"])
        self.assertGreaterEqual(len(app.scan_watermarks), 2)

    def test_delayed_failure_visibility_remains_inside_watermark_overlap(self):
        app = FakeAppServer(
            self.clock,
            [],
            RateLimitBucket(300, 2_000, reached=True),
        )
        supervisor = self._supervisor(app, FakeBridge())

        supervisor.tick()
        app.failures.append(failure("thread-delayed", "turn-delayed", self.clock.now_ms() - 1_000))
        self.clock.advance(15)
        supervisor.tick()

        self.clock.advance(15)
        supervisor.tick()

        self.assertEqual([item.key for item in self.store.load().queue], ["thread-delayed:turn-delayed"])
        self.assertEqual(
            sum(event["event"] == "detected" for event in supervisor.audit_events),
            1,
        )

    def test_persisted_current_watermark_uses_effective_settlement_overlap(self):
        app = FakeAppServer(
            self.clock,
            [],
            RateLimitBucket(300, 2_000, reached=True),
        )
        self.store.save(RetryStoreState(observation_watermark_ms=self.clock.now_ms()))
        supervisor = AutoResumeSupervisor(app, FakeBridge(), self.store, clock=self.clock)
        supervisor.start()

        supervisor.tick()
        app.failures.append(failure("thread-upgrade", "turn-upgrade", self.clock.now_ms() - 1_000))
        self.clock.advance(15)
        supervisor.tick()

        self.assertEqual([item.key for item in self.store.load().queue], ["thread-upgrade:turn-upgrade"])

    def test_only_a_300_minute_bucket_schedules_a_retry(self):
        app = FakeAppServer(self.clock, [failure("thread-1", "turn-1", 100)], None)
        supervisor = self._supervisor(app, FakeBridge())

        supervisor.tick()

        self.assertEqual(self.store.load().queue, ())

    def test_missing_primary_bucket_does_not_advance_failure_watermark(self):
        app = FakeAppServer(self.clock, [failure("thread-1", "turn-1", 100)], None)
        supervisor = self._supervisor(app, FakeBridge())

        supervisor.tick()

        self.assertEqual(self.store.load().observation_watermark_ms, 0)
        app.bucket = RateLimitBucket(300, 2_000, reached=True)
        supervisor.tick()

        self.assertEqual([item.key for item in self.store.load().queue], ["thread-1:turn-1"])

    def test_authoritative_reset_and_margin_gate_readiness(self):
        app = FakeAppServer(
            self.clock,
            [failure("thread-1", "turn-1", 100)],
            RateLimitBucket(300, 1_010, reached=True),
        )
        supervisor = self._supervisor(app, FakeBridge())
        supervisor.tick()
        self.clock.advance(19)
        supervisor.tick()
        self.assertEqual(self.store.load().queue[0].state, RetryState.WAITING_FOR_RESET)

        app.bucket = RateLimitBucket(300, 1_010, reached=False)
        self.clock.advance(1)
        supervisor.tick()

        self.assertEqual(self.store.load().queue[0].state, RetryState.READY)
        self.assertGreaterEqual(app.bucket_reads, 1)

    def test_clock_jump_after_sleep_is_re_evaluated_by_wall_clock(self):
        app = FakeAppServer(
            self.clock,
            [failure("thread-1", "turn-1", 100)],
            RateLimitBucket(300, 1_010, reached=True),
        )
        supervisor = self._supervisor(app, FakeBridge())
        supervisor.tick()
        self.clock.advance(100_000)
        app.bucket = RateLimitBucket(300, 1_010, reached=False)

        supervisor.tick()

        self.assertEqual(self.store.load().queue[0].state, RetryState.READY)

    def test_newer_manual_user_turn_cancels_before_dispatch(self):
        app = FakeAppServer(self.clock, [], RateLimitBucket(300, 1_000, reached=False))
        bridge = FakeBridge(
            {
                "thread-1": {
                    "threadId": "thread-1",
                    "hostId": "local",
                    "status": "failed",
                    "turns": [{"id": "manual", "role": "user", "text": "I continued manually", "createdAt": 901_000}],
                }
            }
        )
        self.store.save(RetryStoreState(observation_watermark_ms=0, queue=(self._ready_item(),)))
        supervisor = AutoResumeSupervisor(app, bridge, self.store, clock=self.clock, margin_seconds=0)
        supervisor.start()

        supervisor.tick()

        self.assertEqual(self.store.load().queue[0].state, RetryState.CANCELLED)
        self.assertEqual(bridge.sends, [])

    def test_active_approval_and_user_input_states_cancel_before_dispatch(self):
        for status_key, status_value in (
            ("status", "active"),
            ("waitingForApproval", True),
            ("waitingForUserInput", True),
        ):
            with self.subTest(status_key=status_key):
                app = FakeAppServer(self.clock, [], RateLimitBucket(300, 1_000, reached=False))
                observation = {"threadId": "thread-1", "hostId": "local", "status": "failed", "turns": []}
                observation[status_key] = status_value
                bridge = FakeBridge({"thread-1": observation})
                self.store.save(RetryStoreState(observation_watermark_ms=0, queue=(self._ready_item(),)))
                supervisor = AutoResumeSupervisor(app, bridge, self.store, clock=self.clock, margin_seconds=0)
                supervisor.start()

                supervisor.tick()

                self.assertEqual(self.store.load().queue[0].state, RetryState.CANCELLED)
                self.assertEqual(bridge.sends, [])

    def test_ready_items_dispatch_one_at_a_time_in_fifo_order(self):
        app = FakeAppServer(self.clock, [], RateLimitBucket(300, 1_000, reached=False))
        bridge = FakeBridge(
            {
                "thread-1": {"threadId": "thread-1", "hostId": "local", "status": "failed", "turns": []},
                "thread-2": {"threadId": "thread-2", "hostId": "local", "status": "failed", "turns": []},
            }
        )
        first = self._ready_item("thread-1", "turn-1")
        second = self._ready_item("thread-2", "turn-2")
        self.store.save(RetryStoreState(observation_watermark_ms=0, queue=(first, second)))
        supervisor = AutoResumeSupervisor(app, bridge, self.store, clock=self.clock, margin_seconds=0)
        supervisor.start()

        supervisor.tick()
        self.assertEqual([send[0] for send in bridge.sends], ["thread-1"])
        bridge.observations["thread-1"] = {
            "threadId": "thread-1",
            "hostId": "local",
            "status": "active",
            "turns": [
                {"id": "user-new", "role": "user", "text": "keep going", "createdAt": 901_000},
                {"id": "assistant-new", "role": "assistant", "status": "active", "createdAt": 902_000},
            ],
        }
        supervisor.tick()
        supervisor.tick()

        self.assertEqual([send[0] for send in bridge.sends], ["thread-1", "thread-2"])

    def test_a_new_limit_uses_a_new_failed_turn_key(self):
        app = FakeAppServer(self.clock, [], RateLimitBucket(300, 1_000, reached=False))
        bridge = FakeBridge(
            {
                "thread-1": {
                    "threadId": "thread-1",
                    "hostId": "local",
                    "status": "active",
                    "turns": [
                        {"id": "user-new", "role": "user", "text": "keep going", "createdAt": 901_000},
                        {"id": "assistant-new", "role": "assistant", "status": "active", "createdAt": 902_000},
                    ],
                }
            }
        )
        item = self._ready_item()
        self.store.save(RetryStoreState(observation_watermark_ms=0, queue=(item,)))
        supervisor = AutoResumeSupervisor(app, bridge, self.store, clock=self.clock, margin_seconds=0)
        supervisor.start()
        supervisor.tick()
        supervisor.tick()
        app.failures.append(failure("thread-1", "turn-2", self.clock.now_ms() + 1))
        app.bucket = RateLimitBucket(300, 1_100, reached=True)

        supervisor.tick()

        self.assertIn("thread-1:turn-2", [queued.key for queued in self.store.load().queue])

    def test_app_loss_reconnects_on_a_later_tick_without_losing_state(self):
        app = FakeAppServer(self.clock, [failure("thread-1", "turn-1", 100)], RateLimitBucket(300, 2_000, reached=True))
        app.fail_next_scan = True
        bridge = FakeBridge()
        replacement_app = FakeAppServer(self.clock, [failure("thread-1", "turn-1", 100)], app.bucket)
        replacement_bridge = FakeBridge()
        reconnects = []

        def reconnect():
            reconnects.append(True)
            return replacement_app, replacement_bridge

        supervisor = self._supervisor(app, bridge, reconnect_factory=reconnect)
        supervisor.tick()
        self.assertEqual(self.store.load().queue, ())
        supervisor.tick()

        self.assertEqual(len(reconnects), 1)
        self.assertEqual(self.store.load().queue[0].thread_id, "thread-1")

    def test_audit_events_do_not_contain_transcript_text(self):
        app = FakeAppServer(self.clock, [failure("thread-1", "turn-1", 100)], RateLimitBucket(300, 2_000, reached=True))
        supervisor = self._supervisor(app, FakeBridge())

        supervisor.tick()

        serialized = repr(supervisor.audit_events)
        self.assertNotIn("usage-limit failure", serialized)
        self.assertTrue(any(event["event"] == "queued" for event in supervisor.audit_events))

    def test_confirmed_send_completes_only_after_new_user_and_assistant_progress(self):
        app = FakeAppServer(self.clock, [], RateLimitBucket(300, 1_000, reached=False))
        bridge = FakeBridge({"thread-1": {"threadId": "thread-1", "hostId": "local", "status": "failed", "turns": []}})
        self.store.save(RetryStoreState(observation_watermark_ms=0, queue=(self._ready_item(),)))
        supervisor = AutoResumeSupervisor(app, bridge, self.store, clock=self.clock, margin_seconds=0)
        supervisor.start()

        supervisor.tick()
        bridge.observations["thread-1"] = {
            "threadId": "thread-1",
            "hostId": "local",
            "status": "active",
            "turns": [
                {"id": "user-new", "role": "user", "text": "keep going", "createdAt": 901_000},
                {"id": "assistant-new", "role": "assistant", "status": "active", "createdAt": 902_000},
            ],
        }
        supervisor.tick()

        self.assertEqual(self.store.load().queue[0].state, RetryState.COMPLETED)
        self.assertEqual(len(bridge.sends), 1)

    def test_uncertain_send_that_arrived_is_verified_without_a_duplicate(self):
        app = FakeAppServer(self.clock, [], RateLimitBucket(300, 1_000, reached=False))
        bridge = FakeBridge({"thread-1": {"threadId": "thread-1", "hostId": "local", "status": "failed", "turns": []}})
        self.store.save(RetryStoreState(observation_watermark_ms=0, queue=(self._ready_item(),)))
        supervisor = AutoResumeSupervisor(
            app,
            bridge,
            self.store,
            clock=self.clock,
            margin_seconds=0,
            reconnect_factory=lambda: (app, bridge),
        )
        supervisor.start()

        def message_arrived():
            bridge.observations["thread-1"] = {
                "threadId": "thread-1",
                "hostId": "local",
                "status": "active",
                "turns": [
                    {"id": "user-new", "role": "user", "text": "keep going", "createdAt": 901_000},
                    {"id": "assistant-new", "role": "assistant", "status": "active", "createdAt": 902_000},
                ],
            }

        # The fake stores the message before the transport error is surfaced.
        def store_then_lose_response():
            message_arrived()
            raise DesktopBridgeTransportUncertain("response lost")

        bridge.send_outcomes.append(store_then_lose_response)
        supervisor.tick()

        self.assertEqual(len(bridge.sends), 1)
        supervisor.tick()

        self.assertEqual(self.store.load().queue[0].state, RetryState.COMPLETED)
        self.assertEqual(len(bridge.sends), 1)

    def test_uncertain_send_without_arrival_retries_boundedly_then_needs_attention(self):
        app = FakeAppServer(self.clock, [], RateLimitBucket(300, 1_000, reached=False))
        bridge = FakeBridge({"thread-1": {"threadId": "thread-1", "hostId": "local", "status": "failed", "turns": []}})
        bridge.send_outcomes = [
            DesktopBridgeTransportUncertain("lost-1"),
            DesktopBridgeTransportUncertain("lost-2"),
            DesktopBridgeTransportUncertain("lost-3"),
        ]
        self.store.save(RetryStoreState(observation_watermark_ms=0, queue=(self._ready_item(),)))
        supervisor = AutoResumeSupervisor(
            app,
            bridge,
            self.store,
            clock=self.clock,
            margin_seconds=0,
            reconnect_factory=lambda: (app, bridge),
        )
        supervisor.start()

        for _ in range(8):
            supervisor.tick()
            self.clock.advance(3)

        self.assertEqual(len(bridge.sends), 3)
        self.assertEqual(self.store.load().queue[0].state, RetryState.NEEDS_ATTENTION)

    def test_uncertain_send_without_exact_user_turn_does_not_accept_generic_real_progress(self):
        app = FakeAppServer(self.clock, [], RateLimitBucket(300, 1_000, reached=False))
        bridge = FakeBridge({"thread-1": {"threadId": "thread-1", "hostId": "local", "status": "failed", "turns": []}})
        bridge.send_outcomes = [DesktopBridgeTransportUncertain("response lost")]
        self.store.save(RetryStoreState(observation_watermark_ms=0, queue=(self._ready_item(),)))
        supervisor = AutoResumeSupervisor(
            app,
            bridge,
            self.store,
            clock=self.clock,
            margin_seconds=0,
            reconnect_factory=lambda: (app, bridge),
        )
        supervisor.start()
        supervisor.tick()
        bridge.observations["thread-1"] = {
            "schemaVersion": 4,
            "thread": {
                "id": "thread-1",
                "hostId": "local",
                "kind": "codex",
                "status": {"type": "idle"},
            },
            "turns": [
                {
                    "id": "turn-real",
                    "status": "completed",
                    "startedAt": 901,
                    "items": [{"type": "agentMessage", "phase": "final_answer", "text": "READY"}],
                }
            ],
        }

        supervisor.tick()

        self.assertEqual(self.store.load().queue[0].state, RetryState.VERIFYING)

    def test_confirmed_send_accepts_new_real_bridge_turn_summary(self):
        app = FakeAppServer(self.clock, [], RateLimitBucket(300, 1_000, reached=False))
        bridge = FakeBridge({"thread-1": {"threadId": "thread-1", "hostId": "local", "status": "failed", "turns": []}})
        self.store.save(RetryStoreState(observation_watermark_ms=0, queue=(self._ready_item(),)))
        supervisor = AutoResumeSupervisor(app, bridge, self.store, clock=self.clock, margin_seconds=0)
        supervisor.start()
        supervisor.tick()
        bridge.observations["thread-1"] = {
            "schemaVersion": 4,
            "thread": {
                "id": "thread-1",
                "hostId": "local",
                "kind": "codex",
                "status": {"type": "idle"},
            },
            "turns": [
                {
                    "id": "turn-real",
                    "status": "completed",
                    "startedAt": 901,
                    "items": [{"type": "agentMessage", "phase": "final_answer", "text": "READY"}],
                }
            ],
        }

        supervisor.tick()

        self.assertEqual(self.store.load().queue[0].state, RetryState.COMPLETED)

    def test_restart_from_confirmed_verifying_accepts_new_real_bridge_turn_summary(self):
        item = self._ready_item().evolve(
            state=RetryState.VERIFYING,
            dispatch_attempts=1,
            dispatch_confirmed=True,
        )
        app = FakeAppServer(self.clock, [], RateLimitBucket(300, 1_000, reached=False))
        bridge = FakeBridge(
            {
                "thread-1": {
                    "schemaVersion": 4,
                    "thread": {
                        "id": "thread-1",
                        "hostId": "local",
                        "kind": "codex",
                        "status": {"type": "idle"},
                    },
                    "turns": [
                        {
                            "id": "turn-real",
                            "status": "completed",
                            "startedAt": 901,
                            "items": [{"type": "agentMessage", "phase": "final_answer", "text": "READY"}],
                        }
                    ],
                }
            }
        )
        self.store.save(RetryStoreState(observation_watermark_ms=0, queue=(item,)))
        supervisor = AutoResumeSupervisor(app, bridge, self.store, clock=self.clock, margin_seconds=0)
        supervisor.start()

        supervisor.tick()

        self.assertEqual(self.store.load().queue[0].state, RetryState.COMPLETED)

    def test_restart_from_dispatching_verifies_before_any_resend(self):
        item = self._ready_item().evolve(state=RetryState.DISPATCHING, dispatch_attempts=1)
        self.store.save(RetryStoreState(observation_watermark_ms=0, queue=(item,)))
        app = FakeAppServer(self.clock, [], RateLimitBucket(300, 1_000, reached=False))
        bridge = FakeBridge(
            {
                "thread-1": {
                    "threadId": "thread-1",
                    "hostId": "local",
                    "status": "active",
                    "turns": [
                        {"id": "user-new", "role": "user", "text": "keep going", "createdAt": 901_000},
                        {"id": "assistant-new", "role": "assistant", "status": "active", "createdAt": 902_000},
                    ],
                }
            }
        )
        supervisor = AutoResumeSupervisor(app, bridge, self.store, clock=self.clock, margin_seconds=0)
        supervisor.start()

        supervisor.tick()

        self.assertEqual(self.store.load().queue[0].state, RetryState.COMPLETED)
        self.assertEqual(bridge.sends, [])

    def test_restart_from_verifying_does_not_resend_an_observed_message(self):
        item = self._ready_item().evolve(state=RetryState.VERIFYING, dispatch_attempts=1)
        self.store.save(RetryStoreState(observation_watermark_ms=0, queue=(item,)))
        app = FakeAppServer(self.clock, [], RateLimitBucket(300, 1_000, reached=False))
        bridge = FakeBridge(
            {
                "thread-1": {
                    "threadId": "thread-1",
                    "hostId": "local",
                    "status": "active",
                    "turns": [
                        {"id": "user-new", "role": "user", "text": "keep going", "createdAt": 901_000},
                        {"id": "assistant-new", "role": "assistant", "status": "active", "createdAt": 902_000},
                    ],
                }
            }
        )
        supervisor = AutoResumeSupervisor(app, bridge, self.store, clock=self.clock, margin_seconds=0)
        supervisor.start()

        supervisor.tick()

        self.assertEqual(self.store.load().queue[0].state, RetryState.COMPLETED)
        self.assertEqual(bridge.sends, [])

    def test_older_duplicate_user_text_does_not_satisfy_verification(self):
        app = FakeAppServer(self.clock, [], RateLimitBucket(300, 1_000, reached=False))
        bridge = FakeBridge(
            {
                "thread-1": {
                    "threadId": "thread-1",
                    "hostId": "local",
                    "status": "failed",
                    "turns": [
                        {"id": "old-user", "role": "user", "text": "keep going", "createdAt": 800},
                        {"id": "old-assistant", "role": "assistant", "status": "completed", "createdAt": 801},
                    ],
                }
            }
        )
        self.store.save(RetryStoreState(observation_watermark_ms=0, queue=(self._ready_item(),)))
        supervisor = AutoResumeSupervisor(app, bridge, self.store, clock=self.clock, margin_seconds=0)
        supervisor.start()

        supervisor.tick()
        self.assertEqual(self.store.load().queue[0].state, RetryState.VERIFYING)
        self.clock.advance(3)
        supervisor.tick()

        self.assertNotEqual(self.store.load().queue[0].state, RetryState.COMPLETED)

    def test_verification_requires_assistant_progress_after_the_new_user_turn(self):
        app = FakeAppServer(self.clock, [], RateLimitBucket(300, 1_000, reached=False))
        bridge = FakeBridge(
            {"thread-1": {"threadId": "thread-1", "hostId": "local", "status": "failed", "turns": []}}
        )
        self.store.save(RetryStoreState(observation_watermark_ms=0, queue=(self._ready_item(),)))
        supervisor = AutoResumeSupervisor(app, bridge, self.store, clock=self.clock, margin_seconds=0)
        supervisor.start()

        supervisor.tick()
        supervisor.tick()
        self.assertNotEqual(self.store.load().queue[0].state, RetryState.COMPLETED)
        bridge.observations["thread-1"]["turns"].extend(
            [
                {"id": "user-new", "role": "user", "text": "keep going", "createdAt": 901_000},
                {"id": "assistant-new", "role": "assistant", "status": "active", "createdAt": 902_000},
            ]
        )
        supervisor.tick()

        self.assertEqual(self.store.load().queue[0].state, RetryState.COMPLETED)

    def test_verification_accepts_equivalent_active_task_progress(self):
        app = FakeAppServer(self.clock, [], RateLimitBucket(300, 1_000, reached=False))
        bridge = FakeBridge(
            {"thread-1": {"threadId": "thread-1", "hostId": "local", "status": "failed", "turns": []}}
        )
        self.store.save(RetryStoreState(observation_watermark_ms=0, queue=(self._ready_item(),)))
        supervisor = AutoResumeSupervisor(app, bridge, self.store, clock=self.clock, margin_seconds=0)
        supervisor.start()

        supervisor.tick()
        bridge.observations["thread-1"] = {
            "threadId": "thread-1",
            "hostId": "local",
            "status": "active",
            "turns": [{"id": "user-new", "role": "user", "text": "keep going", "createdAt": 901_000}],
        }
        supervisor.tick()

        self.assertEqual(self.store.load().queue[0].state, RetryState.COMPLETED)

    def test_normalizes_real_bridge_turn_summaries_as_task_progress(self):
        raw = {
            "schemaVersion": 4,
            "thread": {
                "id": "thread-real",
                "hostId": "local",
                "kind": "codex",
                "status": {"type": "idle"},
            },
            "turns": [
                {
                    "id": "turn-real",
                    "status": "completed",
                    "startedAt": 1_700_000_000,
                    "items": [{"type": "agentMessage", "phase": "final_answer", "text": "READY"}],
                }
            ],
        }

        observation = normalize_task_observation(
            raw,
            expected_thread_id="thread-real",
            expected_host_id="local",
        )

        self.assertEqual(observation.status, "idle")
        self.assertEqual(observation.turns[0].role, "turn")
        self.assertEqual(observation.turns[0].text, "READY")

    def test_newest_first_bridge_history_keeps_the_actual_latest_limit_turn(self):
        raw = {
            "thread": {
                "id": "thread-real",
                "hostId": "local",
                "status": {"type": "idle"},
            },
            "turns": [
                {
                    "id": "limit-new",
                    "status": "failed",
                    "startedAt": 2_000,
                    "error": {
                        "message": "You've hit your usage limit. Try again at 11:10 PM."
                    },
                },
                {
                    "id": "limit-old",
                    "status": "failed",
                    "startedAt": 1_000,
                    "error": {
                        "message": "You've hit your usage limit. Try again at 11:10 PM."
                    },
                },
            ],
        }

        observation = normalize_task_observation(
            raw,
            expected_thread_id="thread-real",
            expected_host_id="local",
        )

        self.assertEqual([turn.turn_id for turn in observation.turns], ["limit-old", "limit-new"])
        self.assertEqual(observation.latest_failed_limit_turn_id, "limit-new")

    def test_live_remote_compact_limit_shape_is_recognized_during_preflight(self):
        raw = {
            "thread": {
                "id": "thread-real",
                "hostId": "local",
                "status": {"type": "failed"},
            },
            "turns": [
                {
                    "id": "limit-compact",
                    "status": "failed",
                    "startedAt": 2_000,
                    "completedAt": 2_100,
                    "error": {
                        "message": (
                            "Error running remote compact task: You've hit your usage limit. "
                            "Upgrade to Pro or try again at 4:44 AM."
                        ),
                        "codexErrorInfo": "usageLimitExceeded",
                    },
                }
            ],
        }

        observation = normalize_task_observation(
            raw,
            expected_thread_id="thread-real",
            expected_host_id="local",
        )

        self.assertEqual(observation.latest_failed_limit_turn_id, "limit-compact")

    def test_preflight_dispatches_when_bridge_returns_queued_limit_turn_first(self):
        app = FakeAppServer(self.clock, [], RateLimitBucket(300, 1_000, reached=False))
        bridge = FakeBridge(
            {
                "thread-1": {
                    "threadId": "thread-1",
                    "hostId": "local",
                    "status": "failed",
                    "turns": [
                        {
                            "id": "limit-new",
                            "status": "failed",
                            "startedAt": 2_000,
                            "error": {
                                "message": "You've hit your usage limit. Try again at 11:10 PM."
                            },
                        },
                        {
                            "id": "limit-old",
                            "status": "failed",
                            "startedAt": 1_000,
                            "error": {
                                "message": "You've hit your usage limit. Try again at 11:10 PM."
                            },
                        },
                    ],
                }
            }
        )
        queued = RetryItem(
            thread_id="thread-1",
            host_id="local",
            failed_turn_id="limit-new",
            failed_at_ms=2_000_000,
            reset_at_epoch=900,
            state=RetryState.READY,
            created_at_ms=2_000_000,
            updated_at_ms=2_000_000,
        )
        self.store.save(RetryStoreState(observation_watermark_ms=0, queue=(queued,)))
        supervisor = AutoResumeSupervisor(app, bridge, self.store, clock=self.clock, margin_seconds=0)
        supervisor.start()

        supervisor.tick()

        self.assertEqual(bridge.sends, [("thread-1", "keep going", "local")])
        self.assertEqual(self.store.load().queue[0].state, RetryState.VERIFYING)

    def test_preflight_cancels_when_bridge_contains_a_truly_newer_limit(self):
        app = FakeAppServer(self.clock, [], RateLimitBucket(300, 1_000, reached=False))
        bridge = FakeBridge(
            {
                "thread-1": {
                    "threadId": "thread-1",
                    "hostId": "local",
                    "status": "failed",
                    "turns": [
                        {
                            "id": "limit-newer",
                            "status": "failed",
                            "startedAt": 3_000,
                            "error": {
                                "message": "You've hit your usage limit. Try again at 11:20 PM."
                            },
                        },
                        {
                            "id": "limit-queued",
                            "status": "failed",
                            "startedAt": 2_000,
                            "error": {
                                "message": "You've hit your usage limit. Try again at 11:10 PM."
                            },
                        },
                    ],
                }
            }
        )
        queued = RetryItem(
            thread_id="thread-1",
            host_id="local",
            failed_turn_id="limit-queued",
            failed_at_ms=2_000_000,
            reset_at_epoch=900,
            state=RetryState.READY,
            created_at_ms=2_000_000,
            updated_at_ms=2_000_000,
        )
        self.store.save(RetryStoreState(observation_watermark_ms=0, queue=(queued,)))
        supervisor = AutoResumeSupervisor(app, bridge, self.store, clock=self.clock, margin_seconds=0)
        supervisor.start()

        supervisor.tick()

        item = self.store.load().queue[0]
        self.assertEqual(bridge.sends, [])
        self.assertEqual(item.state, RetryState.CANCELLED)
        self.assertEqual(item.last_error, "newer_limit_failure")


if __name__ == "__main__":
    unittest.main()
