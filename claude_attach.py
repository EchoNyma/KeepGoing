import ctypes
from ctypes import wintypes
import re
import sys
import os
import time
import subprocess
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



# Regex patterns for Claude Code rate limits
LIMIT_PATTERNS = [
    re.compile(r'(?:hit|exceeded|reached).*(?:your|the)\s*(?:\d+-hour\s+)?limit', re.I),
    re.compile(r'\d+-hour limit', re.I),
    re.compile(r'limit reached', re.I),
    re.compile(r'usage limit', re.I),
    re.compile(r'out of.*usage', re.I),
    re.compile(r'rate limit', re.I),
    re.compile(r'try again in', re.I),
]

RESET_PATTERNS = [
    re.compile(r'resets?\s+(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)?', re.I),
    re.compile(r'resets?\s+in[:\s]\s*\d', re.I),
    re.compile(r'try again in \d+\s*(?:hours?|minutes?|h|m)', re.I),
]

def is_rate_limited(text):
    lines = text.split('\n')
    for i in range(len(lines)):
        if any(p.search(lines[i]) for p in LIMIT_PATTERNS):
            start = max(0, i - 6)
            end = min(len(lines), i + 7)
            for j in range(start, end):
                if any(p.search(lines[j]) for p in RESET_PATTERNS):
                    return True
    return False

def find_reset_line(text):
    lines = text.split('\n')
    for line in reversed(lines):
        if any(p.search(line) for p in RESET_PATTERNS):
            return line.strip()
    return None

RESET_TIME_REGEX = re.compile(r'resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(?:\(([^)]+)\))?', re.I)
RELATIVE_TIME_REGEX = re.compile(r'(?:try again|wait|resets?\s+in)[:\s]\s*(?:for\s+)?(?:in\s+)?(\d+)\s*(hours?|minutes?|mins?|h|m)\b', re.I)

def get_wait_seconds(text, margin_seconds=60, fallback_hours=5):
    reset_line = find_reset_line(text)
    if not reset_line:
        return fallback_hours * 3600 + margin_seconds
    
    abs_match = RESET_TIME_REGEX.search(reset_line)
    if abs_match:
        hour = int(abs_match.group(1))
        minute = int(abs_match.group(2)) if abs_match.group(2) else 0
        ampm = abs_match.group(3).lower() if abs_match.group(3) else None
        
        if ampm == 'pm' and hour != 12:
            hour += 12
        elif ampm == 'am' and hour == 12:
            hour = 0
            
        now = datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target < now:
            # If the target time is in the past, check if it's recent (less than 12 hours ago).
            # If so, the limit has already reset and we can proceed immediately.
            # Otherwise, the reset time is tomorrow.
            if (now - target).total_seconds() < 12 * 3600:
                diff = 0
            else:
                target += timedelta(days=1)
                diff = (target - now).total_seconds()
        else:
            diff = (target - now).total_seconds()
            
        return max(0, int(diff)) + margin_seconds
        
    rel_match = RELATIVE_TIME_REGEX.search(reset_line)
    if rel_match:
        amount = int(rel_match.group(1))
        unit = rel_match.group(2).lower()
        is_minutes = unit.startswith('m')
        seconds = amount * (60 if is_minutes else 3600)
        return seconds + margin_seconds
        
    return fallback_hours * 3600 + margin_seconds


def get_grandparent_pid():
    """Returns the PID of the grandparent process (the shell running Claude) using ctypes."""
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


def find_claude_ancestor():
    """Tries to find the first ancestor process whose name contains 'claude' or 'node' by walking up the process tree."""
    my_pid = os.getpid()
    current_pid = my_pid
    visited = set()
    
    # We walk up to 15 levels to find the calling Claude Code process.
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
            
        # Do not check our own name (pythonw)
        if current_pid != my_pid:
            # We check if the process name is claude.exe or node.exe
            if "claude" in process_name or "node" in process_name:
                return {"pid": current_pid, "name": process_name, "level": level}
                
        visited.add(current_pid)
        current_pid = parent_pid
        
    return None


def get_process_creation_time(pid):
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


def find_active_claude_processes():
    try:
        h_snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if h_snap == wintypes.HANDLE(-1).value or h_snap is None:
            return []
            
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        
        claude_candidates = []
        node_candidates = []
        if kernel32.Process32FirstW(h_snap, ctypes.byref(pe)):
            while True:
                name = pe.szExeFile.lower()
                if "claude" in name:
                    claude_candidates.append({
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
        
        # Prefer claude.exe processes over node.exe
        candidates = claude_candidates if claude_candidates else node_candidates
        
        results = []
        for c in candidates:
            ctime = get_process_creation_time(c["pid"])
            c["creation_time"] = ctime
            results.append(c)
            
        results.sort(key=lambda x: x["creation_time"], reverse=True)
        return [{"pid": r["pid"], "cmd": r["name"]} for r in results]
    except Exception as e:
        return []


def read_console_text(h_stdout):
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


def send_continue_to_console(h_stdin):
    text = "continue\n"
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


def _find_pythonw():
    """Finds pythonw.exe next to the current Python interpreter."""
    python_dir = os.path.dirname(sys.executable)
    pythonw = os.path.join(python_dir, "pythonw.exe")
    if os.path.exists(pythonw):
        return pythonw
    # Check PATH
    import shutil
    return shutil.which("pythonw.exe")


def install_settings_hook():
    """Installs the SessionStart hook in global Claude settings.json file."""
    settings_dir = os.path.join(os.path.expanduser("~"), ".claude")
    settings_path = os.path.join(settings_dir, "settings.json")
    
    # Target command path (current file)
    current_script = os.path.abspath(__file__)
    script_dir = os.path.dirname(current_script)
    
    # Create a .cmd wrapper that launches pythonw.exe in the background using PowerShell.
    # This avoids conhost blocking on standard handles and keeps the parent alive for 500ms.
    pythonw = _find_pythonw()
    if not pythonw:
        print("[Error] pythonw.exe not found. Please install Python with the standard Windows installer.")
        sys.exit(1)
    
    hook_cmd_path = os.path.join(script_dir, "keepgoing_hook.cmd")
    with open(hook_cmd_path, "w", encoding="utf-8") as f:
        f.write(f'@echo off\n')
        f.write(f'powershell.exe -WindowStyle Hidden -Command "Start-Process \'{pythonw}\' -ArgumentList @(\'{current_script}\', \'--hook\') ; Start-Sleep -Milliseconds 500"\n')
    
    hook_command = f'"{hook_cmd_path}"'
    
    print(f"[Install] Using pythonw.exe: {pythonw}")
    print(f"[Install] Generated hook wrapper: {hook_cmd_path}")
    
    print(f"[Install] Target settings file: {settings_path}")
    print(f"[Install] Hook command: {hook_command}")
    
    # Load existing settings
    settings = {}
    if os.path.exists(settings_path):
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                settings = json.load(f)
        except Exception as e:
            print(f"[Error] Failed to read settings.json: {e}")
            sys.exit(1)
            
    # Ensure hooks structure exists
    if "hooks" not in settings:
        settings["hooks"] = {}
    if "SessionStart" not in settings["hooks"]:
        settings["hooks"]["SessionStart"] = []
        
    # Check if hook already exists, and remove any stale/old KeepGoing hooks
    session_start_hooks = settings["hooks"]["SessionStart"]
    hook_exists = False
    clean_hooks = []
    for hook_entry in session_start_hooks:
        is_keepgoing = False
        for hook_action in hook_entry.get("hooks", []):
            cmd = hook_action.get("command", "")
            if "claude_attach" in cmd or "keepgoing_hook" in cmd or "KeepGoing" in cmd:
                is_keepgoing = True
                break
        if is_keepgoing:
            # Replace with updated hook
            if not hook_exists:
                clean_hooks.append({
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": hook_command}]
                })
                hook_exists = True
                print("[Install] Found existing hook. Updating to current location...")
            # else: skip duplicate old hooks
        else:
            clean_hooks.append(hook_entry)
    
    if not hook_exists:
        print("[Install] Registering new SessionStart hook...")
        clean_hooks.append({
            "matcher": "*",
            "hooks": [
                {
                    "type": "command",
                    "command": hook_command
                }
            ]
        })
    
    settings["hooks"]["SessionStart"] = clean_hooks
        
    # Write back settings.json
    try:
        os.makedirs(settings_dir, exist_ok=True)
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
        print("[Install] Successfully configured Claude Code SessionStart hook!")
        print("[Install] KeepGoing will now start automatically whenever you run 'claude'.")
    except Exception as e:
        print(f"[Error] Failed to write settings.json: {e}")
        sys.exit(1)


def is_process_alive(h_process):
    """Checks if the target process is still active."""
    if not h_process:
        return False
    exit_code = wintypes.DWORD()
    if kernel32.GetExitCodeProcess(h_process, ctypes.byref(exit_code)):
        return exit_code.value == STILL_ACTIVE
    return False


def _log(log_path, message):
    """Append a message to the logfile. Safe to call even without a console."""
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now()}] {message}\n")
            f.flush()
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description="KeepGoing: Windows-native background auto-retry wrapper for Claude Code.")
    parser.add_argument("pid", nargs="?", type=int, default=None, help="Process ID of the Claude console session to attach to.")
    parser.add_argument("--hook", action="store_true", help="Launch in automatic Hook mode (detects grandparent PID automatically).")
    parser.add_argument("--install", action="store_true", help="Register the SessionStart hook in global Claude Code config.")
    parser.add_argument("--margin", type=int, default=60, help="Margin in seconds to wait after rate-limit reset (default 60).")
    parser.add_argument("--fallback", type=int, default=5, help="Fallback hours to wait if reset time cannot be parsed (default 5).")
    parser.add_argument("--log-path", type=str, default=None, help="Custom path for the logfile.")
    
    args = parser.parse_args()
    
    # 1. Installer check
    if args.install:
        install_settings_hook()
        sys.exit(0)

    target_pid = args.pid
    is_hook = args.hook
    
    log_path = args.log_path
    if log_path is None:
        log_path = os.path.join(os.path.expanduser("~"), "claude_attach_log.txt")

    # If in hook mode, IMMEDIATELY lookup the ancestor process tree before any parent process exits
    ancestor = None
    if is_hook and target_pid is None:
        ancestor = find_claude_ancestor()
        if ancestor:
            target_pid = ancestor["pid"]
            _log(log_path, f"Hook mode: successfully resolved calling Claude process by walking ancestry tree: PID {target_pid} ({ancestor['name']}) at tree level {ancestor['level']}")
        else:
            _log(log_path, "Hook mode: failed to find Claude process in the parent tree.")

    # In hook mode, wait briefly so Claude Code has time to fully initialize
    if is_hook:
        _log(log_path, f"Hook mode started (my PID={os.getpid()}, passed PID={target_pid})")
        time.sleep(2)

    if is_hook and target_pid is None:
        target_pid = get_grandparent_pid()
        _log(log_path, f"Grandparent PID lookup returned: {target_pid}")

    if target_pid is None:
        if not is_hook:
            print("[Attach] Searching for active Claude Code sessions...")
        processes = find_active_claude_processes()
        
        if not processes:
            if not is_hook:
                print("[Attach] No active Claude Code session found.")
            _log(log_path, "No active Claude Code session found. Exiting.")
            sys.exit(1)
        
        _log(log_path, f"Found {len(processes)} Claude process(es): {processes}")
        
        if not is_hook:
            print(f"\n[Attach] Found Claude processes:")
            for idx, p in enumerate(processes):
                print(f" [{idx}] PID: {p['pid']} | Command: {p['cmd'][:80]}...")
            
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
    mutex_name = f"Local\\KeepGoing_Claude_{target_pid}"
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
        print(f"\n[Attach] Attaching to console of process with PID {target_pid}...")
        print("[Attach] Successfully initialized! This window is now muted.")
        print(f"[Attach] Logs are written to: {log_path}\n")
        sys.stdout.flush()
        time.sleep(0.5)
    
    # Detach from original console (pythonw.exe has none, FreeConsole just returns False) and attach to target
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
        
    h_stdin = kernel32.GetStdHandle(STD_INPUT_HANDLE)
    h_stdout = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
    
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"\n--- Claude Monitoring started for PID {target_pid} at {datetime.now()} ---\n")
        log.flush()
        
        last_check_rate_limit = False
        
        try:
            while True:
                # Check if target console process has exited
                if not is_process_alive(h_process):
                    log.write(f"[{datetime.now()}] Target process {target_pid} exited. Stopping monitor.\n")
                    log.flush()
                    break
                    
                screen_text = read_console_text(h_stdout)
                
                if is_rate_limited(screen_text):
                    if not last_check_rate_limit:
                        last_check_rate_limit = True
                        wait_seconds = get_wait_seconds(screen_text, margin_seconds=args.margin, fallback_hours=args.fallback)
                        
                        log.write(f"[{datetime.now()}] Claude rate limit detected! Waiting for {wait_seconds}s...\n")
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
                            
                        send_continue_to_console(h_stdin)
                        
                        log.write(f"[{datetime.now()}] 'continue' successfully sent to console!\n")
                        log.flush()
                else:
                    last_check_rate_limit = False
                    
                time.sleep(2)
                
        except Exception as e:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"[Crash] Monitoring terminated due to error: {e}\n")
        finally:
            kernel32.FreeConsole()
            kernel32.CloseHandle(h_process)
            if h_mutex:
                kernel32.CloseHandle(h_mutex)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        # Last resort: log to file if main() crashes before logging is set up
        try:
            log_path = os.path.join(os.path.expanduser("~"), "claude_attach_log.txt")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now()}] [Fatal] Unhandled exception in main: {e}\n")
        except Exception:
            pass
