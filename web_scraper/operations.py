"""Реестр операций: модели входа/выхода и описания для агента. Единый источник для
FastAPI, tool definitions, MCP и CLI."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from .models import (
    CrawlRequest,
    CrawlResult,
    ExtractRequest,
    ExtractResult,
    FetchRequest,
    FetchResult,
    SearchAndFetchRequest,
    SearchAndFetchResult,
    SearchRequest,
    SearchResult,
)

TOOL_PREFIX = "scraper_"

_ERRORS_NOTE = (
    "Failures never raise: the response is {ok: false, error: {kind, message, retryable}} with kind one of "
    "blocked, timeout, not_found, http_error, network, unsupported_content, invalid_input, llm_error, internal."
)


@dataclass(frozen=True)
class Operation:
    name: str
    request: type[BaseModel]
    result: type[BaseModel]
    description: str

    @property
    def tool_name(self) -> str:
        return TOOL_PREFIX + self.name


OPERATIONS: dict[str, Operation] = {
    op.name: op
    for op in (
        Operation(
            "search",
            SearchRequest,
            SearchResult,
            "Search the web through a self-hosted SearXNG metasearch engine and return result titles, URLs and "
            "snippets. Use it to discover pages; then read them with scraper_fetch, or use "
            "scraper_search_and_fetch to search and read the top results in one call. " + _ERRORS_NOTE,
        ),
        Operation(
            "fetch",
            FetchRequest,
            FetchResult,
            "Download one web page and return its main content as markdown, plus title, meta tags, all links, "
            "and the e-mail addresses and phone numbers found anywhere on the page (including header/footer). "
            "JavaScript-rendered pages are opened in a headless browser automatically. Long text is cut: when "
            "truncated is true, call again with offset=next_offset to read the next chunk. " + _ERRORS_NOTE,
        ),
        Operation(
            "extract",
            ExtractRequest,
            ExtractResult,
            "Extract structured data from one page. mode='selectors': you give CSS or XPath selectors per field "
            "(fast and deterministic; best for known layouts and repeated items such as list cards). "
            "mode='llm': you give a JSON Schema and a language model fills it from the page content; the answer "
            "is validated against the schema (flexible, slower, costs tokens). Selectors that match nothing are "
            "reported in warnings. " + _ERRORS_NOTE,
        ),
        Operation(
            "crawl",
            CrawlRequest,
            CrawlResult,
            "Crawl a website breadth-first from start_url, following links up to max_depth and max_pages, "
            "optionally only links whose URL matches url_pattern (regex). Respects robots.txt. Returns a short "
            "text excerpt, e-mails and phones for every page visited; per-page failures are in pages[].error. "
            "Keep crawls focused with url_pattern and small limits; use scraper_fetch for full page text. "
            + _ERRORS_NOTE,
        ),
        Operation(
            "search_and_fetch",
            SearchAndFetchRequest,
            SearchAndFetchResult,
            "Search the web and fetch the top n results in one call. Returns each result's title, URL and snippet "
            "together with a text excerpt of the page, its e-mails and phones. A page that fails to load has "
            "page.error set; the other results are still returned. " + _ERRORS_NOTE,
        ),
    )
}


def operation_for_tool(tool_name: str) -> Operation | None:
    name = tool_name.removeprefix(TOOL_PREFIX)
    return OPERATIONS.get(name)
