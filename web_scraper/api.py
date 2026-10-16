"""HTTP API для агента: POST-эндпоинт на каждую операцию, GET /tools и /health."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import __version__
from .config import Config, load_config
from .errors import describe_validation_errors
from .models import (
    CrawlRequest,
    CrawlResult,
    Envelope,
    ErrorInfo,
    ExtractRequest,
    ExtractResult,
    FetchRequest,
    FetchResult,
    SearchAndFetchRequest,
    SearchAndFetchResult,
    SearchRequest,
    SearchResult,
)
from .operations import OPERATIONS
from .scraper import Scraper
from .tools import tool_definitions


def create_app(config: Config | None = None, scraper: Scraper | None = None) -> FastAPI:
    """Приложение FastAPI. Переданный scraper не закрывается при остановке — им владеет вызывающий."""
    owns_scraper = scraper is None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if getattr(app.state, "scraper", None) is None:
            app.state.scraper = Scraper(config or load_config())
        await app.state.scraper.cache.purge_expired()
        try:
            yield
        finally:
            if owns_scraper:
                await app.state.scraper.aclose()

    app = FastAPI(
        title="web-scraper",
        version=__version__,
        description="Universal scraping tool for AI agents: search, fetch, extract, crawl, search_and_fetch.",
        lifespan=lifespan,
    )
    if scraper is not None:
        app.state.scraper = scraper

    def get_scraper(request: Request) -> Scraper:
        return request.app.state.scraper

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        error = ErrorInfo(kind="invalid_input", message=describe_validation_errors(exc.errors(), skip_prefix="body"))
        return JSONResponse(status_code=422, content=Envelope(ok=False, error=error).model_dump(exclude_none=True))

    def route(name: str) -> dict[str, Any]:
        return {
            "response_model_exclude_none": True,
            "summary": name,
            "description": OPERATIONS[name].description,
        }

    @app.post("/search", response_model=Envelope[SearchResult], **route("search"))
    async def search(body: SearchRequest, scraper: Scraper = Depends(get_scraper)) -> Envelope:
        return await scraper.search(body)

    @app.post("/fetch", response_model=Envelope[FetchResult], **route("fetch"))
    async def fetch(body: FetchRequest, scraper: Scraper = Depends(get_scraper)) -> Envelope:
        return await scraper.fetch(body)

    @app.post("/extract", response_model=Envelope[ExtractResult], **route("extract"))
    async def extract(body: ExtractRequest, scraper: Scraper = Depends(get_scraper)) -> Envelope:
        return await scraper.extract(body)

    @app.post("/crawl", response_model=Envelope[CrawlResult], **route("crawl"))
    async def crawl(body: CrawlRequest, scraper: Scraper = Depends(get_scraper)) -> Envelope:
        return await scraper.crawl(body)

    @app.post("/search_and_fetch", response_model=Envelope[SearchAndFetchResult], **route("search_and_fetch"))
    async def search_and_fetch(body: SearchAndFetchRequest, scraper: Scraper = Depends(get_scraper)) -> Envelope:
        return await scraper.search_and_fetch(body)

    @app.get("/tools", summary="Tool definitions (JSON Schema) for the Claude API tools parameter")
    async def tools() -> list[dict[str, Any]]:
        return tool_definitions()

    @app.get("/health")
    async def health(scraper: Scraper = Depends(get_scraper)) -> dict[str, Any]:
        return {
            "ok": True,
            "version": __version__,
            "browser": scraper.browser is not None,
            "plugins": scraper.plugins.names,
            "llm_provider": scraper.config.llm.provider,
        }

    return app
