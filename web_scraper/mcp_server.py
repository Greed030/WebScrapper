"""MCP-сервер (stdio) с теми же пятью инструментами, что и HTTP API. Нужен extra [mcp]."""

from __future__ import annotations

from typing import Any

import mcp_types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from . import __version__
from .config import Config
from .models import Envelope, ErrorInfo
from .operations import operation_for_tool
from .scraper import Scraper
from .tools import tool_definitions

_INSTRUCTIONS = (
    "Web scraping tools: scraper_search finds pages, scraper_fetch reads one page, scraper_extract pulls "
    "structured data, scraper_crawl walks a site, scraper_search_and_fetch searches and reads in one call."
)


def build_server(scraper: Scraper) -> Server[Any]:
    tools = [
        types.Tool(name=d["name"], description=d["description"], input_schema=d["input_schema"])
        for d in tool_definitions()
    ]

    async def list_tools(ctx: Any, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tools)

    async def call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        operation = operation_for_tool(params.name)
        if operation is None:
            error = ErrorInfo(kind="invalid_input", message=f"unknown tool {params.name!r}")
            envelope = Envelope(ok=False, error=error)
        else:
            envelope = await scraper.call(operation.name, params.arguments or {})
        text = envelope.model_dump_json(exclude_none=True)
        return types.CallToolResult(content=[types.TextContent(text=text)], is_error=not envelope.ok)

    return Server(
        "web-scraper",
        version=__version__,
        instructions=_INSTRUCTIONS,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


async def run_stdio(config: Config) -> None:
    async with Scraper(config) as scraper:
        server = build_server(scraper)
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
