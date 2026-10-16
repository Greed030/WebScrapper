"""Отрисовка страниц в headless Chromium через Playwright. Браузер запускается лениво, один на процесс."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

from playwright.async_api import Browser, Playwright, Route, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from ..config import BrowserConfig
from ..errors import ScraperError

log = logging.getLogger(__name__)

_HEAVY_RESOURCES = {"image", "media", "font"}


@dataclass
class RenderedPage:
    url: str
    status_code: int
    html: str
    content_type: str | None = None


class BrowserRenderer:
    def __init__(self, cfg: BrowserConfig, *, proxy: str | None = None, accept_language: str = "en-US") -> None:
        self.cfg = cfg
        self.proxy = proxy
        self.accept_language = accept_language
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(cfg.max_concurrent)
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    async def render(self, url: str, user_agent: str) -> RenderedPage:
        browser = await self._ensure_browser()
        async with self._semaphore:
            context = await browser.new_context(
                user_agent=user_agent,
                extra_http_headers={"Accept-Language": self.accept_language},
            )
            try:
                page = await context.new_page()
                await page.route("**/*", _block_heavy)
                response = None
                try:
                    response = await page.goto(url, wait_until=self.cfg.wait_until, timeout=self.cfg.timeout * 1000)
                except PlaywrightTimeoutError as e:
                    # networkidle может так и не наступить (веб-сокеты, аналитика) — берём то, что успело загрузиться.
                    if page.url in ("", "about:blank"):
                        raise ScraperError("timeout", f"browser navigation timed out after {self.cfg.timeout}s",
                                           url=url) from e
                    log.info("%s: не дождались %s, берём текущий DOM", url, self.cfg.wait_until)
                html = await page.content()
                content_type = await response.header_value("content-type") if response else None
                return RenderedPage(
                    url=page.url,
                    status_code=response.status if response else 200,
                    html=html,
                    content_type=content_type,
                )
            except PlaywrightError as e:
                raise _map_browser_error(e, url) from e
            finally:
                await context.close()

    async def _ensure_browser(self) -> Browser:
        async with self._lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            launch: dict[str, Any] = {"headless": self.cfg.headless}
            if self.proxy:
                launch["proxy"] = _playwright_proxy(self.proxy)
            try:
                self._browser = await self._playwright.chromium.launch(**launch)
            except PlaywrightError as e:
                raise ScraperError(
                    "internal",
                    f"cannot launch Chromium ({e.message.splitlines()[0]}); "
                    "run: python -m playwright install chromium",
                ) from e
            log.info("Chromium запущен (headless=%s)", self.cfg.headless)
            return self._browser

    async def aclose(self) -> None:
        if self._browser is not None:
            try:
                await self._browser.close()
            except PlaywrightError:
                pass
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None


async def _block_heavy(route: Route) -> None:
    if route.request.resource_type in _HEAVY_RESOURCES:
        await route.abort()
    else:
        await route.continue_()


def _playwright_proxy(proxy: str) -> dict[str, str]:
    parts = urlsplit(proxy)
    server = f"{parts.scheme}://{parts.hostname}" + (f":{parts.port}" if parts.port else "")
    result = {"server": server}
    if parts.username:
        result["username"] = unquote(parts.username)
    if parts.password:
        result["password"] = unquote(parts.password)
    return result


def _map_browser_error(error: PlaywrightError, url: str) -> ScraperError:
    message = error.message.splitlines()[0] if error.message else str(error)
    if "Download is starting" in message:  # по ссылке файл (PDF, архив), а не страница
        return ScraperError("unsupported_content", "the URL is a file download, not a web page", url=url)
    if "ERR_NAME_NOT_RESOLVED" in message:
        return ScraperError("not_found", "domain name could not be resolved", url=url)
    if "ERR_TIMED_OUT" in message or "Timeout" in message:
        return ScraperError("timeout", f"browser: {message}", url=url)
    return ScraperError("network", f"browser: {message}", url=url)
