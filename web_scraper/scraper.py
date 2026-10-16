"""Фасад Scraper: собирает зависимости и выполняет операции. Наружу всегда отдаёт Envelope —
исключения не выходят за пределы этого класса."""

from __future__ import annotations

import logging
import time
from typing import Any

from pydantic import BaseModel, ValidationError

from .config import Config
from .core.browser import BrowserRenderer
from .core.cache import PageCache
from .core.http import HttpClient
from .core.ratelimit import DomainRateLimiter
from .core.robots import RobotsChecker
from .errors import ScraperError, describe_validation_errors
from .llm import LLMProvider, create_provider
from .models import (
    CrawlRequest,
    Envelope,
    ErrorInfo,
    ExtractRequest,
    FetchRequest,
    SearchAndFetchRequest,
    SearchRequest,
)
from .operations import OPERATIONS
from .ops.crawl import crawl
from .ops.extract import Extractor
from .ops.fetch import Fetcher
from .ops.search import search
from .ops.search_and_fetch import search_and_fetch
from .plugins import PluginContext, PluginRegistry

log = logging.getLogger(__name__)

_DEFAULT = object()

Payload = BaseModel | dict[str, Any] | None


class Scraper:
    def __init__(self, config: Config | None = None, *, browser: Any = _DEFAULT, llm: LLMProvider | None = None):
        """browser и llm можно подменить (в тестах); по умолчанию создаются из конфига."""
        self.config = cfg = config or Config()
        self.limiter = DomainRateLimiter(cfg.rate_limit.default_rps, cfg.rate_limit.domains)
        self.http = HttpClient(cfg.http, self.limiter)
        self.cache = PageCache(cfg.cache.path, cfg.cache.ttl_seconds, cfg.cache.enabled)
        if browser is _DEFAULT:
            browser = (
                BrowserRenderer(cfg.browser, proxy=cfg.http.proxy, accept_language=cfg.http.accept_language)
                if cfg.browser.enabled
                else None
            )
        self.browser: BrowserRenderer | None = browser
        self.plugins = PluginRegistry.load(cfg.plugins, PluginContext(cfg, self.http, self.cache, self.limiter))
        self.fetcher = Fetcher(cfg, self.http, self.cache, self.browser, self.plugins)
        self.robots = RobotsChecker(self.http) if cfg.crawl.respect_robots else None
        self._llm = llm
        self.extractor = Extractor(cfg, self.fetcher, self._get_llm, self.plugins)

    def _get_llm(self) -> LLMProvider:
        if self._llm is None:
            self._llm = create_provider(self.config.llm)
        return self._llm

    # ------------------------------------------------------------------ операции

    async def search(self, request: SearchRequest | dict[str, Any]) -> Envelope:
        return await self.call("search", request)

    async def fetch(self, request: FetchRequest | dict[str, Any]) -> Envelope:
        return await self.call("fetch", request)

    async def extract(self, request: ExtractRequest | dict[str, Any]) -> Envelope:
        return await self.call("extract", request)

    async def crawl(self, request: CrawlRequest | dict[str, Any]) -> Envelope:
        return await self.call("crawl", request)

    async def search_and_fetch(self, request: SearchAndFetchRequest | dict[str, Any]) -> Envelope:
        return await self.call("search_and_fetch", request)

    async def call(self, name: str, payload: Payload) -> Envelope:
        """Выполнить операцию по имени. Вход — модель запроса или dict (как пришёл от агента)."""
        operation = OPERATIONS.get(name)
        if operation is None:
            message = f"unknown operation {name!r}; available: {', '.join(OPERATIONS)}"
            return Envelope(ok=False, error=ErrorInfo(kind="invalid_input", message=message))

        started = time.monotonic()
        target = ""
        try:
            if isinstance(payload, operation.request):
                request = payload
            else:
                data = payload.model_dump(by_alias=True) if isinstance(payload, BaseModel) else payload or {}
                request = operation.request.model_validate(data)
            target = str(getattr(request, "url", None) or getattr(request, "start_url", None)
                         or getattr(request, "query", ""))
            result = await self._execute(name, request)
        except ValidationError as e:
            error = ErrorInfo(kind="invalid_input", message=describe_validation_errors(e.errors()))
        except ScraperError as e:
            error = e.to_info()
        except Exception as e:
            log.exception("%s %s: внутренняя ошибка", name, target)
            error = ErrorInfo(kind="internal", message=f"{type(e).__name__}: {e}")
        else:
            log.info("%s %s → ok (%.2fс)", name, target, time.monotonic() - started)
            return Envelope(ok=True, result=result)

        log.warning("%s %s → %s: %s (%.2fс)", name, target, error.kind, error.message, time.monotonic() - started)
        return Envelope(ok=False, error=error)

    async def _execute(self, name: str, request: Any) -> BaseModel:
        cfg = self.config
        if name == "search":
            return await search(request, cfg.searxng, self.http)
        if name == "fetch":
            return await self.fetcher.fetch(request)
        if name == "extract":
            return await self.extractor.extract(request)
        if name == "crawl":
            return await crawl(request, config=cfg, fetcher=self.fetcher, robots=self.robots)
        return await search_and_fetch(request, config=cfg, http=self.http, fetcher=self.fetcher)

    # ------------------------------------------------------------------ жизненный цикл

    async def aclose(self) -> None:
        await self.plugins.aclose()
        if self.browser is not None:
            await self.browser.aclose()
        if self._llm is not None:
            await self._llm.aclose()
        await self.http.aclose()
        self.cache.close()

    async def __aenter__(self) -> Scraper:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()
