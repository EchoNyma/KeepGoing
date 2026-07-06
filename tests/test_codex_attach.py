import ctypes
import os
import sys
import unittest


PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import codex_attach


class CodexRateLimitDetectionTests(unittest.TestCase):
    def test_detects_startup_limit_even_when_greeting_pushes_it_out_of_last_six_lines(self):
        screen = "\n".join(
            [
                "You've hit your usage limit.",
                "Please try again at 2:57 PM.",
                "",
                "Welcome to Codex",
                "Use /help for commands.",
                "Use /skills to enable skills.",
                "Model: GPT-5",
                "Approval: never",
                "Sandbox: danger-full-access",
                ">",
            ]
        )

        self.assertTrue(codex_attach.is_rate_limited(screen))

    def test_detects_limit_when_startup_text_mentions_working_directory(self):
        screen = "\n".join(
            [
                "You've hit your usage limit.",
                "Please try again at 2:57 PM.",
                "",
                "Working directory: C:\\Users\\Marcu",
                "Use /skills to enable skills.",
                ">",
            ]
        )

        self.assertTrue(codex_attach.is_rate_limited(screen))


class CodexFocusEventTests(unittest.TestCase):
    def test_send_focus_event_writes_real_focus_event_record(self):
        self.assertTrue(hasattr(codex_attach.EVENT_UNION, "FocusEvent"))

        calls = []

        class FakeKernel32:
            def WriteConsoleInputW(self, _handle, record_ptr, count, written_ptr):
                record = ctypes.cast(
                    record_ptr, ctypes.POINTER(codex_attach.INPUT_RECORD)
                ).contents
                calls.append((record.EventType, bool(record.Event.FocusEvent.bSetFocus), count))
                ctypes.cast(written_ptr, ctypes.POINTER(ctypes.wintypes.DWORD)).contents.value = count
                return True

        original_kernel32 = codex_attach.kernel32
        try:
            codex_attach.kernel32 = FakeKernel32()
            codex_attach.send_focus_event(123)
        finally:
            codex_attach.kernel32 = original_kernel32

        self.assertEqual(calls, [(codex_attach.FOCUS_EVENT, True, 1)])


if __name__ == "__main__":
    unittest.main()
