import ctypes
from ctypes import wintypes
import re
import sys
import os
import time
import json
import argparse
import hashlib
from datetime import datetime, timedelta

# =====================================================================
# Windows API & COM Configuration (Zero-Dependency Pure ctypes)
# =====================================================================

kernel32 = ctypes.windll.kernel32
user32 = ctypes.windll.user32
ole32 = ctypes.windll.ole32
oleaut32 = ctypes.windll.oleaut32
dwmapi = ctypes.windll.dwmapi

HRESULT = ctypes.c_long

# COM Constants
CLSCTX_INPROC_SERVER = 0x1
COINIT_MULTITHREADED = 0x0
TreeScope_Element = 0x1
TreeScope_Children = 0x2
TreeScope_Descendants = 0x4
TreeScope_Subtree = 0x7

# UIA Identifiers
UIA_InvokePatternId = 10000
UIA_ValuePatternId = 10002
UIA_ButtonControlTypeId = 50000
UIA_EditControlTypeId = 50004
UIA_TextControlTypeId = 50020
UIA_CustomControlTypeId = 50025
UIA_GroupControlTypeId = 50026
UIA_DocumentControlTypeId = 50030

UIA_NamePropertyId = 30005
UIA_ControlTypePropertyId = 30003
UIA_IsEnabledPropertyId = 30010
UIA_ValueValuePropertyId = 30045

VT_EMPTY = 0
VT_BSTR = 8
VT_BOOL = 11

# Windows Messages & Constants
WM_GETOBJECT = 0x003D
OBJID_CLIENT = 0xFFFFFFFC
DWMWA_CLOAKED = 14
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
SW_RESTORE = 9
SW_MINIMIZE = 6
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

# Keyboard Input Constants
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_CONTROL = 0x11
VK_RETURN = 0x0D
VK_V = 0x56
VK_MENU = 0x12

# GUID Structure
class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __init__(self, l=0, w1=0, w2=0, b1=0, b2=0, b3=0, b4=0, b5=0, b6=0, b7=0, b8=0):
        super().__init__()
        self.Data1 = l
        self.Data2 = w1
        self.Data3 = w2
        self.Data4 = (ctypes.c_ubyte * 8)(b1, b2, b3, b4, b5, b6, b7, b8)

# Verified UIA GUIDs
CLSID_CUIAutomation = GUID(0xff48dba4, 0x60ef, 0x4201, 0xaa, 0x87, 0x54, 0x10, 0x3e, 0xef, 0x59, 0x4e)
IID_IUIAutomation = GUID(0x30cbe57d, 0xd9d0, 0x452a, 0xab, 0x13, 0x7a, 0xc5, 0xac, 0x48, 0x25, 0xee)
IID_IUIAutomationElement = GUID(0xd22108aa, 0x8ac5, 0x49a5, 0x83, 0x7b, 0x37, 0xbb, 0xb3, 0xd7, 0x59, 0x1e)
IID_IUIAutomationInvokePattern = GUID(0xfb377fbe, 0x8ea6, 0x4671, 0x9e, 0x85, 0x64, 0xe7, 0x3e, 0x85, 0x9b, 0xe9)

# 64-Bit VARIANT Structure (24 Bytes alignment)
class VARIANT_UNION(ctypes.Union):
    _fields_ = [
        ("lVal", wintypes.LONG),
        ("bstrVal", wintypes.LPWSTR),
        ("pdispVal", ctypes.c_void_p),
        ("punkVal", ctypes.c_void_p),
        ("dblVal", ctypes.c_double),
        ("llVal", ctypes.c_int64),
    ]

class VARIANT(ctypes.Structure):
    _fields_ = [
        ("vt", wintypes.WORD),
        ("wReserved1", wintypes.WORD),
        ("wReserved2", wintypes.WORD),
        ("wReserved3", wintypes.WORD),
        ("union", VARIANT_UNION),
        ("decVal_padding", ctypes.c_int64)
    ]

# Win32 SendInput Structures
class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))
    ]

class INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("ki", KEYBDINPUT),
        ("padding", ctypes.c_byte * 32)
    ]

class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("union", INPUT_UNION)
    ]

# Win32 API Function Signatures
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE

kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetExitCodeProcess.restype = wintypes.BOOL

kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE

kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL

kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p

kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.restype = wintypes.BOOL

user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL

user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD

user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int

user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int

user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL

user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL

user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL

user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL

user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SendMessageW.restype = ctypes.c_longlong

user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL

user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = wintypes.BOOL

user32.EmptyClipboard.argtypes = []
user32.EmptyClipboard.restype = wintypes.BOOL

user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE

user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE

user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT

user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_ulong]
user32.keybd_event.restype = None

user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND

user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.AttachThreadInput.restype = wintypes.BOOL

ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
ole32.CoInitializeEx.restype = HRESULT

ole32.CoCreateInstance.argtypes = [
    ctypes.POINTER(GUID),
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(GUID),
    ctypes.POINTER(ctypes.c_void_p)
]
ole32.CoCreateInstance.restype = HRESULT

oleaut32.SysAllocString.argtypes = [wintypes.LPCWSTR]
oleaut32.SysAllocString.restype = wintypes.LPWSTR

oleaut32.SysFreeString.argtypes = [wintypes.LPWSTR]
oleaut32.SysFreeString.restype = None

oleaut32.VariantClear.argtypes = [ctypes.POINTER(VARIANT)]
oleaut32.VariantClear.restype = HRESULT


# =====================================================================
# Low-Level COM Helper Functions
# =====================================================================

def com_call(p_interface, slot, restype, argtypes, *args):
    """Executes a virtual method call on a COM interface pointer using its VTable slot."""
    if not p_interface:
        return None
    try:
        vtable_ptr = ctypes.cast(p_interface, ctypes.POINTER(ctypes.c_void_p))[0]
        func_ptr_array = ctypes.cast(vtable_ptr, ctypes.POINTER(ctypes.c_void_p))
        func_ptr = func_ptr_array[slot]
        if not func_ptr:
            return None
        prototype = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
        func = prototype(func_ptr)
        return func(p_interface, *args)
    except Exception:
        return None

def com_release(p_interface):
    """Releases a COM interface pointer (IUnknown::Release, Slot 2)."""
    if p_interface:
        try:
            com_call(p_interface, 2, wintypes.ULONG, [])
        except Exception:
            pass

class UIAWrapper:
    """Manages the lifecycle and calls to the Windows IUIAutomation COM subsystem."""
    def __init__(self):
        ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
        self.p_uia = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(
            ctypes.byref(CLSID_CUIAutomation),
            None,
            CLSCTX_INPROC_SERVER,
            ctypes.byref(IID_IUIAutomation),
            ctypes.byref(self.p_uia)
        )
        if hr != 0 or not self.p_uia:
            raise RuntimeError(f"Failed to create IUIAutomation COM instance (HRESULT: 0x{hr & 0xFFFFFFFF:08X})")

    def __del__(self):
        if self.p_uia:
            com_release(self.p_uia)
            self.p_uia = None

    def element_from_handle(self, hwnd):
        """IUIAutomation::ElementFromHandle (Slot 6)."""
        p_elem = ctypes.c_void_p()
        hr = com_call(self.p_uia, 6, HRESULT, [wintypes.HWND, ctypes.POINTER(ctypes.c_void_p)], hwnd, ctypes.byref(p_elem))
        if hr == 0 and p_elem.value:
            return p_elem
        return None

    def create_true_condition(self):
        """IUIAutomation::CreateTrueCondition (Slot 21)."""
        p_cond = ctypes.c_void_p()
        hr = com_call(self.p_uia, 21, HRESULT, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(p_cond))
        if hr == 0 and p_cond.value:
            return p_cond
        return None

    def get_element_name(self, p_elem):
        """IUIAutomationElement::get_CurrentName (Slot 23)."""
        if not p_elem:
            return ""
        bstr = wintypes.LPWSTR()
        hr = com_call(p_elem, 23, HRESULT, [ctypes.POINTER(wintypes.LPWSTR)], ctypes.byref(bstr))
        if hr == 0 and bstr.value:
            name = str(bstr.value)
            oleaut32.SysFreeString(bstr)
            return name
        return ""

    def get_element_control_type(self, p_elem):
        """IUIAutomationElement::get_CurrentControlType (Slot 21)."""
        if not p_elem:
            return 0
        ctype = wintypes.LONG()
        hr = com_call(p_elem, 21, HRESULT, [ctypes.POINTER(wintypes.LONG)], ctypes.byref(ctype))
        if hr == 0:
            return ctype.value
        return 0

    def get_element_value(self, p_elem):
        """IUIAutomationElement::GetCurrentPropertyValue (Slot 10) for UIA_ValueValuePropertyId (30045)."""
        if not p_elem:
            return ""
        var = VARIANT()
        hr = com_call(p_elem, 10, HRESULT, [wintypes.DWORD, ctypes.POINTER(VARIANT)], UIA_ValueValuePropertyId, ctypes.byref(var))
        if hr == 0 and var.vt == VT_BSTR and var.union.bstrVal:
            val = str(var.union.bstrVal)
            oleaut32.VariantClear(ctypes.byref(var))
            return val
        return ""

    def find_all(self, p_root, scope, p_cond):
        """IUIAutomationElement::FindAll (Slot 6). Returns a Python list of COM elements."""
        if not p_root or not p_cond:
            return []
        p_array = ctypes.c_void_p()
        hr = com_call(p_root, 6, HRESULT, [wintypes.LONG, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)], scope, p_cond, ctypes.byref(p_array))
        if hr != 0 or not p_array.value:
            return []

        # IUIAutomationElementArray::get_Length (Slot 3)
        length = ctypes.c_int()
        com_call(p_array, 3, HRESULT, [ctypes.POINTER(ctypes.c_int)], ctypes.byref(length))

        elements = []
        # IUIAutomationElementArray::GetElement (Slot 4)
        for i in range(length.value):
            p_elem = ctypes.c_void_p()
            if com_call(p_array, 4, HRESULT, [ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)], i, ctypes.byref(p_elem)) == 0:
                if p_elem.value:
                    elements.append(p_elem)

        com_release(p_array)
        return elements

    def get_invoke_pattern(self, p_elem):
        """IUIAutomationElement::GetCurrentPattern (Slot 16) for InvokePattern (10000)."""
        if not p_elem:
            return None
        p_pattern = ctypes.c_void_p()
        hr = com_call(p_elem, 16, HRESULT, [wintypes.LONG, ctypes.POINTER(ctypes.c_void_p)], UIA_InvokePatternId, ctypes.byref(p_pattern))
        if hr == 0 and p_pattern.value:
            return p_pattern
        return None

    def invoke_element(self, p_elem):
        """Invokes an element's action (IUIAutomationInvokePattern::Invoke, Slot 3)."""
        p_pattern = self.get_invoke_pattern(p_elem)
        if not p_pattern:
            return False
        try:
            hr = com_call(p_pattern, 3, HRESULT, [])
            return hr == 0
        finally:
            com_release(p_pattern)

    def set_element_focus(self, p_elem):
        """IUIAutomationElement::SetFocus (Slot 3)."""
        if not p_elem:
            return False
        hr = com_call(p_elem, 3, HRESULT, [])
        return hr == 0


# =====================================================================
# Rate Limit Detection & Reset Time Parsing (Multilingual DE + EN)
# =====================================================================

LIMIT_ANCHORS = [
    re.compile(r'(?:hit|exceeded|reached).*(?:your|the)\s*(?:\d+-hour\s+)?limit', re.I),
    re.compile(r'limit.*erreicht', re.I),
    re.compile(r'nutzungslimit', re.I),
    re.compile(r'usage limit', re.I),
    re.compile(r'rate limit', re.I),
    re.compile(r'too many requests', re.I),
    re.compile(r'zu viele anfragen', re.I),
    re.compile(r'dismiss usage alert', re.I),
    re.compile(r'usage consumed', re.I),
    re.compile(r'%\s*usage remaining', re.I),
    re.compile(r'resets every week', re.I),
    re.compile(r'goal usage limited', re.I),
]

RELATIVE_RESET_PATTERN = re.compile(
    r'(?:try again|resets?|wieder verf\xFCgbar|versuche?n?\s*(?:sie\s+)?es|available).*?(?:in|nach|f\xFCr|resets?\s+in)[:\s]\s*(\d+)\s*(hours?|minutes?|mins?|stunden?|std|minuten?|min|s|sekunden?|seconds?|h|m)\b',
    re.I
)

MONTH_MAP = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'sept': 9, 'oct': 10, 'nov': 11, 'dec': 12,
    'january': 1, 'february': 2, 'march': 3, 'april': 4, 'june': 6,
    'july': 7, 'august': 8, 'september': 9, 'october': 10, 'november': 11, 'december': 12,
    'jän': 1, 'mär': 3, 'mai': 5, 'okt': 10, 'dez': 12
}

DATE_RESET_PATTERN = re.compile(
    r'(?:try again|resets?|next reset|wieder verf\xFCgbar|versuche?n?\s*(?:sie\s+)?es).*?(?:at|on|am)\s+'
    r'(?:'
    r'([A-Za-z]{3,9})\s+(\d{1,2})(?:[,\s]+(\d{4}))?'
    r'|'
    r'(\d{1,2})\.\s*([A-Za-z\xFC\xE4\xF6\xDF]{3,9}\.?)(?:[,\s]+(\d{4}))?'
    r')'
    r'[,\s]+(?:at|um)?\s*(\d{1,2}):(\d{2})\s*(am|pm|uhr)?',
    re.I
)

ABSOLUTE_RESET_PATTERN = re.compile(
    r'(?:try again|resets?|wieder verf\xFCgbar|versuche?n?\s*(?:sie\s+)?es|available).*?(?:after|at|nach|um|resets?\s+at)[:\s]\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm|uhr)?',
    re.I
)

# High-confidence system error fallback patterns ONLY (no loose chat words like 'später' or 'erreicht')
FALLBACK_RESET_PATTERN = re.compile(
    r'(?:you(?:\'ve| have) reached your (?:current )?usage limit|usage limit exceeded|sie haben ihr nutzungslimit erreicht|too many requests in \d+ (?:hour|minute)|you have hit your \d+-hour limit|try again later|sp\xE4ter erneut versuchen)',
    re.I
)

# Strict whitelist for true token continuation buttons (NO "Regenerate" / "Try again"!)
CONTINUATION_BUTTON_EXACT = {
    "continue generating",
    "generierung fortsetzen",
    "resume goal",
    "ziel fortsetzen",
}

BUTTON_IGNORE_SUBSTRINGS = (
    "google", "apple", "microsoft", "email", "account", "workspace",
    "login", "sign in", "regenerate", "erneut", "neu generieren", "delete", "cancel", "löschen", "abbrechen"
)

def is_continuation_button(name):
    """Verifies that an element is a true continuation button and not an assistant action button."""
    if not name:
        return False
    norm = name.strip().lower()
    if any(ignore in norm for ignore in BUTTON_IGNORE_SUBSTRINGS):
        return False
    return norm in CONTINUATION_BUTTON_EXACT or norm.startswith("continue generating") or norm.startswith("generierung fortsetzen")

def make_fingerprint(text_block):
    """Computes a deterministic, content-based hash of the limit message block."""
    normalized = " ".join(text_block.strip().lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]

def extract_active_rate_limit(text, margin_seconds=60, fallback_seconds=3600, resume_text="keep going"):
    """
    Scans text for an active rate limit message with strict validation:
    1. Evaluates latest turns first (bottom-up).
    2. Ignores limits that were already resumed by user resume_text in subsequent turns.
    3. Proximity check (<= 3 lines between anchor and reset time).
    4. Rejects stale historical timestamps.
    5. Returns a deterministic, stable content fingerprint.
    """
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    if not lines:
        return None

    now = datetime.now()

    # Search backwards from the bottom so we inspect the latest turn first
    for i in reversed(range(len(lines))):
        line = lines[i]
        if any(anchor.search(line) for anchor in LIMIT_ANCHORS):
            # Check if this anchor was already followed by subsequent resume messages
            subsequent_text = " ".join(lines[i + 1:]).lower()
            if resume_text.lower() in subsequent_text:
                # Already resumed in a previous turn
                continue

            # Proximity window: line itself and up to 3 lines around it
            start = max(0, i - 2)
            end = min(len(lines), i + 4)
            block = " ".join(lines[start:end])

            # Deterministic fingerprint based purely on message text
            fingerprint = make_fingerprint(block)

            # 1. Relative reset pattern ("in 45 minutes", "in 10 Minuten")
            rel_match = RELATIVE_RESET_PATTERN.search(block)
            if rel_match:
                amount = int(rel_match.group(1))
                unit = rel_match.group(2).lower()
                if unit.startswith(('h', 'std', 'stund')):
                    secs = amount * 3600
                elif unit.startswith('m'):
                    secs = amount * 60
                else:
                    secs = amount

                target_time = now + timedelta(seconds=secs)
                wait_sec = secs + margin_seconds
                return target_time, wait_sec, fingerprint

            # 2. Date-Time reset pattern (e.g. "try again at Sep 3, 2026, 8:18 PM")
            date_m = DATE_RESET_PATTERN.search(block)
            if date_m:
                g = date_m.groups()
                if g[0]:
                    m_str, d_str, y_str = g[0].lower(), g[1], g[2]
                else:
                    d_str, m_str, y_str = g[3], g[4].lower(), g[5]

                hour = int(g[6])
                minute = int(g[7]) if g[7] else 0
                ampm = g[8].lower() if g[8] else None

                if ampm == 'pm' and hour < 12:
                    hour += 12
                elif ampm == 'am' and hour == 12:
                    hour = 0

                month_num = MONTH_MAP.get(m_str[:3], now.month)
                day_num = int(d_str) if d_str else now.day
                year_num = int(y_str) if y_str else now.year

                try:
                    target_dt = datetime(year_num, month_num, day_num, hour, minute, 0)
                except Exception:
                    target_dt = now + timedelta(hours=5)

                diff = (target_dt - now).total_seconds()
                if diff <= 0:
                    if (now - target_dt).total_seconds() <= 120:
                        wait_sec = margin_seconds
                        return target_dt, wait_sec, fingerprint
                    else:
                        continue

                # If date is > 5 hours away (e.g. multi-day weekly limit), cap wait to 5-hour rolling model limit
                if diff > 5 * 3600:
                    wait_sec = 5 * 3600 + margin_seconds
                    target_dt = now + timedelta(seconds=wait_sec)
                    return target_dt, wait_sec, fingerprint

                wait_sec = int(diff) + margin_seconds
                return target_dt, wait_sec, fingerprint

            # 3. Absolute reset pattern ("after 3:15 PM", "nach 15:30 Uhr")
            abs_match = ABSOLUTE_RESET_PATTERN.search(block)
            if abs_match:
                hour = int(abs_match.group(1))
                minute = int(abs_match.group(2)) if abs_match.group(2) else 0
                ampm = abs_match.group(3).lower() if abs_match.group(3) else None

                if ampm == 'pm' and hour < 12:
                    hour += 12
                elif ampm == 'am' and hour == 12:
                    hour = 0

                target_today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

                if target_today > now:
                    diff = (target_today - now).total_seconds()
                    wait_sec = int(diff) + margin_seconds
                    target_time = target_today
                else:
                    if (now - target_today).total_seconds() > 18 * 3600:
                        target_time = target_today + timedelta(days=1)
                        diff = (target_time - now).total_seconds()
                        wait_sec = int(diff) + margin_seconds
                    elif (now - target_today).total_seconds() <= 120:
                        target_time = target_today
                        wait_sec = margin_seconds
                    else:
                        # Expired more than 2 minutes ago -> STALE HISTORY!
                        continue

                return target_time, wait_sec, fingerprint

            # 3. High-confidence Fallback reset pattern (only explicit system limit phrases)
            if FALLBACK_RESET_PATTERN.search(block):
                target_time = now + timedelta(seconds=fallback_seconds)
                wait_sec = fallback_seconds + margin_seconds
                return target_time, wait_sec, fingerprint

    return None

def is_rate_limited(text, resume_text="keep going"):
    """Backward-compatible helper for rate-limit presence."""
    return extract_active_rate_limit(text, resume_text=resume_text) is not None

def get_wait_seconds(text, margin_seconds=60, fallback_seconds=3600, resume_text="keep going"):
    """Backward-compatible helper returning wait seconds."""
    res = extract_active_rate_limit(text, margin_seconds, fallback_seconds, resume_text=resume_text)
    if res:
        return res[1]
    return fallback_seconds + margin_seconds


# =====================================================================
# Window Management & Process Discovery
# =====================================================================

def is_window_cloaked(hwnd):
    """Checks if the window is cloaked/suspended by Windows Desktop Window Manager."""
    cloaked = wintypes.DWORD()
    hr = dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
    if hr == 0 and cloaked.value != 0:
        return True
    return False

def find_chatgpt_window(target_pid=None):
    """Locates the top-level main window handle for the ChatGPT Desktop application."""
    candidates = []

    def enum_windows_callback(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
            return True

        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

        if target_pid and pid.value != target_pid:
            return True

        title_buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title_buf, 512)
        title = title_buf.value

        class_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buf, 256)
        class_name = class_buf.value

        # Open process to check executable name
        h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        is_chatgpt = False
        if h_proc:
            name_buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if hasattr(kernel32, 'QueryFullProcessImageNameW'):
                if kernel32.QueryFullProcessImageNameW(h_proc, 0, name_buf, ctypes.byref(size)):
                    if 'chatgpt.exe' in name_buf.value.lower():
                        is_chatgpt = True
            kernel32.CloseHandle(h_proc)

        if is_chatgpt or "chatgpt" in title.lower() or "chatgpt" in class_name.lower():
            # Check if valid top-level window
            if class_name in ["Chrome_WidgetWin_1", "ApplicationFrameWindow"] or "chatgpt" in title.lower():
                candidates.append((hwnd, pid.value, title, class_name))
        return True

    ENUM_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(ENUM_PROC(enum_windows_callback), 0)

    if candidates:
        # Prefer non-cloaked / active windows
        for hwnd, pid, title, class_name in candidates:
            if not is_window_cloaked(hwnd):
                return hwnd, pid
        return candidates[0][0], candidates[0][1]
    return None, None


# =====================================================================
# Safe Clipboard & Input Injection Engine
# =====================================================================

def get_clipboard_text():
    """Reads current Unicode text from Windows clipboard safely."""
    for _ in range(5):
        if user32.OpenClipboard(0):
            try:
                h_data = user32.GetClipboardData(CF_UNICODETEXT)
                if h_data:
                    p_text = kernel32.GlobalLock(h_data)
                    if p_text:
                        text = ctypes.wstring_at(p_text)
                        kernel32.GlobalUnlock(h_data)
                        return text
                return ""
            finally:
                user32.CloseClipboard()
        time.sleep(0.05)
    return ""

def set_clipboard_text(text):
    """Sets Unicode text into Windows clipboard safely."""
    unicode_bytes = (text + "\0").encode('utf-16-le')
    h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(unicode_bytes))
    if not h_mem:
        return False

    p_mem = kernel32.GlobalLock(h_mem)
    if not p_mem:
        return False

    ctypes.memmove(p_mem, unicode_bytes, len(unicode_bytes))
    kernel32.GlobalUnlock(h_mem)

    for _ in range(5):
        if user32.OpenClipboard(0):
            try:
                user32.EmptyClipboard()
                user32.SetClipboardData(CF_UNICODETEXT, h_mem)
                return True
            finally:
                user32.CloseClipboard()
        time.sleep(0.05)
    return False

def activate_and_focus_window(hwnd):
    """Bypasses Windows ASFW lock to reliably bring target window to the foreground."""
    foreground_hwnd = user32.GetForegroundWindow()
    if foreground_hwnd == hwnd:
        return True

    fg_thread = user32.GetWindowThreadProcessId(foreground_hwnd, None)
    app_thread = kernel32.GetCurrentThreadId()

    if fg_thread != app_thread:
        user32.AttachThreadInput(fg_thread, app_thread, True)

    # Pulse Alt key to bypass Windows focus lock policies
    user32.keybd_event(VK_MENU, 0, 0, 0)
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
    user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)

    if fg_thread != app_thread:
        user32.AttachThreadInput(fg_thread, app_thread, False)

    time.sleep(0.2)
    return user32.GetForegroundWindow() == hwnd

def send_paste_and_enter():
    """Simulates physical Ctrl+V followed by Enter key presses using SendInput."""
    inputs = (INPUT * 6)()

    # 1. Ctrl Down
    inputs[0].type = INPUT_KEYBOARD
    inputs[0].union.ki.wVk = VK_CONTROL
    inputs[0].union.ki.dwFlags = 0

    # 2. V Down
    inputs[1].type = INPUT_KEYBOARD
    inputs[1].union.ki.wVk = VK_V
    inputs[1].union.ki.dwFlags = 0

    # 3. V Up
    inputs[2].type = INPUT_KEYBOARD
    inputs[2].union.ki.wVk = VK_V
    inputs[2].union.ki.dwFlags = KEYEVENTF_KEYUP

    # 4. Ctrl Up
    inputs[3].type = INPUT_KEYBOARD
    inputs[3].union.ki.wVk = VK_CONTROL
    inputs[3].union.ki.dwFlags = KEYEVENTF_KEYUP

    # 5. Enter Down
    inputs[4].type = INPUT_KEYBOARD
    inputs[4].union.ki.wVk = VK_RETURN
    inputs[4].union.ki.dwFlags = 0

    # 6. Enter Up
    inputs[5].type = INPUT_KEYBOARD
    inputs[5].union.ki.wVk = VK_RETURN
    inputs[5].union.ki.dwFlags = KEYEVENTF_KEYUP

    # Send Ctrl+V
    user32.SendInput(4, ctypes.byref(inputs[0]), ctypes.sizeof(INPUT))
    time.sleep(0.15)  # Pause to let React Controlled Input / Lexical process DOM change
    # Send Enter
    user32.SendInput(2, ctypes.byref(inputs[4]), ctypes.sizeof(INPUT))


# =====================================================================
# Main Scraper & Auto-Resume Worker
# =====================================================================

TAIL_ELEMENT_COUNT = 500

class RateLimitTracker:
    """Tracks handled limit fingerprints with expiration and enforces a cooldown to prevent duplicate triggers."""
    def __init__(self, cooldown_seconds=30):
        self.handled_fingerprints = {}  # fingerprint -> timestamp
        self.last_action_timestamp = 0
        self.cooldown_seconds = cooldown_seconds

    def can_act(self):
        return (time.time() - self.last_action_timestamp) >= self.cooldown_seconds

    def is_already_handled(self, fingerprint):
        if fingerprint not in self.handled_fingerprints:
            return False
        handled_time = self.handled_fingerprints[fingerprint]
        # Keep fingerprints active for 2 hours to prevent stale history from re-triggering
        if (time.time() - handled_time) < 7200:
            return True
        return False

    def mark_handled(self, fingerprint):
        self.handled_fingerprints[fingerprint] = time.time()
        self.last_action_timestamp = time.time()
        # Clean up entries older than 2 hours
        now = time.time()
        expired = [fp for fp, ts in self.handled_fingerprints.items() if now - ts >= 7200]
        for fp in expired:
            del self.handled_fingerprints[fp]


def scrape_chatgpt_text_and_buttons(uia, hwnd):
    """Scrapes visible text nodes from the bottom tail of the ChatGPT window to ignore old history."""
    # Send WM_GETOBJECT to force Chromium to wake its accessibility tree
    user32.SendMessageW(hwnd, WM_GETOBJECT, 0, OBJID_CLIENT)

    p_root = uia.element_from_handle(hwnd)
    if not p_root:
        return "", None, None

    p_cond = uia.create_true_condition()
    if not p_cond:
        com_release(p_root)
        return "", None, None

    try:
        elements = uia.find_all(p_root, TreeScope_Descendants, p_cond)
        if not elements:
            return "", None, None

        # Inspect only the tail elements (latest messages & prompt box)
        tail_elements = elements[-TAIL_ELEMENT_COUNT:] if len(elements) > TAIL_ELEMENT_COUNT else elements

        # Immediately release older elements to prevent memory leaks and ignore old history
        if len(elements) > TAIL_ELEMENT_COUNT:
            for p_elem in elements[:-TAIL_ELEMENT_COUNT]:
                com_release(p_elem)

        text_lines = []
        continuation_btn = None
        prompt_box = None
        candidates = []

        for p_elem in tail_elements:
            ctype = uia.get_element_control_type(p_elem)
            name = uia.get_element_name(p_elem).strip()

            if name:
                text_lines.append(name)

            # Check for true token continuation button ("Continue generating")
            if ctype == UIA_ButtonControlTypeId and not continuation_btn:
                if is_continuation_button(name):
                    continuation_btn = p_elem
                    continue

            # Check for prompt input box candidates
            if ctype in [UIA_EditControlTypeId, UIA_DocumentControlTypeId]:
                candidates.append((p_elem, ctype, name))
                continue

            com_release(p_elem)

        # Select the actual prompt box
        placeholders = ("message", "ask", "nachricht", "frage", "send a message", "work with chatgpt")
        # Priority 1: Exact prompt box name or explicit placeholder
        for p_elem, ctype, name in candidates:
            name_lower = name.lower()
            if any(ph in name_lower for ph in ("work with chatgpt", "message chatgpt", "nachricht an chatgpt", "ask anything")):
                prompt_box = p_elem
                break

        # Priority 2: Any Edit control containing general placeholder
        if not prompt_box:
            for p_elem, ctype, name in reversed(candidates):
                name_lower = name.lower()
                if ctype == UIA_EditControlTypeId and any(ph in name_lower for ph in placeholders):
                    prompt_box = p_elem
                    break

        for p_elem, ctype, name in candidates:
            if p_elem != prompt_box:
                com_release(p_elem)

        return "\n".join(text_lines), prompt_box, continuation_btn
    finally:
        com_release(p_cond)
        com_release(p_root)


def perform_resume_action(uia, hwnd, prompt_box, action_button, resume_text="keep going", log_file=None):
    """Executes the two-stage resume action: Button click if available, else safe paste."""
    # Stage 1: Direct button invoke or center physical click (for custom div buttons)
    if action_button:
        btn_name = uia.get_element_name(action_button).strip()
        if log_file:
            log_file.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Found action button ('{btn_name}'). Invoking...\n")
            log_file.flush()
        success = uia.invoke_element(action_button)
        if not success:
            # Fallback: physical click at button center (essential for buttons without InvokePattern like 'Resume goal')
            var = VARIANT()
            hr = com_call(action_button, 10, HRESULT, [wintypes.DWORD, ctypes.POINTER(VARIANT)], 30001, ctypes.byref(var))
            if hr == 0 and var.vt == 8197:
                p_sa = ctypes.c_void_p(var.union.punkVal)
                p_data = ctypes.c_void_p()
                oleaut32.SafeArrayAccessData(p_sa, ctypes.byref(p_data))
                doubles = ctypes.cast(p_data, ctypes.POINTER(ctypes.c_double))
                cx = int(doubles[0] + doubles[2] / 2)
                cy = int(doubles[1] + doubles[3] / 2)
                oleaut32.SafeArrayUnaccessData(p_sa)
                oleaut32.VariantClear(ctypes.byref(var))
                if log_file:
                    log_file.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] InvokePattern unavailable. Physically clicking button center at ({cx}, {cy})...\n")
                    log_file.flush()
                activate_and_focus_window(hwnd)
                time.sleep(0.1)
                user32.SetCursorPos(cx, cy)
                time.sleep(0.05)
                user32.mouse_event(0x0002, 0, 0, 0, 0)
                user32.mouse_event(0x0004, 0, 0, 0, 0)
                success = True

        com_release(action_button)
        if prompt_box:
            com_release(prompt_box)
        return success

    # Stage 2: Safe Clipboard Paste & Enter
    if log_file:
        log_file.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Executing auto-resume paste of '{resume_text}'...\n")
        log_file.flush()

    was_minimized = bool(user32.IsIconic(hwnd))
    activate_and_focus_window(hwnd)

    if prompt_box:
        uia.set_element_focus(prompt_box)
        com_release(prompt_box)

    # Safe clipboard swap
    original_clipboard = get_clipboard_text()
    try:
        set_clipboard_text(resume_text)
        time.sleep(0.05)
        # Select all in prompt box (Ctrl+A) to replace any stale draft
        user32.keybd_event(VK_CONTROL, 0, 0, 0)
        user32.keybd_event(0x41, 0, 0, 0)  # 'A'
        user32.keybd_event(0x41, 0, KEYEVENTF_KEYUP, 0)
        user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.05)
        send_paste_and_enter()
        time.sleep(0.15)
    finally:
        # Restore user's original clipboard
        set_clipboard_text(original_clipboard)

    if was_minimized:
        time.sleep(0.3)
        user32.ShowWindow(hwnd, SW_MINIMIZE)

    return True


def scrape_profile_usage_status(hwnd):
    """
    Directly inspects the live 5h and Weekly quota status from the ChatGPT profile menu.
    Returns a dict with percentage and reset timestamps, e.g.:
      {
         "5h_pct": 70,
         "5h_reset_str": "19:39",
         "5h_target_dt": datetime(...),
         "5h_wait_sec": 14400,
         "weekly_pct": 17,
         "weekly_reset_str": "3. Sept.",
         "is_5h_exhausted": False
      }
    """
    user32.SendMessageW(hwnd, WM_GETOBJECT, 0, OBJID_CLIENT)
    uia = UIAWrapper()
    p_root = uia.element_from_handle(hwnd)
    if not p_root:
        return None

    p_cond = uia.create_true_condition()
    if not p_cond:
        com_release(p_root)
        return None

    try:
        elements = uia.find_all(p_root, TreeScope_Descendants, p_cond)
        profile_elem = None
        for p in elements:
            if uia.get_element_name(p).strip() == "Open profile menu":
                profile_elem = p
                break

        if not profile_elem:
            for p in elements:
                com_release(p)
            return None

        # Determine center of profile menu button
        var = VARIANT()
        hr = com_call(profile_elem, 10, HRESULT, [wintypes.DWORD, ctypes.POINTER(VARIANT)], 30001, ctypes.byref(var))
        p_x, p_y = None, None
        if hr == 0 and var.vt == 8197:
            p_sa = ctypes.c_void_p(var.union.punkVal)
            p_data = ctypes.c_void_p()
            oleaut32.SafeArrayAccessData(p_sa, ctypes.byref(p_data))
            doubles = ctypes.cast(p_data, ctypes.POINTER(ctypes.c_double))
            l, t, w, h = doubles[0], doubles[1], doubles[2], doubles[3]
            oleaut32.SafeArrayUnaccessData(p_sa)
            oleaut32.VariantClear(ctypes.byref(var))
            p_x = int(l + w / 2)
            p_y = int(t + h / 2)

        for p in elements:
            com_release(p)

        if not p_x:
            rc = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rc))
            p_x = rc.left + 49
            p_y = rc.bottom - 18

        activate_and_focus_window(hwnd)
        time.sleep(0.15)

        # 1. Click Open profile menu
        user32.SetCursorPos(p_x, p_y)
        time.sleep(0.05)
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        user32.mouse_event(0x0004, 0, 0, 0, 0)
        time.sleep(0.6)

        # 2. Click Usage remaining (47px right, 125px up from profile button)
        u_x = p_x + 47
        u_y = p_y - 125
        user32.SetCursorPos(u_x, u_y)
        time.sleep(0.05)
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        user32.mouse_event(0x0004, 0, 0, 0, 0)
        time.sleep(0.6)

        user32.SendMessageW(hwnd, WM_GETOBJECT, 0, OBJID_CLIENT)
        fresh = uia.find_all(p_root, TreeScope_Descendants, p_cond)

        # 3. Always close menu with Escape
        user32.keybd_event(0x1B, 0, 0, 0)
        user32.keybd_event(0x1B, 0, 2, 0)

        names = []
        for p in fresh:
            n = uia.get_element_name(p).strip()
            if n:
                names.append(n)
            com_release(p)

        return parse_profile_menu_tokens(names)
    except Exception:
        return None
    finally:
        com_release(p_cond)
        com_release(p_root)


def parse_profile_menu_tokens(names, now=None):
    """Parses scraped profile menu element names into structured quota dictionary."""
    if now is None:
        now = datetime.now()
    status = {}

    for i in range(len(names) - 2):
        token = names[i].lower()
        if token in ('5h', '5-hour', '5 hour'):
            pct_str = names[i + 1]
            time_str = names[i + 2]
            pct_digits = "".join([c for c in pct_str if c.isdigit()])
            pct_val = int(pct_digits) if pct_digits else 0

            m = re.match(r'^(\d{1,2}):(\d{2})$', time_str)
            if m:
                hour, minute = int(m.group(1)), int(m.group(2))
                target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if target < now:
                    target += timedelta(days=1)
                diff = int((target - now).total_seconds())
                status["5h_pct"] = pct_val
                status["5h_reset_str"] = time_str
                status["5h_target_dt"] = target
                status["5h_wait_sec"] = diff
                status["is_5h_exhausted"] = (pct_val <= 0)
        elif token in ('weekly', 'wöchentlich', 'woche'):
            pct_str = names[i + 1]
            reset_str = names[i + 2]
            pct_digits = "".join([c for c in pct_str if c.isdigit()])
            pct_val = int(pct_digits) if pct_digits else 0
            status["weekly_pct"] = pct_val
            status["weekly_reset_str"] = reset_str
            status["is_weekly_exhausted"] = (pct_val <= 0)

    return status


def monitor_chatgpt_session(hwnd, pid, resume_text="keep going", margin_seconds=60, fallback_seconds=3600, log_path=None):
    """Continuous monitoring loop for the ChatGPT Desktop application with 0% Usage Ground Truth."""
    if not log_path:
        log_path = os.path.join(os.path.expanduser("~"), "chatgpt_attach_log.txt")

    log_file = open(log_path, "a", encoding="utf-8")
    log_file.write(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] KeepGoing ChatGPT Monitor attached to PID {pid} (HWND: 0x{hwnd:08X})\n")
    log_file.flush()

    h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h_proc:
        log_file.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Error: Unable to open process handle for PID {pid}.\n")
        log_file.close()
        return

    # Mutex protection against duplicate instances
    mutex_name = f"Global\\KeepGoing_ChatGPT_{pid}"
    h_mutex = kernel32.CreateMutexW(None, True, mutex_name)
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        print(f"[KeepGoing] Notice: Another KeepGoing instance is already monitoring PID {pid}. Exiting.")
        log_file.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] An instance is already monitoring PID {pid}. Exiting.\n")
        log_file.close()
        kernel32.CloseHandle(h_proc)
        return

    try:
        uia = UIAWrapper()
    except Exception as e:
        log_file.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] UIA Init Error: {e}\n")
        log_file.close()
        kernel32.CloseHandle(h_proc)
        return

    log_file.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] UIA Initialized. Starting background monitoring loop...\n")
    log_file.flush()

    tracker = RateLimitTracker(cooldown_seconds=30)
    last_usage_check = 0.0

    try:
        exit_code = wintypes.DWORD()
        while True:
            # Check target process liveness
            if kernel32.GetExitCodeProcess(h_proc, ctypes.byref(exit_code)):
                if exit_code.value != STILL_ACTIVE:
                    log_file.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ChatGPT process exited (Code {exit_code.value}). Worker shutting down.\n")
                    break

            try:
                text, prompt_box, continuation_btn = scrape_chatgpt_text_and_buttons(uia, hwnd)

                # Path 1: Token continuation button ("Continue generating")
                if continuation_btn and tracker.can_act():
                    log_file.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Active continuation button detected. Invoking...\n")
                    perform_resume_action(uia, hwnd, prompt_box, continuation_btn, resume_text, log_file)
                    tracker.last_action_timestamp = time.time()
                    time.sleep(5)
                    continue

                # Path 2: Check if chat mentions rate limit
                has_chat_limit = extract_active_rate_limit(text, margin_seconds, fallback_seconds, resume_text) is not None

                # Check usage ONLY when a rate limit is actively signaled in the chat (never periodic!)
                if has_chat_limit and tracker.can_act():
                    log_file.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Rate limit detected in chat. Verifying live quota in profile menu...\n")
                    log_file.flush()
                    status = scrape_profile_usage_status(hwnd)
                    if status:
                        pct_5h = status.get("5h_pct", 100)
                        reset_5h = status.get("5h_reset_str", "?")
                        log_file.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Live Quota: 5h Limit: {pct_5h}% (resets at {reset_5h}) | Weekly: {status.get('weekly_pct', '?')}%\n")
                        log_file.flush()

                        # PRIMARY GROUND TRUTH: If 5h usage is 0%, rate limit is ACTIVE!
                        if pct_5h <= 0 and status.get("5h_target_dt"):
                            wait_sec = status["5h_wait_sec"] + margin_seconds
                            target_time = status["5h_target_dt"] + timedelta(seconds=margin_seconds)
                            fingerprint = f"5h_exhausted_{reset_5h}"

                            if not tracker.is_already_handled(fingerprint):
                                log_file.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 5-Hour limit reached (0% remaining)! Scheduled auto-resume for reset at {target_time.strftime('%H:%M:%S')} (waiting {wait_sec}s)...\n")
                                log_file.flush()

                                if prompt_box:
                                    com_release(prompt_box)

                                sleep_until = time.time() + wait_sec
                                while time.time() < sleep_until:
                                    if kernel32.GetExitCodeProcess(h_proc, ctypes.byref(exit_code)) and exit_code.value != STILL_ACTIVE:
                                        break
                                    time.sleep(min(5.0, max(0.1, sleep_until - time.time())))

                                if exit_code.value != STILL_ACTIVE:
                                    break

                                log_file.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 5h Limit wait expired. Triggering auto-resume action ('{resume_text}')...\n")
                                log_file.flush()

                                _, fresh_prompt, fresh_btn = scrape_chatgpt_text_and_buttons(uia, hwnd)
                                perform_resume_action(uia, hwnd, fresh_prompt, fresh_btn, resume_text, log_file)
                                tracker.mark_handled(fingerprint)
                                time.sleep(5)
                                continue
                        elif pct_5h > 0:
                            # 5-Hour limit has reset / quota is available!
                            log_file.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 5-Hour quota is available ({pct_5h}%). Rate limit is cleared! Triggering auto-resume action ('{resume_text}')...\n")
                            log_file.flush()
                            _, fresh_prompt, fresh_btn = scrape_chatgpt_text_and_buttons(uia, hwnd)
                            perform_resume_action(uia, hwnd, fresh_prompt, fresh_btn, resume_text, log_file)
                            tracker.mark_handled("5h_cleared_resume")
                            time.sleep(5)
                            continue

                # Path 3: Fallback verified chat rate limit (only if usage scraper was inconclusive)
                limit_info = extract_active_rate_limit(text, margin_seconds, fallback_seconds, resume_text)
                if limit_info and tracker.can_act():
                    target_time, wait_sec, fingerprint = limit_info

                    if not tracker.is_already_handled(fingerprint):
                        resume_time = datetime.now() + timedelta(seconds=wait_sec)
                        log_file.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Fallback verified chat rate limit detected! Waiting {wait_sec}s until {resume_time.strftime('%H:%M:%S')}...\n")
                        log_file.flush()

                        if prompt_box:
                            com_release(prompt_box)

                        sleep_until = time.time() + wait_sec
                        while time.time() < sleep_until:
                            if kernel32.GetExitCodeProcess(h_proc, ctypes.byref(exit_code)) and exit_code.value != STILL_ACTIVE:
                                break
                            time.sleep(min(5.0, max(0.1, sleep_until - time.time())))

                        if exit_code.value != STILL_ACTIVE:
                            break

                        log_file.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Wait expired. Triggering auto-resume action...\n")
                        log_file.flush()

                        _, fresh_prompt, fresh_btn = scrape_chatgpt_text_and_buttons(uia, hwnd)
                        perform_resume_action(uia, hwnd, fresh_prompt, fresh_btn, resume_text, log_file)
                        tracker.mark_handled(fingerprint)
                        time.sleep(5)
                        continue

                if prompt_box:
                    com_release(prompt_box)
                if continuation_btn:
                    com_release(continuation_btn)
            except Exception as loop_err:
                log_file.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Iteration error: {loop_err}\n")
                log_file.flush()

            time.sleep(5)
    finally:
        log_file.close()
        kernel32.CloseHandle(h_proc)
        if h_mutex:
            kernel32.CloseHandle(h_mutex)


def main():
    GENERIC_ALL = 0x10000000
    h_input = user32.OpenInputDesktop(0, False, GENERIC_ALL)
    if h_input:
        user32.SetThreadDesktop(h_input)

    parser = argparse.ArgumentParser(description="KeepGoing: Background auto-resume monitoring for ChatGPT Windows Desktop App.")
    parser.add_argument("pid", nargs="?", type=int, help="Optional Process ID of the ChatGPT instance.")
    parser.add_argument("--text", default="keep going", help="Continuation text to paste when limit resets (default: 'keep going').")
    parser.add_argument("--margin", type=int, default=60, help="Margin in seconds to wait after reset time (default: 60).")
    parser.add_argument("--fallback", type=int, default=3600, help="Fallback seconds to wait if reset time cannot be parsed (default: 3600).")
    parser.add_argument("--log-path", help="Custom path for the logfile.")
    parser.add_argument("--test-paste", action="store_true", help="Execute an immediate test paste after 3 seconds to verify input injection.")
    parser.add_argument("--check-quota", action="store_true", help="Inspect and display live 5-hour and weekly quota from profile menu.")

    args = parser.parse_args()

    hwnd, pid = find_chatgpt_window(args.pid)
    if not hwnd or not pid:
        print("[Error] Could not find any running ChatGPT Desktop app window.")
        print("Please ensure the ChatGPT app is started and visible on your desktop.")
        sys.exit(1)

    print(f"[KeepGoing] Successfully connected to ChatGPT Desktop (PID: {pid}, HWND: 0x{hwnd:08X})")

    if args.check_quota:
        print("[KeepGoing] Querying live quota from profile menu...")
        try:
            status = scrape_profile_usage_status(hwnd)
            if status:
                pct_5h = status.get("5h_pct", "?")
                reset_5h = status.get("5h_reset_str", "?")
                pct_w = status.get("weekly_pct", "?")
                reset_w = status.get("weekly_reset_str", "?")
                print(f"[KeepGoing] Current Live Quota Status:")
                print(f"  • 5-Hour Limit:  {pct_5h}% remaining (Resets at {reset_5h})")
                print(f"  • Weekly Limit:  {pct_w}% remaining (Resets at {reset_w})")
            else:
                print("[KeepGoing] Could not read quota from profile menu.")
        except Exception as e:
            print(f"[KeepGoing Error] Failed to read quota: {e}")
        sys.exit(0)

    if args.test_paste:
        print(f"[Test Mode] Initiating test paste of '{args.text}' in 3 seconds.")
        print("[Test Mode] Please look at your ChatGPT window...")
        for i in range(3, 0, -1):
            print(f"[Test Mode] Pasting in {i}...")
            time.sleep(1)
        try:
            uia = UIAWrapper()
            print("[Test Mode] Scanning ChatGPT elements...")
            _, prompt_box, action_button = scrape_chatgpt_text_and_buttons(uia, hwnd)
            print("[Test Mode] Activating window and injecting text...")
            perform_resume_action(uia, hwnd, prompt_box, action_button, args.text)
            print("[Test Mode] Success! Text was pasted and sent. Check your ChatGPT chat window.")
            sys.exit(0)
        except Exception as e:
            print(f"[Test Mode Error] Failed to execute test paste: {e}")
            sys.exit(1)

    log_p = args.log_path or os.path.join(os.path.expanduser("~"), "chatgpt_attach_log.txt")
    print(f"[KeepGoing] Background logging to: {log_p}")
    print("[KeepGoing] Monitor active. Press Ctrl+C in this window to stop.")

    monitor_chatgpt_session(
        hwnd=hwnd,
        pid=pid,
        resume_text=args.text,
        margin_seconds=args.margin,
        fallback_seconds=args.fallback,
        log_path=args.log_path
    )

if __name__ == "__main__":
    main()
