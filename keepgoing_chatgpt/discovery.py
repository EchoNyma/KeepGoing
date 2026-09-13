"""Trusted process and bundled-runtime discovery without UI automation."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Callable, Iterable, Mapping, Sequence


class DiscoveryError(RuntimeError):
    """Raised when the desktop-owned runtime cannot be trusted."""


@dataclass(frozen=True)
class ProcessRecord:
    pid: int
    parent_pid: int
    executable_path: str
    command_line: str = ""
    environment: Mapping[str, str] = field(default_factory=dict)
    alive: bool = True

    def __post_init__(self) -> None:
        if self.pid <= 0 or self.parent_pid < 0:
            raise ValueError("process IDs must be non-negative and the PID must be positive")
        if not self.executable_path:
            raise ValueError("executable_path must not be empty")
        object.__setattr__(self, "environment", {str(key): str(value) for key, value in self.environment.items()})

    @property
    def executable_name(self) -> str:
        return Path(self.executable_path).name.lower()


@dataclass(frozen=True)
class DiscoverySnapshot:
    chatgpt_pid: int
    codex_pid: int
    chatgpt_executable: str
    codex_executable: str
    node_executable: str
    bridge_server: str
    pipe_path: str
    package_root: str
    runtime_root: str


_PROCESS_QUERY = (
    "Get-CimInstance Win32_Process | "
    "Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine | "
    "ConvertTo-Json -Compress"
)
_PIPE_PREFIX = "\\\\.\\pipe\\"
_KEY_VALUE_RE = re.compile(r"(?:^|\s)([A-Za-z_][A-Za-z0-9_]*)=(?:\"([^\"]+)\"|([^\s]+))")
_QUOTED_KEY_VALUE_RE = re.compile(
    r"(?:\\?['\"])?([A-Za-z_][A-Za-z0-9_]*)(?:\\?['\"])?\s*=\s*\\?['\"]((?:[^'\"]|\\.)*)\\?['\"]"
)
_FLAG_RE = re.compile(
    r"(?:^|\s)(--(?:node|node-path|node-executable|server|server-path|app-tools-server|app-tools-pipe-path))\s+(?:\"([^\"]+)\"|([^\s]+))",
    re.IGNORECASE,
)


def query_process_records(
    *,
    powershell: str = "powershell.exe",
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[ProcessRecord, ...]:
    """Read process ancestry/configuration using a fixed, read-only PowerShell query."""

    try:
        result = runner(
            [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", _PROCESS_QUERY],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DiscoveryError("unable to query desktop process records") from exc
    if result.returncode != 0:
        raise DiscoveryError("desktop process query failed")
    try:
        raw = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise DiscoveryError("desktop process query returned malformed JSON") from exc
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        raise DiscoveryError("desktop process query returned an invalid shape")

    records: list[ProcessRecord] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            records.append(
                ProcessRecord(
                    pid=int(item.get("ProcessId")),
                    parent_pid=int(item.get("ParentProcessId") or 0),
                    executable_path=str(item.get("ExecutablePath") or ""),
                    command_line=str(item.get("CommandLine") or ""),
                )
            )
        except (TypeError, ValueError):
            continue
    return tuple(records)


def select_trusted_runtime(
    records: Iterable[ProcessRecord],
    *,
    path_exists: Callable[[str], bool] = os.path.isfile,
) -> DiscoverySnapshot:
    """Select only a live Codex descendant and resources under trusted roots."""

    process_records = tuple(records)
    by_pid = {record.pid: record for record in process_records if record.alive}
    chatgpt_candidates = [
        record
        for record in process_records
        if record.alive
        and record.executable_name == "chatgpt.exe"
        and _is_packaged_chatgpt_path(record.executable_path)
        and path_exists(record.executable_path)
    ]
    if not chatgpt_candidates:
        raise DiscoveryError("no live packaged ChatGPT.exe was discovered")

    for chatgpt in sorted(chatgpt_candidates, key=lambda record: record.pid):
        descendants = [
            record
            for record in process_records
            if record.alive
            and record.executable_name == "codex.exe"
            and path_exists(record.executable_path)
            and _has_ancestor(record.pid, chatgpt.pid, by_pid)
        ]
        for codex in sorted(descendants, key=lambda record: record.pid):
            try:
                return _build_snapshot(chatgpt, codex, path_exists)
            except DiscoveryError:
                continue
    raise DiscoveryError("no trusted live Codex descendant with a complete bridge configuration was found")


def discover_runtime(
    *,
    powershell: str = "powershell.exe",
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    path_exists: Callable[[str], bool] = os.path.isfile,
) -> DiscoverySnapshot:
    return select_trusted_runtime(query_process_records(powershell=powershell, runner=runner), path_exists=path_exists)


def diagnostic_payload(snapshot: DiscoverySnapshot, *, version: str | None = None) -> dict[str, object]:
    """Return a small diagnostic record without command lines, environments, or payloads."""

    return {
        "codex_version": version,
        "chatgpt_pid": snapshot.chatgpt_pid,
        "codex_pid": snapshot.codex_pid,
        "chatgpt_executable": snapshot.chatgpt_executable,
        "codex_executable": snapshot.codex_executable,
        "node_executable": snapshot.node_executable,
        "bridge_server": snapshot.bridge_server,
        "pipe_path": snapshot.pipe_path,
    }


def diagnostic_json(snapshot: DiscoverySnapshot, *, version: str | None = None) -> str:
    return json.dumps(diagnostic_payload(snapshot, version=version), ensure_ascii=False, sort_keys=True)


def _build_snapshot(
    chatgpt: ProcessRecord,
    codex: ProcessRecord,
    path_exists: Callable[[str], bool],
) -> DiscoverySnapshot:
    package_root = _canonical_path(str(Path(chatgpt.executable_path).parent))
    runtime_root = _canonical_path(str(Path(codex.executable_path).parent))
    managed_root = _config_value(codex, "CODEX_APP_MANAGED_ROOT", "CODEX_MANAGED_ROOT")
    trusted_roots = [package_root]
    if managed_root:
        managed_root = _canonical_path(managed_root)
        if not _is_within(managed_root, package_root):
            local_app_data = os.environ.get("LOCALAPPDATA", "")
            if not local_app_data or not _is_within(managed_root, _canonical_path(local_app_data)):
                raise DiscoveryError("app-managed runtime root is outside trusted application data")
        trusted_roots.append(managed_root)
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    standard_managed_root = _canonical_path(os.path.join(local_app_data, "OpenAI", "Codex")) if local_app_data else ""
    if standard_managed_root and _is_within(codex.executable_path, standard_managed_root):
        trusted_roots.append(standard_managed_root)
    if not any(_is_within(codex.executable_path, root) for root in trusted_roots):
        raise DiscoveryError("Codex executable is outside the ChatGPT package/runtime")

    config = _configuration(codex)
    pipe_path = config.get("CODEX_APP_TOOLS_PIPE_PATH", "").strip().strip('"')
    if not _valid_pipe_path(pipe_path):
        raise DiscoveryError("Codex App Tools named pipe is missing or untrusted")

    node_path = _first_config(
        config,
        "CODEX_BUNDLED_NODE_PATH",
        "CODEX_APP_TOOLS_NODE_PATH",
        "CODEX_NODE_PATH",
        "CODEX_MCP_NODE_PATH",
    )
    server_path = _first_config(
        config,
        "CODEX_APP_TOOLS_SERVER_PATH",
        "CODEX_APP_TOOLS_SERVER",
        "CODEX_APP_TOOLS_SERVER_MJS",
        "CODEX_SERVER_PATH",
    )
    if not node_path:
        node_path = _find_resource((runtime_root, package_root), ("node.exe", "node"), path_exists)
    if not server_path:
        tools_cwd = _first_config(config, "CODEX_APP_TOOLS_CWD")
        if tools_cwd:
            server_path = os.path.join(tools_cwd, "server.mjs")
    if not server_path:
        server_path = _find_resource(
            (runtime_root, package_root),
            ("app-tools\\server.mjs", "app-tools/server.mjs", "server.mjs"),
            path_exists,
        )
    if not node_path or not server_path:
        raise DiscoveryError("bundled Node or App Tools server path is missing")
    node_path = _canonical_path(node_path)
    server_path = _canonical_path(server_path)
    if not path_exists(node_path) or not path_exists(server_path):
        raise DiscoveryError("bundled Node or App Tools server does not exist")
    if not any(_is_within(node_path, root) for root in trusted_roots):
        raise DiscoveryError("bundled Node executable is outside the trusted runtime")
    if not any(_is_within(server_path, root) for root in trusted_roots):
        raise DiscoveryError("App Tools server is outside the trusted runtime")

    return DiscoverySnapshot(
        chatgpt_pid=chatgpt.pid,
        codex_pid=codex.pid,
        chatgpt_executable=_canonical_path(chatgpt.executable_path),
        codex_executable=_canonical_path(codex.executable_path),
        node_executable=node_path,
        bridge_server=server_path,
        pipe_path=pipe_path,
        package_root=package_root,
        runtime_root=runtime_root,
    )


def _configuration(record: ProcessRecord) -> dict[str, str]:
    result = {key.upper(): value for key, value in record.environment.items()}
    command_line = record.command_line or ""
    for match in _KEY_VALUE_RE.finditer(command_line):
        result[match.group(1).upper()] = match.group(2) or match.group(3) or ""
    for match in _QUOTED_KEY_VALUE_RE.finditer(command_line):
        key = match.group(1).upper()
        value = match.group(2).replace("\\\"", '"').replace("\\\\", "\\")
        if value.endswith("\\") and key in {
            "CODEX_APP_TOOLS_PIPE_PATH",
            "CODEX_MCP_NODE_PATH",
            "CODEX_APP_TOOLS_CWD",
            "CODEX_BUNDLED_NODE_PATH",
            "CWD",
        }:
            value = value[:-1]
        if key == "CWD" and "codex-app-tools" in value.lower():
            key = "CODEX_APP_TOOLS_CWD"
        result[key] = value
    for match in _FLAG_RE.finditer(command_line):
        flag = match.group(1).lower()
        value = match.group(2) or match.group(3) or ""
        flag_key = {
            "--node": "CODEX_BUNDLED_NODE_PATH",
            "--node-path": "CODEX_BUNDLED_NODE_PATH",
            "--node-executable": "CODEX_BUNDLED_NODE_PATH",
            "--server": "CODEX_APP_TOOLS_SERVER_PATH",
            "--server-path": "CODEX_APP_TOOLS_SERVER_PATH",
            "--app-tools-server": "CODEX_APP_TOOLS_SERVER_PATH",
            "--app-tools-pipe-path": "CODEX_APP_TOOLS_PIPE_PATH",
        }[flag]
        result[flag_key] = value
    return result


def _config_value(record: ProcessRecord, *keys: str) -> str | None:
    config = _configuration(record)
    return _first_config(config, *keys)


def _first_config(config: Mapping[str, str], *keys: str) -> str | None:
    for key in keys:
        value = config.get(key.upper())
        if value:
            return value
    return None


def _find_resource(
    roots: Sequence[str], names: Sequence[str], path_exists: Callable[[str], bool]
) -> str | None:
    for root in roots:
        for name in names:
            candidate = os.path.join(root, name)
            if path_exists(candidate):
                return candidate
    return None


def _valid_pipe_path(value: str) -> bool:
    if not value.lower().startswith(_PIPE_PREFIX.lower()):
        return False
    name = value[len(_PIPE_PREFIX) :]
    lowered = name.lower()
    return (
        bool(name)
        and "\\" not in name
        and "/" not in name
        and lowered.startswith("codex-")
        and ("app-tools" in lowered or "browser-use" in lowered)
    )


def _canonical_path(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(os.path.expandvars(path))))


def _is_packaged_chatgpt_path(path: str) -> bool:
    executable = Path(_canonical_path(path))
    package = executable.parent.parent
    return (
        executable.name.casefold() == "chatgpt.exe"
        and executable.parent.name.casefold() == "app"
        and package.parent.name.casefold() == "windowsapps"
        and package.name.casefold().startswith("openai.codex_")
    )


def _is_within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath((_canonical_path(path), _canonical_path(root))) == _canonical_path(root)
    except ValueError:
        return False


def _has_ancestor(pid: int, ancestor_pid: int, by_pid: Mapping[int, ProcessRecord]) -> bool:
    seen: set[int] = set()
    current = pid
    while current not in seen:
        if current == ancestor_pid:
            return True
        seen.add(current)
        record = by_pid.get(current)
        if record is None or record.parent_pid == current:
            return False
        current = record.parent_pid
    return False


__all__ = [
    "DiscoveryError",
    "DiscoverySnapshot",
    "ProcessRecord",
    "diagnostic_json",
    "diagnostic_payload",
    "discover_runtime",
    "query_process_records",
    "select_trusted_runtime",
]
