import ctypes
from ctypes import wintypes
import re
import sys
import os
import time
import argparse
from datetime import datetime, timedelta

# Windows Console & Process API Configuration
kernel32 = ctypes.windll.kernel32

STD_INPUT_HANDLE = -10
STD_OUTPUT_HANDLE = -11
KEY_EVENT = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259

# Ctypes structures for Windows Console API
class COORD(ctypes.Structure):
    _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

class SMALL_RECT(ctypes.Structure):
    _fields_ = [
        ("Left", wintypes.SHORT),
        ("Top", wintypes.SHORT),
        ("Right", wintypes.SHORT),
        ("Bottom", wintypes.SHORT)
    ]

class CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
    _fields_ = [
        ("dwSize", COORD),
        ("dwCursorPosition", COORD),
        ("wAttributes", wintypes.WORD),
        ("srWindow", SMALL_RECT),
        ("dwMaximumWindowSize", COORD)
    ]

class CHAR_UNION(ctypes.Union):
    _fields_ = [("UnicodeChar", wintypes.WCHAR), ("AsciiChar", wintypes.CHAR)]

class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [
        ("bKeyDown", wintypes.BOOL),
        ("wRepeatCount", wintypes.WORD),
        ("wVirtualKeyCode", wintypes.WORD),
        ("wVirtualScanCode", wintypes.WORD),
        ("uChar", CHAR_UNION),
        ("dwControlKeyState", wintypes.DWORD)
    ]

class EVENT_UNION(ctypes.Union):
    _fields_ = [
        ("KeyEvent", KEY_EVENT_RECORD),
        ("Filler", wintypes.DWORD * 4)
    ]

class INPUT_RECORD(ctypes.Structure):
    _fields_ = [
        ("EventType", wintypes.WORD),
        ("Event", EVENT_UNION)
    ]

# Setup Windows API signatures
kernel32.AttachConsole.argtypes = [wintypes.DWORD]
kernel32.AttachConsole.restype = wintypes.BOOL

kernel32.FreeConsole.argtypes = []
kernel32.FreeConsole.restype = wintypes.BOOL

kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
kernel32.GetStdHandle.restype = wintypes.HANDLE

kernel32.GetConsoleScreenBufferInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(CONSOLE_SCREEN_BUFFER_INFO)]
kernel32.GetConsoleScreenBufferInfo.restype = wintypes.BOOL

kernel32.ReadConsoleOutputCharacterW.argtypes = [
    wintypes.HANDLE,
    wintypes.LPWSTR,
    wintypes.DWORD,
    COORD,
    ctypes.POINTER(wintypes.DWORD)
]
kernel32.ReadConsoleOutputCharacterW.restype = wintypes.BOOL

kernel32.WriteConsoleInputW.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(INPUT_RECORD),
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD)
]
kernel32.WriteConsoleInputW.restype = wintypes.BOOL

# Process checking API signatures
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE

kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetExitCodeProcess.restype = wintypes.BOOL

kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE

# Toolhelp32 Snapshot API for fast process enumeration
TH32CS_SNAPPROCESS = 0x00000002

class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260)
    ]

class FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", wintypes.DWORD),
        ("dwHighDateTime", wintypes.DWORD)
    ]

kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE

kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32FirstW.restype = wintypes.BOOL

kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.restype = wintypes.BOOL

kernel32.GetProcessTimes.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME)
]
kernel32.GetProcessTimes.restype = wintypes.BOOL


# ──────────────────────────────────────────────────────────────
# OpenAI Codex CLI rate limit patterns
# ──────────────────────────────────────────────────────────────
LIMIT_PATTERNS = [
    re.compile(r"You['\u2019]ve hit your usage limit", re.I),
    re.compile(r'try again at', re.I),
    re.compile(r'rate limit', re.I),
    re.compile(r'Rate limit exceeded', re.I),
    re.compile(r'\b429\b'),
    re.compile(r'usage limit', re.I),
]

# Reset time patterns for Codex CLI
# 1) Absolute date+time: "try again at Jul 20th, 2026 3:45 PM"
RESET_ABSOLUTE_DATETIME = re.compile(
    r'try\s+again\s+at\s+'
    r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+'
    r'(\d{1,2})(?:st|nd|rd|th)?,?\s*'
    r'(\d{4})\s+'
    r'(\d{1,2}):(\d{2})\s*(AM|PM)',
    re.I
)

# 2) Time only: "try again at 2:57 PM"
RESET_TIME_ONLY = re.compile(
    r'try\s+again\s+at\s+(\d{1,2}):(\d{2})\s*(AM|PM)',
    re.I
)

# 3) Relative: "try again in 30 minutes" / "try again in 2 hours"
RESET_RELATIVE = re.compile(
    r'try\s+again\s+in\s+(\d+)\s*(s|sec|seconds?|m|min|minutes?|h|hr|hours?)',
    re.I
)

# 4) Generic: "retry in X seconds/minutes"
RESET_GENERIC = re.compile(
    r'retry\s+in\s+(\d+)\s*(s|sec|seconds?|m|min|minutes?|h|hr|hours?)',
    re.I
)

MONTH_MAP = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4,
    'may': 5, 'jun': 6, 'jul': 7, 'aug': 8,
    'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12
}


def is_rate_limited(text):
    """Check if the console text contains a Codex rate limit message."""
    lines = text.split('\n')
    for line in lines:
        if any(p.search(line) for p in LIMIT_PATTERNS):
            return True
    return False


def _parse_unit_to_seconds(amount, unit):
    """Convert an amount+unit pair to seconds."""
    u = unit.lower()
    if u.startswith('h'):
        return amount * 3600
    elif u.startswith('m'):
        return amount * 60
    return amount  # seconds


def get_wait_seconds(text, margin_seconds=60, fallback_seconds=3600):
    """
    Parse the console text for a Codex reset time and return wait seconds.
    
    Tries patterns in order of specificity:
    1. Absolute date+time: "try again at Jul 20th, 2026 3:45 PM"
    2. Time-only: "try again at 2:57 PM"  
    3. Relative: "try again in 30 minutes"
    4. Generic retry: "retry in 60 seconds"
    
    Returns fallback_seconds (default 1 hour) if nothing can be parsed.
    """
    lines = text.split('\n')
    
    for line in reversed(lines):
        # 1) Absolute date+time: "try again at Jul 20th, 2026 3:45 PM"
        match = RESET_ABSOLUTE_DATETIME.search(line)
        if match:
            month_str, day_str, year_str, hour_str, minute_str, ampm = match.groups()
            month = MONTH_MAP.get(month_str.lower()[:3], 1)
            day = int(day_str)
            year = int(year_str)
            hour = int(hour_str)
            minute = int(minute_str)
            
            # Convert 12-hour to 24-hour
            if ampm.upper() == 'PM' and hour != 12:
                hour += 12
            elif ampm.upper() == 'AM' and hour == 12:
                hour = 0
                
            try:
                target_time = datetime(year, month, day, hour, minute)
                delta = (target_time - datetime.now()).total_seconds()
                if delta > 0:
                    return int(delta) + margin_seconds
            except ValueError:
                pass
        
        # 2) Time-only: "try again at 2:57 PM"
        # (Only match if the absolute pattern didn't already match on this line)
        if not RESET_ABSOLUTE_DATETIME.search(line):
            match = RESET_TIME_ONLY.search(line)
            if match:
                hour_str, minute_str, ampm = match.groups()
                hour = int(hour_str)
                minute = int(minute_str)
                
                if ampm.upper() == 'PM' and hour != 12:
                    hour += 12
                elif ampm.upper() == 'AM' and hour == 12:
                    hour = 0
                    
                now = datetime.now()
                target_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                
                # If the target time is already past, assume it means tomorrow
                if target_time <= now:
                    target_time += timedelta(days=1)
                    
                delta = (target_time - now).total_seconds()
                if delta > 0:
                    return int(delta) + margin_seconds
        
        # 3) Relative: "try again in 30 minutes"
        match = RESET_RELATIVE.search(line)
        if match:
            amount = int(match.group(1))
            unit = match.group(2)
            return _parse_unit_to_seconds(amount, unit) + margin_seconds
        
        # 4) Generic: "retry in 60 seconds"
        match = RESET_GENERIC.search(line)
        if match:
            amount = int(match.group(1))
            unit = match.group(2)
            return _parse_unit_to_seconds(amount, unit) + margin_seconds
    
    return fallback_seconds


# ──────────────────────────────────────────────────────────────
# Process detection via ctypes Toolhelp32Snapshot (fast, no powershell)
# ──────────────────────────────────────────────────────────────

def get_process_creation_time(pid):
    """Get the creation time of a process for sorting (newest first)."""
    h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h_proc:
        return 0
    creation_time = FILETIME()
    exit_time = FILETIME()
    kernel_time = FILETIME()
    user_time = FILETIME()
    success = kernel32.GetProcessTimes(h_proc, ctypes.byref(creation_time), ctypes.byref(exit_time), ctypes.byref(kernel_time), ctypes.byref(user_time))
    kernel32.CloseHandle(h_proc)
    if success:
        return (creation_time.dwHighDateTime << 32) | creation_time.dwLowDateTime
    return 0


def find_active_codex_processes():
    """
    Find active OpenAI Codex CLI processes using Toolhelp32Snapshot.
    Looks for processes named 'codex.exe' or 'node.exe' (with 'codex' in context).
    """
    try:
        h_snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if h_snap == wintypes.HANDLE(-1).value or h_snap is None:
            return []
            
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        
        codex_candidates = []
        node_candidates = []
        if kernel32.Process32FirstW(h_snap, ctypes.byref(pe)):
            while True:
                name = pe.szExeFile.lower()
                if "codex" in name:
                    codex_candidates.append({
                        "pid": pe.th32ProcessID,
                        "name": pe.szExeFile
                    })
                elif "node" in name:
                    node_candidates.append({
                        "pid": pe.th32ProcessID,
                        "name": pe.szExeFile
                    })
                if not kernel32.Process32NextW(h_snap, ctypes.byref(pe)):
                    break
        kernel32.CloseHandle(h_snap)
        
        # Prefer codex.exe processes over node.exe
        candidates = codex_candidates if codex_candidates else node_candidates
        
        results = []
        for c in candidates:
            ctime = get_process_creation_time(c["pid"])
            c["creation_time"] = ctime
            results.append(c)
            
        # Sort by creation time, newest first
        results.sort(key=lambda x: x["creation_time"], reverse=True)
        return [{"pid": r["pid"], "cmd": r["name"]} for r in results]
    except Exception as e:
        print(f"[Error] Failed to enumerate processes: {e}")
        return []


# ──────────────────────────────────────────────────────────────
# Console I/O helpers
# ──────────────────────────────────────────────────────────────

def read_console_text(h_stdout):
    """Read the last ~50 lines from the attached console buffer."""
    info = CONSOLE_SCREEN_BUFFER_INFO()
    if not kernel32.GetConsoleScreenBufferInfo(h_stdout, ctypes.byref(info)):
        return ""
    
    width = info.dwSize.X
    height = info.dwSize.Y
    
    num_lines = min(50, info.dwCursorPosition.Y + 1)
    start_y = max(0, info.dwCursorPosition.Y - num_lines + 1)
    
    total_chars = width * num_lines
    buffer = ctypes.create_unicode_buffer(total_chars)
    read = wintypes.DWORD(0)
    
    coord = COORD(0, start_y)
    
    if not kernel32.ReadConsoleOutputCharacterW(h_stdout, buffer, total_chars, coord, ctypes.byref(read)):
        return ""
        
    text = buffer.value
    lines = []
    for i in range(0, total_chars, width):
        lines.append(text[i:i+width].rstrip())
    return "\n".join(lines)


def send_enter_to_console(h_stdin):
    """Send a single Enter keypress to the attached console."""
    text = "\n"
    for char in text:
        ev_down = INPUT_RECORD()
        ev_down.EventType = KEY_EVENT
        ev_down.Event.KeyEvent.bKeyDown = True
        ev_down.Event.KeyEvent.wRepeatCount = 1
        
        if char == '\n' or char == '\r':
            ev_down.Event.KeyEvent.wVirtualKeyCode = 13  # VK_RETURN
            ev_down.Event.KeyEvent.wVirtualScanCode = 28  # Enter scan code
            ev_down.Event.KeyEvent.uChar.UnicodeChar = '\r'
        else:
            ev_down.Event.KeyEvent.wVirtualKeyCode = 0
            ev_down.Event.KeyEvent.wVirtualScanCode = 0
            ev_down.Event.KeyEvent.uChar.UnicodeChar = char
            
        ev_down.Event.KeyEvent.dwControlKeyState = 0
        
        ev_up = INPUT_RECORD()
        ev_up.EventType = KEY_EVENT
        ev_up.Event.KeyEvent.bKeyDown = False
        ev_up.Event.KeyEvent.wRepeatCount = 1
        
        if char == '\n' or char == '\r':
            ev_up.Event.KeyEvent.wVirtualKeyCode = 13  # VK_RETURN
            ev_up.Event.KeyEvent.wVirtualScanCode = 28  # Enter scan code
            ev_up.Event.KeyEvent.uChar.UnicodeChar = '\r'
        else:
            ev_up.Event.KeyEvent.wVirtualKeyCode = 0
            ev_up.Event.KeyEvent.wVirtualScanCode = 0
            ev_up.Event.KeyEvent.uChar.UnicodeChar = char
            
        ev_up.Event.KeyEvent.dwControlKeyState = 0
        
        written = wintypes.DWORD(0)
        kernel32.WriteConsoleInputW(h_stdin, ctypes.byref(ev_down), 1, ctypes.byref(written))
        kernel32.WriteConsoleInputW(h_stdin, ctypes.byref(ev_up), 1, ctypes.byref(written))


def is_process_alive(h_process):
    """Check if a process handle still refers to a running process."""
    if not h_process:
        return False
    exit_code = wintypes.DWORD()
    if kernel32.GetExitCodeProcess(h_process, ctypes.byref(exit_code)):
        return exit_code.value == STILL_ACTIVE
    return False


# ──────────────────────────────────────────────────────────────
# Main entry point
# ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="KeepGoing: Windows-native background auto-retry wrapper for OpenAI Codex CLI.")
    parser.add_argument("pid", nargs="?", type=int, default=None, help="Process ID of the Codex console session to attach to.")
    parser.add_argument("--margin", type=int, default=60, help="Margin in seconds to wait after rate-limit reset (default 60).")
    parser.add_argument("--fallback", type=int, default=3600, help="Fallback seconds to wait if reset time cannot be parsed (default 3600 = 1 hour).")
    parser.add_argument("--log-path", type=str, default=None, help="Custom path for the logfile.")
    
    args = parser.parse_args()

    target_pid = args.pid
    if target_pid is None:
        print("[Attach] Searching for active Codex sessions...")
        processes = find_active_codex_processes()
        
        if not processes:
            print("[Attach] No active Codex session found.")
            sys.exit(1)
            
        print(f"\n[Attach] Found Codex processes:")
        for idx, p in enumerate(processes):
            print(f" [{idx}] PID: {p['pid']} | Process: {p['cmd']}")
            
        target_idx = 0
        if len(processes) > 1:
            try:
                choice = input(f"\nPlease select the process index (0-{len(processes)-1}) [Default 0]: ").strip()
                if choice:
                    target_idx = int(choice)
            except Exception:
                target_idx = 0
                
        target_pid = processes[target_idx]['pid']

    # Check for duplicate watchers using a named Mutex
    mutex_name = f"Local\\KeepGoing_Codex_{target_pid}"
    h_mutex = kernel32.CreateMutexW(None, False, mutex_name)
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        if h_mutex:
            kernel32.CloseHandle(h_mutex)
        sys.exit(0)

    # Get process handle to check for liveness
    h_process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, target_pid)
    if not h_process:
        print(f"[Error] Failed to open process handle for PID {target_pid}. Error: {kernel32.GetLastError()}")
        if h_mutex:
            kernel32.CloseHandle(h_mutex)
        sys.exit(1)

    log_path = args.log_path
    if log_path is None:
        log_path = os.path.join(os.path.expanduser("~"), "codex_attach_log.txt")

    print(f"\n[Attach] Attaching to Codex console of process with PID {target_pid}...")
    print("[Attach] Successfully initialized! This window is now muted.")
    print(f"[Attach] Logs are written to: {log_path}\n")
    sys.stdout.flush()
    time.sleep(0.5)
    
    # Detach from our console and attach to target
    kernel32.FreeConsole()
    if not kernel32.AttachConsole(target_pid):
        kernel32.AllocConsole()
        print(f"[Error] Failed to attach console to PID {target_pid}. Error: {kernel32.GetLastError()}")
        kernel32.CloseHandle(h_process)
        sys.exit(1)
        
    h_stdin = kernel32.GetStdHandle(STD_INPUT_HANDLE)
    h_stdout = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
    
    with open(log_path, "a") as log:
        log.write(f"\n--- Codex Monitoring started for PID {target_pid} at {datetime.now()} ---\n")
        log.flush()
        
        last_check_rate_limit = False
        
        try:
            while True:
                # Check process alive status
                if not is_process_alive(h_process):
                    log.write(f"[{datetime.now()}] Target process {target_pid} exited. Stopping monitor.\n")
                    log.flush()
                    break
                    
                screen_text = read_console_text(h_stdout)
                
                if is_rate_limited(screen_text):
                    if not last_check_rate_limit:
                        last_check_rate_limit = True
                        wait_seconds = get_wait_seconds(screen_text, margin_seconds=args.margin, fallback_seconds=args.fallback)
                        
                        log.write(f"[{datetime.now()}] Codex rate limit detected! Waiting for {wait_seconds}s...\n")
                        log.flush()
                        
                        time.sleep(wait_seconds)
                        
                        send_enter_to_console(h_stdin)
                        
                        log.write(f"[{datetime.now()}] Enter successfully sent to Codex console!\n")
                        log.flush()
                else:
                    last_check_rate_limit = False
                    
                time.sleep(2)
                
        except Exception as e:
            with open(log_path, "a") as f:
                f.write(f"[Crash] Monitoring terminated due to error: {e}\n")
        finally:
            kernel32.FreeConsole()
            kernel32.CloseHandle(h_process)
            if h_mutex:
                kernel32.CloseHandle(h_mutex)

if __name__ == '__main__':
    main()
