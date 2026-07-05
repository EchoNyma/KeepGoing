import ctypes
from ctypes import wintypes
import re
import sys
import os
import time
import json
import argparse
from datetime import datetime, timedelta

# Windows Console & Process API Configuration
kernel32 = ctypes.windll.kernel32

STD_INPUT_HANDLE = -10
STD_OUTPUT_HANDLE = -11
KEY_EVENT = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259

# Setup CreateFileW
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3

kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE
]
kernel32.CreateFileW.restype = wintypes.HANDLE

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
RESET_ABSOLUTE_DATETIME = re.compile(
    r'try\s+again\s+at\s+'
    r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+'
    r'(\d{1,2})(?:st|nd|rd|th)?,?\s*'
    r'(\d{4})\s+'
    r'(\d{1,2}):(\d{2})\s*(AM|PM)',
    re.I
)

RESET_TIME_ONLY = re.compile(
    r'try\s+again\s+at\s+(\d{1,2}):(\d{2})\s*(AM|PM)',
    re.I
)

RESET_RELATIVE = re.compile(
    r'try\s+again\s+in\s+(\d+)\s*(s|sec|seconds?|m|min|minutes?|h|hr|hours?)',
    re.I
)

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
                else:
                    return 0
            except ValueError:
                pass
        
        # 2) Time-only: "try again at 2:57 PM"
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
                
                # If target time is in the past, check if it's recent (less than 12 hours ago).
                # If so, the limit has already reset, so we return 0.
                # Otherwise, assume the reset time is for tomorrow.
                if target_time <= now:
                    if (now - target_time).total_seconds() < 12 * 3600:
                        return 0
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
# Process detection via ctypes Toolhelp32Snapshot
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
        
        candidates = codex_candidates if codex_candidates else node_candidates
        
        results = []
        for c in candidates:
            ctime = get_process_creation_time(c["pid"])
            c["creation_time"] = ctime
            results.append(c)
            
        results.sort(key=lambda x: x["creation_time"], reverse=True)
        return [{"pid": r["pid"], "cmd": r["name"]} for r in results]
    except Exception as e:
        print(f"[Error] Failed to enumerate processes: {e}")
        return []


def get_grandparent_pid():
    """Returns the PID of the grandparent process (the shell running Codex) using ctypes."""
    my_pid = os.getpid()
    
    h_snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if h_snap == wintypes.HANDLE(-1).value or h_snap is None:
        return None
        
    pe = PROCESSENTRY32W()
    pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    
    parent_pid = None
    grandparent_pid = None
    
    if kernel32.Process32FirstW(h_snap, ctypes.byref(pe)):
        while True:
            if pe.th32ProcessID == my_pid:
                parent_pid = pe.th32ParentProcessID
                break
            if not kernel32.Process32NextW(h_snap, ctypes.byref(pe)):
                break
                
        if parent_pid is not None:
            kernel32.CloseHandle(h_snap)
            h_snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
            if h_snap != wintypes.HANDLE(-1).value and h_snap is not None:
                pe = PROCESSENTRY32W()
                pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
                if kernel32.Process32FirstW(h_snap, ctypes.byref(pe)):
                    while True:
                        if pe.th32ProcessID == parent_pid:
                            grandparent_pid = pe.th32ParentProcessID
                            break
                        if not kernel32.Process32NextW(h_snap, ctypes.byref(pe)):
                            break
                            
    if h_snap and h_snap != wintypes.HANDLE(-1).value:
        kernel32.CloseHandle(h_snap)
        
    return grandparent_pid


def find_codex_ancestor():
    """Tries to find the first ancestor process whose name contains 'codex', 'node', or 'python' by walking up the process tree."""
    my_pid = os.getpid()
    current_pid = my_pid
    visited = set()
    
    for level in range(15):
        h_snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if h_snap == wintypes.HANDLE(-1).value or h_snap is None:
            break
            
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        
        parent_pid = None
        process_name = ""
        
        if kernel32.Process32FirstW(h_snap, ctypes.byref(pe)):
            while True:
                if pe.th32ProcessID == current_pid:
                    parent_pid = pe.th32ParentProcessID
                    process_name = pe.szExeFile.lower()
                    break
                if not kernel32.Process32NextW(h_snap, ctypes.byref(pe)):
                    break
                    
        kernel32.CloseHandle(h_snap)
        
        if not parent_pid or parent_pid in visited or parent_pid == 0:
            break
            
        if current_pid != my_pid:
            if "codex" in process_name or "node" in process_name or "python" in process_name:
                return {"pid": current_pid, "name": process_name, "level": level}
                
        visited.add(current_pid)
        current_pid = parent_pid
        
    return None


# ──────────────────────────────────────────────────────────────
# Console I/O helpers
# ──────────────────────────────────────────────────────────────

def read_console_text(h_stdout):
    """Read the last ~50 lines from the attached console buffer."""
    info = CONSOLE_SCREEN_BUFFER_INFO()
    if not kernel32.GetConsoleScreenBufferInfo(h_stdout, ctypes.byref(info)):
        return ""
    
    width = info.dwSize.X
    
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


def send_resume_to_console(h_stdin):
    """Send 'keep going' command followed by Enter to the attached console."""
    text = "keep going\n"
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


def _find_pythonw():
    """Finds pythonw.exe next to the current Python interpreter."""
    python_dir = os.path.dirname(sys.executable)
    pythonw = os.path.join(python_dir, "pythonw.exe")
    if os.path.exists(pythonw):
        return pythonw
    import shutil
    return shutil.which("pythonw.exe")


def install_settings_hook():
    """Installs the SessionStart hook in global Codex config.toml and hooks.json files."""
    codex_dir = os.path.join(os.path.expanduser("~"), ".codex")
    config_path = os.path.join(codex_dir, "config.toml")
    hooks_path = os.path.join(codex_dir, "hooks.json")
    
    current_script = os.path.abspath(__file__)
    script_dir = os.path.dirname(current_script)
    
    pythonw = _find_pythonw()
    if not pythonw:
        print("[Error] pythonw.exe not found. Please install Python with the standard Windows installer.")
        sys.exit(1)
        
    hook_cmd_path = os.path.join(script_dir, "keepgoing_hook_codex.cmd")
    with open(hook_cmd_path, "w", encoding="utf-8") as f:
        f.write('@echo off\n')
        f.write(f'powershell.exe -WindowStyle Hidden -Command "Start-Process \'{pythonw}\' -ArgumentList @(\'{current_script}\', \'--hook\') ; Start-Sleep -Milliseconds 500"\n')
        
    hook_command = f'"{hook_cmd_path}"'
    
    print(f"[Install] Using pythonw.exe: {pythonw}")
    print(f"[Install] Generated hook wrapper: {hook_cmd_path}")
    print(f"[Install] Target config directory: {codex_dir}")
    
    # 1. Enable hooks in config.toml
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config_content = f.read()
            
            has_hooks_enabled = re.search(r'hooks\s*=\s*true', config_content, re.I)
            if not has_hooks_enabled:
                print("[Install] Enabling hooks feature in config.toml...")
                new_content = config_content
                if "[features]" in config_content:
                    new_content = re.sub(r'(\[features\])', r'\1\nhooks = true', config_content, flags=re.I)
                else:
                    new_content += "\n\n[features]\nhooks = true\n"
                with open(config_path, "w", encoding="utf-8") as f:
                    f.write(new_content)
        except Exception as e:
            print(f"[Warning] Failed to update config.toml: {e}")
    else:
        try:
            os.makedirs(codex_dir, exist_ok=True)
            with open(config_path, "w", encoding="utf-8") as f:
                f.write("[features]\nhooks = true\n")
            print("[Install] Created config.toml with hooks enabled.")
        except Exception as e:
            print(f"[Error] Failed to create config.toml: {e}")
            sys.exit(1)
            
    # 2. Add hook to hooks.json
    hooks_data = {}
    if os.path.exists(hooks_path):
        try:
            with open(hooks_path, "r", encoding="utf-8") as f:
                hooks_data = json.load(f)
        except Exception as e:
            print(f"[Error] Failed to read hooks.json: {e}")
            sys.exit(1)
            
    if "hooks" not in hooks_data:
        hooks_data["hooks"] = {}
    if "SessionStart" not in hooks_data["hooks"]:
        hooks_data["hooks"]["SessionStart"] = []
        
    session_start_hooks = hooks_data["hooks"]["SessionStart"]
    hook_exists = False
    clean_hooks = []
    
    for hook_entry in session_start_hooks:
        is_keepgoing = False
        for hook_action in hook_entry.get("hooks", []):
            cmd = hook_action.get("command", "")
            if "codex_attach" in cmd or "keepgoing_hook_codex" in cmd:
                is_keepgoing = True
                break
        if is_keepgoing:
            if not hook_exists:
                clean_hooks.append({
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": hook_command}]
                })
                hook_exists = True
                print("[Install] Found existing hook. Updating to current location...")
        else:
            clean_hooks.append(hook_entry)
            
    if not hook_exists:
        print("[Install] Registering new SessionStart hook in hooks.json...")
        clean_hooks.append({
            "matcher": "*",
            "hooks": [
                {
                    "type": "command",
                    "command": hook_command
                }
            ]
        })
        
    hooks_data["hooks"]["SessionStart"] = clean_hooks
    
    try:
        os.makedirs(codex_dir, exist_ok=True)
        with open(hooks_path, "w", encoding="utf-8") as f:
            json.dump(hooks_data, f, indent=2)
        print("[Install] Successfully configured Codex SessionStart hook!")
        print("[Install] KeepGoing will now start automatically whenever you run Codex CLI.")
    except Exception as e:
        print(f"[Error] Failed to write hooks.json: {e}")
        sys.exit(1)


def _log(log_path, message):
    """Append a message to the logfile. Safe to call even without a console."""
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now()}] {message}\n")
            f.flush()
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────
# Main entry point
# ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="KeepGoing: Windows-native background auto-retry wrapper for OpenAI Codex CLI.")
    parser.add_argument("pid", nargs="?", type=int, default=None, help="Process ID of the Codex console session to attach to.")
    parser.add_argument("--hook", action="store_true", help="Launch in automatic Hook mode (detects grandparent PID automatically).")
    parser.add_argument("--install", action="store_true", help="Register the SessionStart hook in global Codex config.")
    parser.add_argument("--margin", type=int, default=60, help="Margin in seconds to wait after rate-limit reset (default 60).")
    parser.add_argument("--fallback", type=int, default=3600, help="Fallback seconds to wait if reset time cannot be parsed (default 3600 = 1 hour).")
    parser.add_argument("--log-path", type=str, default=None, help="Custom path for the logfile.")
    
    args = parser.parse_args()

    if args.install:
        install_settings_hook()
        sys.exit(0)

    target_pid = args.pid
    is_hook = args.hook
    
    log_path = args.log_path
    if log_path is None:
        log_path = os.path.join(os.path.expanduser("~"), "codex_attach_log.txt")

    # If in hook mode, IMMEDIATELY lookup the ancestor process tree before any parent process exits
    ancestor = None
    if is_hook and target_pid is None:
        ancestor = find_codex_ancestor()
        if ancestor:
            target_pid = ancestor["pid"]
            _log(log_path, f"Hook mode: successfully resolved calling Codex process by walking ancestry tree: PID {target_pid} ({ancestor['name']}) at tree level {ancestor['level']}")
        else:
            _log(log_path, "Hook mode: failed to find Codex process in the parent tree.")

    # In hook mode, wait briefly so Codex CLI has time to fully initialize
    if is_hook:
        _log(log_path, f"Hook mode started (my PID={os.getpid()}, passed PID={target_pid})")
        time.sleep(2)

    if is_hook and target_pid is None:
        target_pid = get_grandparent_pid()
        _log(log_path, f"Grandparent PID lookup returned: {target_pid}")

    if target_pid is None:
        if not is_hook:
            print("[Attach] Searching for active Codex sessions...")
        processes = find_active_codex_processes()
        
        if not processes:
            if not is_hook:
                print("[Attach] No active Codex session found.")
            _log(log_path, "No active Codex session found. Exiting.")
            sys.exit(1)
            
        _log(log_path, f"Found {len(processes)} Codex process(es): {processes}")

        if not is_hook:
            print(f"\n[Attach] Found Codex processes:")
            for idx, p in enumerate(processes):
                print(f" [{idx}] PID: {p['pid']} | Process: {p['cmd']}")
            
        target_idx = 0
        if len(processes) > 1 and not is_hook:
            try:
                choice = input(f"\nPlease select the process index (0-{len(processes)-1}) [Default 0]: ").strip()
                if choice:
                    target_idx = int(choice)
            except Exception:
                target_idx = 0
                
        target_pid = processes[target_idx]['pid']

    _log(log_path, f"Target PID resolved to: {target_pid}")

    # Check for duplicate watchers using a named Mutex
    mutex_name = f"Local\\KeepGoing_Codex_{target_pid}"
    h_mutex = kernel32.CreateMutexW(None, False, mutex_name)
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        _log(log_path, f"Duplicate watcher detected for PID {target_pid}. Exiting.")
        if h_mutex:
            kernel32.CloseHandle(h_mutex)
        sys.exit(0)

    # Get process handle to check for liveness
    h_process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, target_pid)
    if not h_process:
        err = kernel32.GetLastError()
        if not is_hook:
            print(f"[Error] Failed to open process handle for PID {target_pid}. Error: {err}")
        _log(log_path, f"Failed to open process handle for PID {target_pid}. Error: {err}")
        if h_mutex:
            kernel32.CloseHandle(h_mutex)
        sys.exit(1)

    if not is_hook:
        print(f"\n[Attach] Attaching to Codex console of process with PID {target_pid}...")
        print("[Attach] Successfully initialized! This window is now muted.")
        print(f"[Attach] Logs are written to: {log_path}\n")
        sys.stdout.flush()
        time.sleep(0.5)
    
    # Detach from our console and attach to target
    kernel32.FreeConsole()
    if not kernel32.AttachConsole(target_pid):
        err = kernel32.GetLastError()
        if not is_hook:
            kernel32.AllocConsole()
            print(f"[Error] Failed to attach console to PID {target_pid}. Error: {err}")
        _log(log_path, f"Failed to AttachConsole to PID {target_pid}. Error: {err}")
        kernel32.CloseHandle(h_process)
        if h_mutex:
            kernel32.CloseHandle(h_mutex)
        sys.exit(1)
        
    # Open handles to the attached console buffer (using CreateFileW for CONIN$/CONOUT$ to bypass pythonw.exe std handle limits)
    h_stdin = kernel32.CreateFileW(
        "CONIN$",
        GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        0,
        None
    )
    if h_stdin == wintypes.HANDLE(-1).value or h_stdin is None or h_stdin == 0:
        h_stdin = kernel32.GetStdHandle(STD_INPUT_HANDLE)

    h_stdout = kernel32.CreateFileW(
        "CONOUT$",
        GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        0,
        None
    )
    if h_stdout == wintypes.HANDLE(-1).value or h_stdout is None or h_stdout == 0:
        h_stdout = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
    
    with open(log_path, "a") as log:
        log.write(f"\n--- Codex Monitoring started for PID {target_pid} at {datetime.now()} ---\n")
        
        # Verify screen buffer access
        info = CONSOLE_SCREEN_BUFFER_INFO()
        if not kernel32.GetConsoleScreenBufferInfo(h_stdout, ctypes.byref(info)):
            log.write(f"[{datetime.now()}] [Error] GetConsoleScreenBufferInfo failed for h_stdout={h_stdout}. Error code: {kernel32.GetLastError()}\n")
        else:
            log.write(f"[{datetime.now()}] Successfully attached to screen buffer. Dimensions: {info.dwSize.X}x{info.dwSize.Y}, Cursor: {info.dwCursorPosition.X},{info.dwCursorPosition.Y}\n")
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
                        
                        slept = 0
                        while slept < wait_seconds:
                            if not is_process_alive(h_process):
                                break
                            time.sleep(1)
                            slept += 1
                            
                        if not is_process_alive(h_process):
                            log.write(f"[{datetime.now()}] Target process {target_pid} exited during rate limit wait. Stopping monitor.\n")
                            log.flush()
                            break
                            
                        send_resume_to_console(h_stdin)
                        
                        log.write(f"[{datetime.now()}] 'keep going' command successfully sent to Codex console!\n")
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
