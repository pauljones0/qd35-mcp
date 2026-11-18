"""Translate expected domain errors at the MCP boundary."""

from collections.abc import Awaitable, Callable
from typing import ParamSpec

from mcp.server.mcpserver.exceptions import ToolError

from ..errors import QD35Error

P = ParamSpec("P")


async def call(
    method: Callable[P, Awaitable[dict[str, object]]], *args: P.args, **kwargs: P.kwargs
) -> dict[str, object]:
    try:
        return await method(*args, **kwargs)
    except (QD35Error, ValueError) as exc:
        raise ToolError(str(exc)) from exc
