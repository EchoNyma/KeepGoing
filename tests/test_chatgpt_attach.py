import ctypes
from ctypes import wintypes
import os
import sys
import unittest
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from keepgoing_chatgpt import legacy_uia as chatgpt_attach


class ChatGPTRateLimitDetectionTests(unittest.TestCase):
    def test_detects_english_rate_limit_messages(self):
        future_time = (datetime.now() + chatgpt_attach.timedelta(hours=2)).strftime("%I:%M %p").lstrip("0")
        future_date = datetime.now() + timedelta(days=2)
        future_date_text = f"{future_date.strftime('%b')} {future_date.day}, {future_date.year}"
        samples = [
            f"You've reached your current usage limit for GPT-4o. Try again after {future_time}.",
            "Rate limit reached. Try again in 45 minutes.",
            "You have hit your 3-hour limit. Resets in 20 mins.",
            "Too many requests in 1 hour. Try again later.",
            "Usage limit exceeded for this model.",
            f"You've hit your usage limit. Upgrade your plan or add credits to continue, or try again at {future_date_text}, 8:18 PM.",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertTrue(chatgpt_attach.is_rate_limited(sample))

    def test_detects_german_rate_limit_messages(self):
        future_time = (datetime.now() + chatgpt_attach.timedelta(hours=2)).strftime("%H:%M")
        future_date = datetime.now() + timedelta(days=2)
        samples = [
            f"Sie haben Ihr Limit für GPT-4o erreicht. Versuchen Sie es nach {future_time} Uhr erneut.",
            "Nutzungslimit erreicht. Wieder verfügbar in 20 Minuten.",
            f"Limit erreicht. Versuche es nach {future_time} erneut.",
            "Zu viele Anfragen. Bitte versuchen Sie es in 10 Minuten erneut.",
            f"Sie haben Ihr Nutzungslimit erreicht. Versuchen Sie es am {future_date.day}. {future_date.strftime('%B')} {future_date.year} um 20:18 Uhr erneut.",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertTrue(chatgpt_attach.is_rate_limited(sample))

    def test_non_limit_messages_return_false(self):
        normal_text = "Here is the code you requested:\n```python\nprint('Hello world')\n```"
        self.assertFalse(chatgpt_attach.is_rate_limited(normal_text))


class ChatGPTResetTimeParsingTests(unittest.TestCase):
    def test_relative_time_parsing_minutes(self):
        text = "Rate limit reached. Try again in 45 minutes."
        wait_sec = chatgpt_attach.get_wait_seconds(text, margin_seconds=10)
        self.assertEqual(wait_sec, 45 * 60 + 10)

    def test_relative_time_parsing_hours_german(self):
        text = "Nutzungslimit erreicht. Wieder verfügbar in 2 Stunden."
        wait_sec = chatgpt_attach.get_wait_seconds(text, margin_seconds=5)
        self.assertEqual(wait_sec, 2 * 3600 + 5)

    def test_relative_time_parsing_seconds(self):
        text = "Too many requests. Resets in 30 seconds."
        wait_sec = chatgpt_attach.get_wait_seconds(text, margin_seconds=0)
        self.assertEqual(wait_sec, 30)

    def test_fallback_when_time_cannot_be_parsed(self):
        text = "You've reached your usage limit. Exceeded quota."
        wait_sec = chatgpt_attach.get_wait_seconds(text, margin_seconds=60, fallback_seconds=1800)
        self.assertEqual(wait_sec, 1800 + 60)

    def test_stale_history_limits_are_ignored(self):
        now = datetime.now()
        if now.hour > 0 or now.minute > 5:
            past_dt = now - chatgpt_attach.timedelta(minutes=5)
            past_time = past_dt.strftime("%H:%M")
            old_limit_text = f"Sie haben Ihr Limit erreicht. Versuchen Sie es nach {past_time} Uhr erneut."
            self.assertIsNone(chatgpt_attach.extract_active_rate_limit(old_limit_text))


class ChatGPTCOMStructureTests(unittest.TestCase):
    def test_variant_alignment_and_size(self):
        # 64-bit VARIANT struct size in Windows x64 is 24 bytes
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            self.assertEqual(ctypes.sizeof(chatgpt_attach.VARIANT), 24)

    def test_guid_structure(self):
        guid = chatgpt_attach.CLSID_CUIAutomation
        self.assertEqual(guid.Data1, 0xff48dba4)
        self.assertEqual(guid.Data2, 0x60ef)
        self.assertEqual(guid.Data3, 0x4201)

    def test_uia_wrapper_initialization(self):
        try:
            uia = chatgpt_attach.UIAWrapper()
            self.assertIsNotNone(uia.p_uia)
        except Exception as e:
            self.fail(f"UIAWrapper failed to initialize: {e}")


    def test_continuation_buttons_filtering(self):
        valid_buttons = ["Continue generating", "Generierung fortsetzen", "Resume goal", "Ziel fortsetzen"]
        for btn in valid_buttons:
            self.assertTrue(chatgpt_attach.is_continuation_button(btn), f"Should accept {btn}")

        # Normal assistant response buttons and generic dialog buttons must NEVER trigger!
        invalid_buttons = [
            "Regenerate response", "Antwort neu generieren",
            "Try again", "Erneut versuchen",
            "Continue", "Fortfahren", "fortsetzen",
            "Continue with Google", "Continue with Apple", "Sign in", "Delete chat"
        ]
        for btn in invalid_buttons:
            self.assertFalse(chatgpt_attach.is_continuation_button(btn), f"Should reject {btn}")

    def test_rate_limit_tracker_deduplication(self):
        tracker = chatgpt_attach.RateLimitTracker(cooldown_seconds=1)
        fp = "test_limit_fingerprint_123"
        self.assertFalse(tracker.is_already_handled(fp))
        self.assertTrue(tracker.can_act())

        tracker.mark_handled(fp)
        self.assertTrue(tracker.is_already_handled(fp))
        self.assertFalse(tracker.can_act())  # In cooldown

    def test_resumed_limits_in_previous_turns_are_ignored(self):
        # A limit followed by "keep going" and subsequent assistant text is already resolved
        text = "Sie haben Ihr Limit für GPT-4o erreicht. Wieder verfügbar in 10 Minuten.\nkeep going\nGerne! Hier ist der Code..."
        self.assertIsNone(chatgpt_attach.extract_active_rate_limit(text, resume_text="keep going"))

    def test_relative_limit_fingerprint_is_stable_over_time(self):
        text = "Nutzungslimit erreicht. Wieder verfügbar in 10 Minuten."
        res1 = chatgpt_attach.extract_active_rate_limit(text)
        self.assertIsNotNone(res1)
        res2 = chatgpt_attach.extract_active_rate_limit(text)
        self.assertIsNotNone(res2)
        # Content hash MUST be identical regardless of when it is called
        self.assertEqual(res1[2], res2[2])

    def test_normal_chat_words_do_not_trigger_fallback(self):
        # Normal conversation containing "später" or "erreicht" must NEVER trigger fallback!
        normal_conversation = "Ich habe mein Ziel erreicht. Können wir später weitermachen?"
        self.assertIsNone(chatgpt_attach.extract_active_rate_limit(normal_conversation))

    def test_multiday_quota_limit_is_capped_to_5_hours(self):
        # When a multi-day limit (e.g. Sep 3 = 5 days away) is encountered,
        # KeepGoing must NOT sleep for 5 days, but cap the wait to max 5 hours (18000s + margin)
        future_date = datetime.now() + timedelta(days=2)
        msg = f"You've hit your usage limit. Upgrade your plan or add credits to continue, or try again at {future_date.strftime('%b')} {future_date.day}, {future_date.year}, 8:18 PM."
        res = chatgpt_attach.extract_active_rate_limit(msg, margin_seconds=60)
        self.assertIsNotNone(res)
        target_dt, wait_sec, fp = res
        # Wait time must be capped to 5 hours + 60s margin = 18060s
        self.assertEqual(wait_sec, 5 * 3600 + 60)

    def test_detects_usage_alert_banner(self):
        future_date = datetime.now() + timedelta(days=2)
        banner_msg = f"Resets every week · Next reset is on {future_date.strftime('%b')} {future_date.day}, {future_date.year} at 8:18 PM\nDismiss usage alert\nUsage consumed"
        res = chatgpt_attach.extract_active_rate_limit(banner_msg, margin_seconds=60)
        self.assertIsNotNone(res)
        target_dt, wait_sec, fp = res
        self.assertEqual(wait_sec, 5 * 3600 + 60)


class ChatGPTClipboardTests(unittest.TestCase):
    def test_clipboard_read_and_write(self):
        original = chatgpt_attach.get_clipboard_text()
        test_payload = "KeepGoing Test Clipboard Text 12345"
        try:
            write_success = chatgpt_attach.set_clipboard_text(test_payload)
            self.assertTrue(write_success)
            read_back = chatgpt_attach.get_clipboard_text()
            self.assertEqual(read_back, test_payload)
        finally:
            chatgpt_attach.set_clipboard_text(original)


class ChatGPTProfileMenuParsingTests(unittest.TestCase):
    def test_parses_live_quota_tokens(self):
        sample_names = [
            "Open profile menu", "Kraten", "Usage remaining",
            "5h", "70%", "19:39",
            "Weekly", "17%", "3. Sept.",
            "Upgrade to Pro", "Settings Ctrl+,"
        ]
        fixed_now = datetime(2026, 8, 29, 15, 30, 0)
        res = chatgpt_attach.parse_profile_menu_tokens(sample_names, now=fixed_now)
        self.assertIsNotNone(res)
        self.assertEqual(res["5h_pct"], 70)
        self.assertEqual(res["5h_reset_str"], "19:39")
        self.assertFalse(res["is_5h_exhausted"])
        self.assertEqual(res["weekly_pct"], 17)
        self.assertEqual(res["weekly_reset_str"], "3. Sept.")
        self.assertEqual(res["5h_wait_sec"], (19 - 15) * 3600 + (39 - 30) * 60)

    def test_detects_exhausted_0_percent_quota(self):
        sample_names = [
            "Usage remaining",
            "5h", "0%", "19:39",
            "Weekly", "0%", "3. Sept."
        ]
        fixed_now = datetime(2026, 8, 29, 15, 30, 0)
        res = chatgpt_attach.parse_profile_menu_tokens(sample_names, now=fixed_now)
        self.assertIsNotNone(res)
        self.assertEqual(res["5h_pct"], 0)
        self.assertTrue(res["is_5h_exhausted"])
        self.assertEqual(res["5h_reset_str"], "19:39")


if __name__ == "__main__":
    unittest.main()
