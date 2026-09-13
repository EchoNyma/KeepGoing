"""MCP bridge adapter; the only component allowed to write to desktop tasks."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from typing import Any, Mapping, Protocol, Sequence

from .jsonrpc import (
    JsonRpcError,
    JsonRpcProcessExitedError,
    JsonRpcRemoteError,
    JsonRpcTimeoutError,
    sanitize_error_text,
)


class DesktopBridgeError(RuntimeError):
    pass


class DesktopBridgeContractError(DesktopBridgeError):
    pass


class DesktopBridgeMethodDenied(DesktopBridgeError):
    pass


class DesktopBridgeToolError(DesktopBridgeError):
    def __init__(self, message: str, *, confirmed: bool = True) -> None:
        self.confirmed = confirmed
        super().__init__(sanitize_error_text(message))


class DesktopBridgeTransportUncertain(DesktopBridgeError):
    """The bridge may have accepted a send before transport failure."""

    def __init__(self, message: str, *, cause: BaseException | None = None) -> None:
        self.cause = cause
        super().__init__(sanitize_error_text(message))


class _Rpc(Protocol):
    def request(self, method: str, params: Any = None, timeout: float | None = None) -> Any: ...

    def send_notification(self, method: str, params: Any = None) -> None: ...


@dataclass(frozen=True)
class BridgeInfo:
    protocol_version: str
    server_name: str
    server_version: str


REQUIRED_TOOLS = frozenset({"list_threads", "wait_threads", "send_message_to_thread"})
READ_TOOL_ALIASES = ("read_thread", "get_thread", "thread_read")


class DesktopTaskBridge:
    def __init__(
        self,
        rpc: _Rpc,
        *,
        request_timeout: float = 10.0,
        executor_thread_id: str | None = None,
    ) -> None:
        self.rpc = rpc
        self.request_timeout = request_timeout
        self.executor_thread_id = executor_thread_id
        self.info: BridgeInfo | None = None
        self.tools: dict[str, Mapping[str, Any]] = {}
        self.allowed_tools: frozenset[str] = frozenset()
        self.read_tool_name: str | None = None
        self.ready_for_dispatch = False

    @property
    def protocol_version(self) -> str | None:
        return self.info.protocol_version if self.info else None

    def initialize(self) -> BridgeInfo:
        result = self.rpc.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "keepgoing", "version": "1.0.0"},
            },
            timeout=self.request_timeout,
        )
        if not isinstance(result, Mapping):
            raise DesktopBridgeContractError("MCP initialize returned a non-object result")
        protocol_version = result.get("protocolVersion")
        server_info = result.get("serverInfo")
        if not isinstance(protocol_version, str) or not isinstance(server_info, Mapping):
            raise DesktopBridgeContractError("MCP initialize omitted protocol or server information")
        server_name = server_info.get("name")
        server_version = server_info.get("version")
        if not isinstance(server_name, str) or not isinstance(server_version, str):
            raise DesktopBridgeContractError("MCP server information is incomplete")
        self.rpc.send_notification("notifications/initialized")
        tool_result = self.rpc.request("tools/list", {}, timeout=self.request_timeout)
        tools = _parse_tools(tool_result)
        required = REQUIRED_TOOLS.difference(tools)
        read_tool = next((name for name in READ_TOOL_ALIASES if name in tools), None)
        if required or read_tool is None:
            missing = sorted(required | {"read_thread"} if read_tool is None else required)
            raise DesktopBridgeContractError(f"MCP bridge is missing required tools: {', '.join(missing)}")
        self.info = BridgeInfo(protocol_version, server_name, server_version)
        self.tools = tools
        self.allowed_tools = frozenset(REQUIRED_TOOLS | {read_tool})
        self.read_tool_name = read_tool
        self.ready_for_dispatch = True
        return self.info

    def reconnect(self, rpc: _Rpc) -> BridgeInfo:
        old_rpc = self.rpc
        if old_rpc is not rpc:
            close = getattr(old_rpc, "close", None)
            if callable(close):
                close()
        self.rpc = rpc
        self.info = None
        self.tools = {}
        self.allowed_tools = frozenset()
        self.read_tool_name = None
        self.ready_for_dispatch = False
        return self.initialize()

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Any:
        if not self.ready_for_dispatch or name not in self.allowed_tools:
            raise DesktopBridgeMethodDenied(f"MCP tool is not allowlisted or bridge is not ready: {name}")
        if not isinstance(arguments, Mapping):
            raise ValueError("tool arguments must be an object")
        if name == "send_message_to_thread":
            _validate_send_arguments(arguments)
        try:
            request: dict[str, Any] = {"name": name, "arguments": dict(arguments)}
            if self.executor_thread_id:
                request["_meta"] = {"threadId": self.executor_thread_id}
            result = self.rpc.request(
                "tools/call",
                request,
                timeout=self.request_timeout,
            )
        except JsonRpcRemoteError as exc:
            raise DesktopBridgeToolError(exc.message, confirmed=True) from exc
        except (JsonRpcTimeoutError, JsonRpcProcessExitedError) as exc:
            raise DesktopBridgeTransportUncertain(str(exc), cause=exc) from exc
        except JsonRpcError as exc:
            raise DesktopBridgeTransportUncertain(str(exc), cause=exc) from exc
        return _unwrap_tool_result(result)

    def list_threads(self, *, limit: int = 50, cursor: str | None = None) -> Any:
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        arguments: dict[str, Any] = {"limit": limit}
        if cursor:
            arguments["cursor"] = cursor
        return self.call_tool("list_threads", arguments)

    def read_thread(
        self,
        thread_id: str,
        *,
        host_id: str | None = None,
        cursor: str | None = None,
        turn_limit: int = 10,
    ) -> Any:
        if not thread_id:
            raise ValueError("thread_id must not be empty")
        if not 1 <= turn_limit <= 10:
            raise ValueError("turn_limit must be between 1 and 10")
        arguments: dict[str, Any] = {"threadId": thread_id}
        if host_id is not None:
            arguments["hostId"] = host_id
        if cursor:
            arguments["cursor"] = cursor
        arguments["turnLimit"] = turn_limit
        assert self.read_tool_name is not None
        return self.call_tool(self.read_tool_name, arguments)

    def wait_for_thread(self, thread_id: str, *, host_id: str | None = None, timeout_ms: int = 1000) -> Any:
        if not thread_id:
            raise ValueError("thread_id must not be empty")
        if timeout_ms < 0:
            raise ValueError("timeout_ms must not be negative")
        target: dict[str, Any] = {"threadId": thread_id}
        if host_id is not None:
            target["hostId"] = host_id
        return self.call_tool("wait_threads", {"targets": [target], "timeoutMs": timeout_ms})

    def send_message(self, thread_id: str, prompt: str, host_id: str | None = None) -> Any:
        if not thread_id:
            raise ValueError("thread_id must not be empty")
        if not prompt:
            raise ValueError("prompt must not be empty")
        arguments: dict[str, Any] = {"threadId": thread_id, "prompt": prompt}
        if host_id is not None:
            arguments["hostId"] = host_id
        return self.call_tool("send_message_to_thread", arguments)

    def close(self) -> None:
        close = getattr(self.rpc, "close", None)
        if callable(close):
            close()
        self.ready_for_dispatch = False

    @classmethod
    def launch(
        cls,
        snapshot: Any,
        *,
        request_timeout: float = 10.0,
        node_args: Sequence[str] = (),
        executor_thread_id: str | None = None,
    ) -> "DesktopTaskBridge":
        from .jsonrpc import JsonRpcClient

        environment = os.environ.copy()
        environment["CODEX_APP_TOOLS_PIPE_PATH"] = snapshot.pipe_path
        rpc = JsonRpcClient(
            [snapshot.node_executable, *node_args, snapshot.bridge_server],
            env=environment,
            request_timeout=request_timeout,
        )
        rpc.start()
        bridge = cls(rpc, request_timeout=request_timeout, executor_thread_id=executor_thread_id)
        bridge.initialize()
        return bridge


def _parse_tools(result: Any) -> dict[str, Mapping[str, Any]]:
    if not isinstance(result, Mapping) or not isinstance(result.get("tools"), list):
        raise DesktopBridgeContractError("MCP tools/list returned no tool list")
    tools: dict[str, Mapping[str, Any]] = {}
    for item in result["tools"]:
        if isinstance(item, Mapping) and isinstance(item.get("name"), str):
            tools[item["name"]] = item
    return tools


def _validate_send_arguments(arguments: Mapping[str, Any]) -> None:
    allowed = {"threadId", "prompt", "hostId"}
    if set(arguments).difference(allowed):
        raise DesktopBridgeContractError("send_message_to_thread accepts only threadId, prompt, and optional hostId")
    if not isinstance(arguments.get("threadId"), str) or not arguments["threadId"]:
        raise DesktopBridgeContractError("send_message_to_thread requires a non-empty threadId")
    if not isinstance(arguments.get("prompt"), str) or not arguments["prompt"]:
        raise DesktopBridgeContractError("send_message_to_thread requires a non-empty prompt")
    if "hostId" in arguments and (not isinstance(arguments["hostId"], str) or not arguments["hostId"]):
        raise DesktopBridgeContractError("send_message_to_thread hostId must be a non-empty string when supplied")


def _unwrap_tool_result(result: Any) -> Any:
    if not isinstance(result, Mapping):
        raise DesktopBridgeContractError("MCP tools/call returned a non-object result")
    if result.get("isError") is True:
        raise DesktopBridgeToolError(_tool_text(result), confirmed=True)
    if "structuredContent" in result:
        return result["structuredContent"]
    content = result.get("content")
    if not isinstance(content, list):
        return result
    texts = [item.get("text") for item in content if isinstance(item, Mapping) and isinstance(item.get("text"), str)]
    if len(texts) == 1:
        try:
            return json.loads(texts[0])
        except json.JSONDecodeError:
            return {"text": texts[0]}
    return {"content": content}


def _tool_text(result: Mapping[str, Any]) -> str:
    content = result.get("content")
    if isinstance(content, list):
        parts = [item.get("text") for item in content if isinstance(item, Mapping) and isinstance(item.get("text"), str)]
        if parts:
            return " ".join(parts)
    return sanitize_error_text(str(result.get("error", "desktop bridge rejected tool call")))


__all__ = [
    "BridgeInfo",
    "DesktopBridgeContractError",
    "DesktopBridgeError",
    "DesktopBridgeMethodDenied",
    "DesktopBridgeToolError",
    "DesktopBridgeTransportUncertain",
    "DesktopTaskBridge",
    "REQUIRED_TOOLS",
]
