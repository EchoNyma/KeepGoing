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


# Gemini API / Antigravity rate limit patterns
LIMIT_PATTERNS = [
    re.compile(r'ResourceExhausted', re.I),
    re.compile(r'Quota exceeded', re.I),
    re.compile(r'Rate limit exceeded', re.I),
    re.compile(r'Too Many Requests', re.I),
    re.compile(r'\b429\b'),
    re.compile(r'limit.*exhausted', re.I),
    re.compile(r'API.*limit', re.I)
]

RESET_PATTERNS = [
    re.compile(r'retry\s+in\s+(\d+)\s*(s|sec|seconds?|m|min|minutes?)', re.I),
    re.compile(r'wait\s+(\d+)\s*(s|sec|seconds?|m|min|minutes?)', re.I),
    re.compile(r'try\s+again\s+in\s+(\d+)', re.I)
]

def is_rate_limited(text):
    lines = text.split('\n')
    for i in range(len(lines)):
        if any(p.search(lines[i]) for p in LIMIT_PATTERNS):
            return True
    return False

def get_wait_seconds(text, margin_seconds=5, fallback_seconds=60):
    lines = text.split('\n')
    for line in reversed(lines):
        for p in RESET_PATTERNS:
            match = p.search(line)
            if match:
                amount = int(match.group(1))
                unit = match.group(2).lower() if len(match.groups()) > 1 else 's'
                is_minutes = unit.startswith('m')
                return amount * (60 if is_minutes else 1) + margin_seconds
    return fallback_seconds


def find_active_antigravity_processes():
    try:
        cmd = 'powershell.exe -Command "Get-CimInstance Win32_Process -Filter \\"Name = \'node.exe\' or Name = \'antigravity.exe\'\\" | Select-Object ProcessId, CommandLine | ConvertTo-Json"'
        output = subprocess.check_output(cmd, shell=True).decode('utf-8', errors='ignore').strip()
        if not output:
            return []
            
        data = json.loads(output)
        if isinstance(data, dict):
            data = [data]
            
        results = []
        for p in data:
            pid = p.get('ProcessId')
            cmdline = p.get('CommandLine') or ""
            if 'antigravity' in cmdline.lower() and 'antigravity_attach' not in cmdline.lower():
                results.append({'pid': pid, 'cmd': cmdline})
        return results
    except Exception as e:
        print(f"[Error] Failed to search processes: {e}")
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


def send_enter_to_console(h_stdin):
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
    if not h_process:
        return False
    exit_code = wintypes.DWORD()
    if kernel32.GetExitCodeProcess(h_process, ctypes.byref(exit_code)):
        return exit_code.value == STILL_ACTIVE
    return False


def main():
    parser = argparse.ArgumentParser(description="KeepGoing: Windows-native background auto-retry wrapper for Antigravity CLI.")
    parser.add_argument("pid", nargs="?", type=int, default=None, help="Process ID of the Antigravity console session to attach to.")
    parser.add_argument("--margin", type=int, default=5, help="Margin in seconds to wait after rate-limit reset (default 5).")
    parser.add_argument("--fallback", type=int, default=60, help="Fallback seconds to wait if reset time cannot be parsed (default 60).")
    parser.add_argument("--log-path", type=str, default=None, help="Custom path for the logfile.")
    
    args = parser.parse_args()

    target_pid = args.pid
    if target_pid is None:
        print("[Attach] Searching for active Antigravity sessions...")
        processes = find_active_antigravity_processes()
        
        if not processes:
            print("[Attach] No active Antigravity session found.")
            sys.exit(1)
            
        print(f"\n[Attach] Found Antigravity processes:")
        for idx, p in enumerate(processes):
            print(f" [{idx}] PID: {p['pid']} | Command: {p['cmd'][:80]}...")
            
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
    mutex_name = f"Local\\KeepGoing_Antigravity_{target_pid}"
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
        log_path = os.path.join(os.path.expanduser("~"), "antigravity_attach_log.txt")

    print(f"\n[Attach] Attaching to Antigravity console of process with PID {target_pid}...")
    print("[Attach] Successfully initialized! This window is now muted.")
    print(f"[Attach] Logs are written to: {log_path}\n")
    sys.stdout.flush()
    time.sleep(0.5)
    
    # Detach and attach
    kernel32.FreeConsole()
    if not kernel32.AttachConsole(target_pid):
        kernel32.AllocConsole()
        print(f"[Error] Failed to attach console to PID {target_pid}. Error: {kernel32.GetLastError()}")
        kernel32.CloseHandle(h_process)
        sys.exit(1)
        
    h_stdin = kernel32.GetStdHandle(STD_INPUT_HANDLE)
    h_stdout = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
    
    with open(log_path, "a") as log:
        log.write(f"\n--- Antigravity Monitoring started for PID {target_pid} at {datetime.now()} ---\n")
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
                        
                        log.write(f"[{datetime.now()}] Gemini rate limit/quota error detected! Waiting for {wait_seconds}s...\n")
                        log.flush()
                        
                        time.sleep(wait_seconds)
                        
                        send_enter_to_console(h_stdin)
                        
                        log.write(f"[{datetime.now()}] Enter successfully sent to Antigravity console!\n")
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
