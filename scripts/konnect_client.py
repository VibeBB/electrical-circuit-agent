"""Small stdio JSON-RPC client for the Konnect MCP subprocess."""

from __future__ import annotations

import json
import selectors
import subprocess
import time
from typing import Any, cast


def read_mcp_response(
    process: subprocess.Popen[str], message_id: int, timeout: float = 30.0
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if process.stdout is None:
        raise RuntimeError("Konnect stdout is unavailable")
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    messages: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        events = selector.select(max(0.0, deadline - time.monotonic()))
        if not events:
            continue
        line = process.stdout.readline()
        if not line:
            break
        value = json.loads(line)
        messages.append(value)
        if value.get("id") == message_id:
            return value, messages
    raise RuntimeError(f"timeout waiting for MCP response {message_id}: {messages}")


def request(
    process: subprocess.Popen[str], message_id: int, method: str, params: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if process.stdin is None:
        raise RuntimeError("Konnect stdin is unavailable")
    payload = {"jsonrpc": "2.0", "id": message_id, "method": method, "params": params}
    process.stdin.write(json.dumps(payload) + "\n")
    process.stdin.flush()
    return read_mcp_response(process, message_id)


def notify(
    process: subprocess.Popen[str], method: str, params: dict[str, Any] | None = None
) -> None:
    if process.stdin is None:
        raise RuntimeError("Konnect stdin is unavailable")
    payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        payload["params"] = params
    process.stdin.write(json.dumps(payload) + "\n")
    process.stdin.flush()


def tool_body(response: dict[str, Any]) -> Any:
    raw_result: Any = response.get("result")
    if not isinstance(raw_result, dict):
        raise RuntimeError(f"MCP error response: {response}")
    result = cast(dict[str, Any], raw_result)
    if result.get("isError"):
        raise RuntimeError(f"MCP tool error: {response}")
    content: Any = result.get("content", [])
    if isinstance(content, list) and content:
        first: Any = cast(Any, content[0])
        if isinstance(first, dict):
            first_item = cast(dict[str, Any], first)
            text: Any = first_item.get("text")
            if first_item.get("type") == "text" and isinstance(text, str):
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text
    return result


def call_tool(
    process: subprocess.Popen[str], next_id: int, name: str, arguments: dict[str, Any]
) -> tuple[Any, int]:
    response, _ = request(process, next_id, "tools/call", {"name": name, "arguments": arguments})
    return tool_body(response), next_id + 1
