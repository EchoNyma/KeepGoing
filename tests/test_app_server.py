from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from keepgoing_chatgpt.app_server import (
    AppServerClient,
    AppServerContractError,
    AppServerMethodDenied,
    TaskTurn,
    classify_failed_turn,
)
from keepgoing_chatgpt.models import RateLimitBucket


FIXTURES = Path(__file__).parent / "fixtures"


class FixtureRpc:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self.closed = False

    def request(self, method, params=None, timeout=None):
        self.calls.append((method, params))
        response = self.responses[method]
        if isinstance(response, list):
            if not response:
                raise AssertionError(f"no fixture response left for {method}")
            return response.pop(0)
        if callable(response):
            return response(params)
        return response

    def send_notification(self, method, params=None):
        self.calls.append((method, params))

    def close(self):
        self.closed = True


def fixture_result(name):
    line = (FIXTURES / name).read_text(encoding="utf-8").strip()
    return json.loads(line)["result"]


class AppServerClientTests(unittest.TestCase):
    def test_close_closes_the_underlying_transport(self):
        rpc = FixtureRpc({})
        client = AppServerClient(rpc)

        client.close()

        self.assertTrue(rpc.closed)

    def test_initialize_validates_the_bundled_version_and_codex_home(self):
        rpc = FixtureRpc({"initialize": fixture_result("app_server_initialize.jsonl")})
        client = AppServerClient(rpc, bundled_version="0.151.0-alpha.7.2")

        info = client.initialize()

        self.assertEqual(info.version, "0.151.0-alpha.7.2")
        self.assertEqual(info.codex_home, r"C:\Users\Marcu\.codex")
        self.assertEqual(rpc.calls[0][0], "initialize")
        self.assertEqual(rpc.calls[1][0], "initialized")

    def test_initialize_rejects_a_global_or_mismatched_codex_version(self):
        rpc = FixtureRpc({"initialize": fixture_result("app_server_initialize.jsonl")})
        client = AppServerClient(rpc, bundled_version="0.145.0")

        with self.assertRaises(AppServerContractError):
            client.initialize()

        self.assertEqual([method for method, _ in rpc.calls], ["initialize"])

    def test_initialize_rejects_a_codex_home_different_from_the_explicit_binding(self):
        rpc = FixtureRpc({"initialize": fixture_result("app_server_initialize.jsonl")})
        client = AppServerClient(rpc, expected_codex_home=r"C:\Users\Other\.codex")

        with self.assertRaises(AppServerContractError):
            client.initialize()

    def test_initialize_extracts_the_bundled_version_from_user_agent(self):
        result = fixture_result("app_server_initialize.jsonl")
        result.pop("version")
        result["userAgent"] = "keepgoing-readonly-probe/0.151.0-alpha.7.2 (Windows)"
        rpc = FixtureRpc({"initialize": result})
        client = AppServerClient(rpc, bundled_version="0.151.0-alpha.7.2")

        info = client.initialize()

        self.assertEqual(info.version, "0.151.0-alpha.7.2")

    def test_only_the_primary_300_minute_bucket_is_selected(self):
        rpc = FixtureRpc({"account/rateLimits/read": fixture_result("app_server_rate_limits.jsonl")})
        client = AppServerClient(rpc)

        bucket = client.read_five_hour_bucket()

        self.assertIsInstance(bucket, RateLimitBucket)
        self.assertEqual(bucket.window_duration_minutes, 300)
        self.assertEqual(bucket.resets_at_epoch, 1790000123)
        self.assertTrue(bucket.reached)

    def test_weekly_only_limits_do_not_schedule_a_five_hour_retry(self):
        result = fixture_result("app_server_rate_limits.jsonl")
        result["rateLimits"] = {"primary": result["rateLimits"]["weekly"]}
        rpc = FixtureRpc({"account/rateLimits/read": result})
        client = AppServerClient(rpc)

        self.assertIsNone(client.read_five_hour_bucket())

    def test_paginated_history_is_read_and_failure_keeps_exact_ids(self):
        first_page = fixture_result("app_server_failed_thread.jsonl")
        first_page["thread"]["turns"] = []
        first_page["nextCursor"] = "cursor-1"
        second_page = fixture_result("app_server_failed_thread.jsonl")
        def read_page(params):
            if params["threadId"] == "thread-hidden":
                return second_page if params.get("cursor") == "cursor-1" else first_page
            return {
                "thread": {
                    "id": "thread-visible",
                    "kind": "codex",
                    "hostId": "local",
                    "status": "completed",
                    "turns": [],
                },
                "nextCursor": None,
            }

        rpc = FixtureRpc(
            {
                "thread/list": {
                    "data": [
                        {"id": "thread-hidden", "kind": "codex", "status": "failed", "hostId": "local"},
                        {"id": "thread-visible", "kind": "codex", "status": "completed", "hostId": "local"},
                    ],
                    "nextCursor": None,
                },
                "thread/read": read_page,
            }
        )
        client = AppServerClient(rpc)

        failures = client.find_five_hour_failures(observation_watermark_ms=1789990000000)

        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].thread_id, "thread-hidden")
        self.assertEqual(failures[0].failed_turn_id, "turn-failed")
        self.assertEqual(failures[0].host_id, "local")
        reads = [params for method, params in rpc.calls if method == "thread/read" and params["threadId"] == "thread-hidden"]
        self.assertEqual(
            reads,
            [
                {"threadId": "thread-hidden", "includeTurns": True},
                {"threadId": "thread-hidden", "includeTurns": True, "cursor": "cursor-1"},
            ],
        )

    def test_long_running_turn_is_detected_by_failure_completion_time(self):
        rpc = FixtureRpc(
            {
                "thread/list": {
                    "data": [
                        {"id": "thread-long", "kind": "codex", "status": "failed", "hostId": "local"}
                    ],
                    "nextCursor": None,
                },
                "thread/read": {
                    "thread": {
                        "id": "thread-long",
                        "kind": "codex",
                        "hostId": "local",
                        "status": "failed",
                        "turns": [
                            {
                                "id": "turn-historical",
                                "status": "failed",
                                "startedAt": 500,
                                "completedAt": 1_400,
                                "error": {
                                    "type": "usage_limit",
                                    "message": "You've hit your usage limit. Try again at 10:10 PM.",
                                },
                            },
                            {
                                "id": "turn-long",
                                "status": "failed",
                                "startedAt": 1_000,
                                "completedAt": 2_000,
                                "error": {
                                    "type": "usage_limit",
                                    "message": "You've hit your usage limit. Try again at 11:10 PM.",
                                },
                            }
                        ],
                    },
                    "nextCursor": None,
                },
            }
        )
        client = AppServerClient(rpc)

        failures = client.find_five_hour_failures(observation_watermark_ms=1_500_000)

        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].failed_turn_id, "turn-long")
        self.assertEqual(failures[0].failed_at_ms, 2_000_000)

    def test_live_remote_compact_usage_limit_shape_is_classified(self):
        rpc = FixtureRpc(
            {
                "thread/list": {
                    "data": [
                        {"id": "thread-compact", "kind": "codex", "status": "failed", "hostId": "local"}
                    ],
                    "nextCursor": None,
                },
                "thread/read": {
                    "thread": {
                        "id": "thread-compact",
                        "kind": "codex",
                        "hostId": "local",
                        "status": "failed",
                        "turns": [
                            {
                                "id": "turn-compact",
                                "status": "failed",
                                "startedAt": 1_000,
                                "completedAt": 2_000,
                                "error": {
                                    "message": (
                                        "Error running remote compact task: You've hit your usage limit. "
                                        "Upgrade to Pro or try again at 4:44 AM."
                                    ),
                                    "codexErrorInfo": "usageLimitExceeded",
                                },
                            }
                        ],
                    },
                    "nextCursor": None,
                },
            }
        )
        client = AppServerClient(rpc)

        failures = client.find_five_hour_failures(observation_watermark_ms=1_500_000)

        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].failed_turn_id, "turn-compact")
        self.assertEqual(failures[0].error_class, "usageLimitExceeded")

    def test_task_identity_fields_are_required(self):
        entry = {"kind": "codex", "hostId": "local", "status": "failed"}
        client = AppServerClient(FixtureRpc({"thread/list": {"data": [entry], "nextCursor": None}}))

        with self.assertRaises(AppServerContractError):
            client.list_local_tasks()

    def test_thread_list_scope_supplies_local_codex_identity_when_fields_are_omitted(self):
        entry = {"id": "thread-hidden", "status": "failed"}
        client = AppServerClient(FixtureRpc({"thread/list": {"data": [entry], "nextCursor": None}}))

        tasks = client.list_local_tasks()

        self.assertEqual((tasks[0].thread_id, tasks[0].host_id, tasks[0].kind), ("thread-hidden", "local", "codex"))

    def test_thread_list_rejects_explicit_identity_outside_its_local_codex_scope(self):
        for field, value in (("hostId", "remote"), ("kind", "chatgpt")):
            with self.subTest(field=field):
                entry = {"id": "thread-hidden", "status": "failed", field: value}
                client = AppServerClient(FixtureRpc({"thread/list": {"data": [entry], "nextCursor": None}}))

                with self.assertRaises(AppServerContractError):
                    client.list_local_tasks()

    def test_malformed_task_list_entry_fails_closed(self):
        client = AppServerClient(FixtureRpc({"thread/list": {"data": [None], "nextCursor": None}}))

        with self.assertRaises(AppServerContractError):
            client.list_local_tasks()

    def test_read_rejects_a_task_identity_mismatch(self):
        client = AppServerClient(
            FixtureRpc(
                {
                    "thread/read": {
                        "thread": {
                            "id": "other-thread",
                            "kind": "codex",
                            "hostId": "local",
                            "status": "completed",
                            "turns": [],
                        },
                        "nextCursor": None,
                    }
                }
            )
        )

        with self.assertRaises(AppServerContractError):
            client.read_task_history("requested-thread")

    def test_read_may_use_an_already_validated_list_identity_when_read_omits_host_and_kind(self):
        client = AppServerClient(
            FixtureRpc(
                {
                    "thread/read": {
                        "thread": {
                            "id": "thread-hidden",
                            "status": "completed",
                            "turns": [],
                        },
                        "nextCursor": None,
                    }
                }
            )
        )

        with self.assertRaises(AppServerContractError):
            client.read_task_history("thread-hidden")

        client = AppServerClient(
            FixtureRpc(
                {
                    "thread/read": {
                        "thread": {
                            "id": "thread-hidden",
                            "status": "completed",
                            "turns": [],
                        },
                        "nextCursor": None,
                    }
                }
            )
        )

        task = client.read_task_history("thread-hidden", expected_host_id="local", expected_kind="codex")

        self.assertEqual((task.thread_id, task.host_id, task.kind), ("thread-hidden", "local", "codex"))

    def test_server_classified_and_narrow_message_failures_are_distinguished(self):
        classified = TaskTurn(
            turn_id="t1",
            status="failed",
            created_at_ms=100,
            error_class="usage_limit",
            error_message="opaque server message",
        )
        german = TaskTurn(
            turn_id="t2",
            status="failed",
            created_at_ms=100,
            error_message="Nutzungslimit erreicht. Wieder verfügbar in 20 Minuten.",
        )
        ordinary = TaskTurn(
            turn_id="t3",
            status="failed",
            created_at_ms=100,
            error_message="The answer mentions a usage limit in ordinary conversation.",
        )
        weekly = TaskTurn(
            turn_id="t4",
            status="failed",
            created_at_ms=100,
            error_class="weekly_limit",
            error_message="Weekly usage limit reached; try again next week.",
        )

        self.assertTrue(classify_failed_turn(classified))
        self.assertTrue(classify_failed_turn(german))
        self.assertFalse(classify_failed_turn(ordinary))
        self.assertFalse(classify_failed_turn(weekly))

    def test_desktop_usage_limit_message_with_upgrade_links_is_classified(self):
        turn = TaskTurn(
            turn_id="t5",
            status="failed",
            created_at_ms=100,
            error_message=(
                "You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), "
                "visit https://chatgpt.com/codex/settings/usage to purchase more credits or try again at 9:27 AM."
            ),
        )

        self.assertTrue(classify_failed_turn(turn))

    def test_write_methods_are_denied_before_the_rpc_is_called(self):
        rpc = FixtureRpc({})
        client = AppServerClient(rpc)

        with self.assertRaises(AppServerMethodDenied):
            client.request("thread/resume", {"threadId": "thread-hidden"})

        self.assertEqual(rpc.calls, [])


if __name__ == "__main__":
    unittest.main()
