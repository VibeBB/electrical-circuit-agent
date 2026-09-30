from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, TextContent

from .mcp_args import _json, _socket_url
from .paths import KONNECT_SOCKET_URL

__all__ = [
    "_BASE64_MIN_LENGTH",
    "_decode_image_payload",
    "_konnect_call",
    "_konnect_image_dir",
    "_rewrite_base64_images",
    "_rewrite_text_block_images",
]

_BASE64_MIN_LENGTH = 1024


def _konnect_image_dir() -> Path:
    override = os.environ.get("CIRCUIT_KONNECT_IMAGE_DIR")
    if override:
        return Path(override)
    return Path.cwd() / "circuit-reports" / "konnect-images"


def _decode_image_payload(value: str) -> tuple[bytes, str] | None:
    candidate = value
    if candidate.startswith("data:"):
        prefix, separator, candidate = candidate.partition(",")
        if not separator or not prefix.startswith("data:image/"):
            return None
    if len(candidate) < _BASE64_MIN_LENGTH:
        return None
    try:
        data = base64.b64decode(candidate, validate=True)
    except ValueError:
        return None
    if data.startswith(b"\x89PNG"):
        return data, ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return data, ".jpg"
    return None


def _rewrite_base64_images(value: Any, image_dir: Path, counter: list[int]) -> Any:
    if isinstance(value, str):
        decoded = _decode_image_payload(value)
        if decoded is None:
            return value
        data, suffix = decoded
        image_dir.mkdir(parents=True, exist_ok=True)
        counter[0] += 1
        path = image_dir / f"{counter[0]:03d}{suffix}"
        path.write_bytes(data)
        return {
            "image_path": str(path),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    if isinstance(value, dict):
        return {
            key: _rewrite_base64_images(item, image_dir, counter)
            for key, item in cast(dict[str, Any], value).items()
        }
    if isinstance(value, list):
        return [_rewrite_base64_images(item, image_dir, counter) for item in cast(list[Any], value)]
    return value


def _rewrite_text_block_images(
    text: str,
    image_dir: Path,
    counter: list[int],
    rewrite_base64_images: Callable[[Any, Path, list[int]], Any] | None = None,
) -> str:
    try:
        parsed: object = json.loads(text)
    except json.JSONDecodeError:
        return text
    rewrite = rewrite_base64_images or _rewrite_base64_images
    return json.dumps(rewrite(parsed, image_dir, counter), ensure_ascii=False)


async def _konnect_call(
    tool: str,
    arguments: dict[str, Any],
    socket: str | None,
    ops: list[dict[str, Any]] | None = None,
    *,
    rewrite_text_block_images: Callable[[str, Path, list[int]], str] = _rewrite_text_block_images,
    rewrite_base64_images: Callable[[Any, Path, list[int]], Any] = _rewrite_base64_images,
) -> CallToolResult:
    command = shlex.split(os.environ.get("CIRCUIT_KONNECT", "konnect"))
    socket_url = (
        _socket_url(socket or os.environ.get("KICAD_API_SOCKET") or KONNECT_SOCKET_URL)
        or KONNECT_SOCKET_URL
    )
    env = {
        **os.environ,
        "KICAD_API_SOCKET": socket_url,
    }
    params = StdioServerParameters(command=command[0], args=command[1:], env=env)
    counter = [0]
    image_dir = _konnect_image_dir()
    async with (
        stdio_client(params) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        if ops is None:
            result = await session.call_tool(tool, arguments)
            for index, block in enumerate(result.content):
                text = getattr(block, "text", None)
                if text is not None:
                    result.content[index] = TextContent(
                        type="text",
                        text=rewrite_text_block_images(text, image_dir, counter),
                    )
            return result
        results: list[dict[str, Any]] = []
        for op in ops:
            op_tool = str(op.get("tool", ""))
            op_arguments_value = op.get("arguments")
            op_arguments: dict[str, Any] = (
                cast(dict[str, Any], op_arguments_value)
                if isinstance(op_arguments_value, dict)
                else {}
            )
            op_result = await session.call_tool(op_tool, op_arguments)
            content: list[Any] = []
            for block in op_result.content:
                text = getattr(block, "text", None)
                if text is not None:
                    content.append(rewrite_text_block_images(text, image_dir, counter))
                else:
                    content.append(
                        rewrite_base64_images(block.model_dump(mode="json"), image_dir, counter)
                    )
            results.append(
                {
                    "tool": op_tool,
                    "isError": bool(op_result.isError),
                    "content": content,
                }
            )
        return CallToolResult(
            content=[TextContent(type="text", text=_json({"results": results}))],
            isError=any(item["isError"] for item in results),
        )
