from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.interceptors import MCPToolCallRequest
from langchain_mcp_adapters.sessions import (
    StdioConnection,
    StreamableHttpConnection,
    create_session,
)
from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.types import Command
from mcp.types import CallToolResult, TextContent

from src.agents.sothoth.untrusted import untrusted
from src.core.config import BrowserCommand, BrowserServer

BROWSER_TOOLS = frozenset(
    {
        "browser_navigate",
        "browser_navigate_back",
        "browser_snapshot",
        "browser_find",
        "browser_click",
        "browser_type",
        "browser_press_key",
        "browser_select_option",
        "browser_hover",
        "browser_wait_for",
        "browser_tabs",
        "browser_handle_dialog",
    }
)
"""The Playwright MCP tools the agent gets; the others run code, touch files, read
the network or take screenshots"""


async def _as_page(
    request: MCPToolCallRequest,
    handler: Callable[
        [MCPToolCallRequest], Awaitable[CallToolResult | ToolMessage | Command]
    ],
) -> CallToolResult | ToolMessage | Command:
    """Runs the tool without saving its output to a file, and returns its text inside
    <page untrusted>"""
    result = await handler(
        request.override(
            args={
                key: value for key, value in request.args.items() if key != "filename"
            }
        )
    )
    if not isinstance(result, CallToolResult):
        return result
    return result.model_copy(
        update={
            "content": [
                TextContent(type="text", text=untrusted("page", block.text))
                if isinstance(block, TextContent)
                else block
                for block in result.content
            ]
        }
    )


@asynccontextmanager
async def open_browser(
    browser: BrowserCommand | BrowserServer,
) -> AsyncIterator[list[BaseTool]]:
    """Opens a Playwright MCP session and yields its browser tools; the browser closes
    on exit"""
    async with create_session(
        StdioConnection(
            transport="stdio", command=browser.command, args=list(browser.args)
        )
        if isinstance(browser, BrowserCommand)
        else StreamableHttpConnection(transport="streamable_http", url=browser.url)
    ) as session:
        await session.initialize()
        yield [
            tool.model_copy(
                update={"metadata": {**(tool.metadata or {}), "codename": "Traversal"}}
            )
            for tool in await load_mcp_tools(session, tool_interceptors=[_as_page])
            if tool.name in BROWSER_TOOLS
        ]
